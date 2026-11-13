from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm import LLMConfig, OpenAILLM

from .contracts import validate_output
from .inventory import DEFAULT_SUITE, read_cases, validate_case
from .release import PromptRelease, PromptReleaseError


def run_evaluation(
    release: PromptRelease,
    cases_path: Path | None,
    output_root: Path,
    *,
    llm_config: LLMConfig | None = None,
    llm=None,
    nodes: list[str] | None = None,
    require_coverage: bool = False,
    validation_retries: int = 0,
) -> Path:
    """Evaluate prompts using the exact same shared LLM adapter as production.

    AppService passes the resolved pipeline configuration. Standalone callers
    may provide an adapter, or use the shared environment configuration.
    """
    default_suite = cases_path is None
    if (
        not isinstance(validation_retries, int)
        or isinstance(validation_retries, bool)
        or validation_retries < 0
    ):
        raise PromptReleaseError("validation_retries must be a nonnegative integer")
    cases_path = release.project_root / DEFAULT_SUITE if cases_path is None else Path(cases_path)
    registered = release.manifest["nodes"]
    selected = set(registered) if nodes is None else set(nodes)
    if not selected or selected - set(registered):
        raise PromptReleaseError("Evaluation selected empty or unknown prompt nodes")
    cases = read_cases(cases_path)
    for case in cases:
        validate_case(case, registered)
        release.render(case["node"], case["variables"])
    cases = [case for case in cases if case["node"] in selected]
    covered = {case["node"] for case in cases}
    if not cases:
        raise PromptReleaseError("No evaluation cases match the selected nodes")
    if (default_suite or require_coverage) and selected - covered:
        raise PromptReleaseError(
            "Missing evaluation cases for: " + ", ".join(sorted(selected - covered))
        )
    config = llm_config or (llm.config if llm is not None else LLMConfig.from_env())
    owned = llm is None
    client = llm or OpenAILLM(config)
    model = config.model
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / f"{run_id}-{release.version[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    input_file = run_dir / "cases.jsonl"
    results_file = run_dir / "results.jsonl"
    shutil_copy = cases_path.read_bytes()
    input_file.write_bytes(shutil_copy)

    total = passed = first_pass = 0
    counts = {
        node: {"case_count": 0, "structural_pass_count": 0, "source_sha256": entry["sha256"]}
        for node, entry in registered.items()
    }
    try:
        with results_file.open("w", encoding="utf-8", newline="") as destination:
            for case in cases:
                total += 1
                node_id = case["node"]
                counts[node_id]["case_count"] += 1
                record: dict[str, Any]
                attempts = []
                try:
                    variables = case["variables"]
                    prompt = release.render(node_id, variables)
                    correction = ""
                    for _ in range(validation_retries + 1):
                        raw = (
                            client.complete(prompt, correction=correction)
                            if correction
                            else client.complete(prompt)
                        )
                        valid, note = validate_output(
                            raw,
                            release.manifest["nodes"][node_id]["output_contract"],
                            variables,
                            node_id,
                        )
                        attempts.append(
                            {
                                "raw_output": raw,
                                "request_id": getattr(client, "last_request_id", None),
                                "structural_check": {"passed": valid, "note": note},
                            }
                        )
                        if valid:
                            break
                        correction = note
                    passed += int(valid)
                    first_pass += int(attempts[0]["structural_check"]["passed"])
                    counts[node_id]["structural_pass_count"] += int(valid)
                    record = {
                        "line": case["_line"],
                        "id": case["id"],
                        "node": node_id,
                        "release": release.version,
                        "model": model,
                        "api": config.api,
                        "request_id": getattr(client, "last_request_id", None),
                        "raw_output": raw,
                        "attempts": attempts,
                        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        "structural_check": {"passed": valid, "note": note},
                    }
                except (
                    Exception
                ) as exc:  # Preserve failed cases to support training review and reruns.
                    message = str(exc)
                    secret = os.getenv(config.api_key_env)
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
                    if getattr(config, "base_url", None):
                        message = message.replace(str(config.base_url), "[ENDPOINT]")
                    record = {
                        "line": case["_line"],
                        "id": case["id"],
                        "node": node_id,
                        "release": release.version,
                        "error": f"{type(exc).__name__}: {message}",
                        "attempts": attempts,
                    }
                destination.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if owned:
            client.close()

    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "release": release.version,
        "model": model,
        "api": config.api,
        "case_count": total,
        "structural_pass_count": passed,
        "first_attempt_pass_count": first_pass,
        "validation_retries": validation_retries,
        "selected_nodes": sorted(selected),
        "evaluated_nodes": sorted(covered),
        "missing_nodes": sorted(set(registered) - covered),
        "coverage_complete": covered == set(registered),
        "nodes": counts,
        "cases_sha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
        "results_sha256": hashlib.sha256(results_file.read_bytes()).hexdigest(),
        "assessment": "Structural checks only; semantic and media quality require review.",
    }
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline=""
    )
    return run_dir


def verify_evaluation(release: PromptRelease, path: Path) -> dict[str, Any]:
    """Reject incomplete, failed, stale or edited reports before activation."""
    report_path = path / "run.json" if path.is_dir() else path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("schema_version") != 1 or report.get("release") != release.version:
            raise PromptReleaseError("Evaluation report does not match the candidate version")
        directory = report_path.parent
        source = directory / "cases.jsonl"
        result = directory / "results.jsonl"
        for key, file in (("cases_sha256", source), ("results_sha256", result)):
            if not file.resolve().is_relative_to(directory.resolve()):
                raise PromptReleaseError("Evaluation evidence escapes its report directory")
            if hashlib.sha256(file.read_bytes()).hexdigest() != report.get(key):
                raise PromptReleaseError("Evaluation inputs or results changed after recording")
        cases = {case["_line"]: case for case in read_cases(source)}
        records = [json.loads(line) for line in result.read_text("utf-8").splitlines() if line]
        if not records or len(records) != report.get("case_count"):
            raise PromptReleaseError("Evaluation report has incomplete results")
        registered = release.manifest["nodes"]
        if {case["node"] for case in cases.values()} != set(registered):
            raise PromptReleaseError("Publishing with an evaluation requires all registered nodes")
        actual_cases = {case["id"]: case for case in cases.values()}
        for required in read_cases(release.project_root / DEFAULT_SUITE):
            actual = actual_cases.get(required["id"])
            if actual is None or any(actual[key] != required[key] for key in ("node", "variables")):
                raise PromptReleaseError("Evaluation must include the unchanged default suite")
        retries = report.get("validation_retries")
        if type(retries) is not int or retries < 0:
            raise PromptReleaseError("Evaluation retry policy is invalid")
        counts = {
            node: {"case_count": 0, "structural_pass_count": 0, "source_sha256": entry["sha256"]}
            for node, entry in registered.items()
        }
        first_pass = 0
        covered = set()
        seen = set()
        for record in records:
            line = record["line"]
            if line in seen or line not in cases or record.get("error"):
                raise PromptReleaseError("Evaluation contains duplicate, missing or failed cases")
            seen.add(line)
            case = cases[line]
            node = case["node"]
            validate_case(case, release.manifest["nodes"])
            if (
                record.get("node") != node
                or record.get("id") != case["id"]
                or record.get("release") != release.version
                or record.get("model") != report.get("model")
                or record.get("api") != report.get("api")
            ):
                raise PromptReleaseError("Evaluation case belongs to another node or version")
            prompt = release.render(node, case["variables"])
            if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != record.get("prompt_sha256"):
                raise PromptReleaseError("Evaluation prompt no longer matches its candidate")
            attempts = record.get("attempts")
            if not isinstance(attempts, list) or not 1 <= len(attempts) <= retries + 1:
                raise PromptReleaseError("Evaluation attempt history is invalid")
            for index, attempt in enumerate(attempts):
                raw = attempt["raw_output"]
                if not isinstance(raw, str):
                    raise PromptReleaseError("Evaluation output must be text")
                valid, _ = validate_output(
                    raw, registered[node]["output_contract"], case["variables"], node
                )
                if attempt.get("structural_check", {}).get("passed") is not valid:
                    raise PromptReleaseError("Evaluation attempt check is inconsistent")
                if index == 0:
                    first_pass += int(valid)
                if valid and index != len(attempts) - 1:
                    raise PromptReleaseError("Evaluation retries continued after a valid output")
            final = attempts[-1]
            if (
                record.get("raw_output") != final["raw_output"]
                or record.get("structural_check") != final.get("structural_check")
                or record.get("request_id") != final.get("request_id")
            ):
                raise PromptReleaseError("Evaluation final result differs from its last attempt")
            if not valid:
                raise PromptReleaseError("Evaluation contains a rejected output")
            covered.add(node)
            counts[node]["case_count"] += 1
            counts[node]["structural_pass_count"] += 1
        if covered != set(registered) or seen != set(cases):
            raise PromptReleaseError("Publishing with an evaluation requires all registered nodes")
        expected = {
            "case_count": len(records),
            "structural_pass_count": len(records),
            "first_attempt_pass_count": first_pass,
            "selected_nodes": sorted(registered),
            "evaluated_nodes": sorted(covered),
            "missing_nodes": [],
            "coverage_complete": True,
            "nodes": counts,
        }
        for key, value in expected.items():
            if report.get(key) != value or type(report.get(key)) is not type(value):
                raise PromptReleaseError(f"Evaluation summary is inconsistent: {key}")
        return report
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, PromptReleaseError):
            raise
        raise PromptReleaseError("Invalid or incomplete evaluation report") from exc
