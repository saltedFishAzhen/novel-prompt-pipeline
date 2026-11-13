from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from llm import LLMConfig
from prompt_project.compiler import build, publish, stage
from prompt_project.evaluate import run_evaluation, verify_evaluation
from prompt_project.inventory import check_contracts, coverage, read_cases
from prompt_project.release import PromptReleaseError

PROJECT = Path(__file__).resolve().parents[1] / "prompts"


def workspace(tmp_path):
    root = tmp_path / "prompts"
    for relative in ("src", "tests/fixtures", "evaluations/suites"):
        shutil.copytree(PROJECT / relative, root / relative)
    return root


class FixtureLLM:
    def __init__(self, release):
        self.calls = []
        self.responses = {
            release.render(case["node"], case["variables"]): case["valid_output"]
            for case in read_cases(release.project_root / "tests/fixtures/node_cases.jsonl")
        }

    def complete(self, prompt, *, correction=""):
        self.calls.append((prompt, correction))
        return self.responses[prompt]


def evaluate_all(release, path):
    client = FixtureLLM(release)
    result = run_evaluation(
        release, None, path, llm=client, llm_config=LLMConfig(model="offline-fixture")
    )
    return result, client


def test_every_registered_node_has_a_valid_positive_negative_and_default_case():
    report = coverage(PROJECT)
    assert report["node_count"] == 16
    assert not report["missing_default_cases"] and not report["missing_contract_cases"]
    assert all(
        item["default_case_count"] and item["contract_case_count"]
        for item in report["nodes"].values()
    )
    assert check_contracts(PROJECT) == []


def test_new_registered_node_cannot_bypass_the_same_publication_gate(tmp_path):
    root = workspace(tmp_path)
    old = publish(root=root, offline=True)
    pointer = (root / "current.json").read_bytes()
    registry = root / "src/nodes/index.json"
    index = json.loads(registry.read_text("utf-8"))
    index["nodes"]["additional_skill_node"] = {
        "file": "additional.md",
        "variables": [],
        "output_contract": {"format": "single_line_text"},
    }
    registry.write_text(json.dumps(index), encoding="utf-8")
    (root / "src/nodes/additional.md").write_text("Return one line.", encoding="utf-8")
    with pytest.raises(PromptReleaseError, match="additional_skill_node"):
        publish(root=root, offline=True)
    assert (root / "current.json").read_bytes() == pointer
    assert old.version == json.loads(pointer)["version"]


def test_contract_negative_which_passes_prevents_publication(tmp_path):
    root = workspace(tmp_path)
    cases = read_cases(root / "tests/fixtures/node_cases.jsonl")
    cases[0]["invalid_outputs"] = [cases[0]["valid_output"]]
    (root / "tests/fixtures/node_cases.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8"
    )
    with pytest.raises(PromptReleaseError, match="incorrectly accepted"):
        publish(root=root, offline=True)
    assert not (root / "current.json").exists()


def test_candidate_evaluation_and_guarded_activation_are_separate(tmp_path):
    root = workspace(tmp_path)
    publish(root=root, offline=True)
    pointer = (root / "current.json").read_bytes()
    source = root / "src/nodes/context_summary.md"
    source.write_text(source.read_text("utf-8") + "\nKeep the summary concise.\n", encoding="utf-8")
    candidate = stage(root=root)
    assert (root / "current.json").read_bytes() == pointer
    run, client = evaluate_all(candidate, tmp_path / "evaluation")
    report = verify_evaluation(candidate, run)
    assert len(client.calls) == 16
    assert report["coverage_complete"] and not report["missing_nodes"]
    assert report["structural_pass_count"] == 16 and report["first_attempt_pass_count"] == 16
    active = publish(root=root, evaluation=run)
    assert active.version == candidate.version
    assert json.loads((root / "current.json").read_text("utf-8"))["version"] == candidate.version


def test_partial_evaluation_is_visible_and_cannot_be_used_as_complete_approval(tmp_path):
    root = workspace(tmp_path)
    release = publish(root=root, offline=True)
    run = run_evaluation(
        release,
        None,
        tmp_path / "evaluation",
        nodes=["context_summary"],
        llm=FixtureLLM(release),
        llm_config=LLMConfig(model="offline-fixture"),
    )
    report = json.loads((run / "run.json").read_text("utf-8"))
    assert report["case_count"] == 1 and len(report["missing_nodes"]) == 15
    with pytest.raises(PromptReleaseError, match="all registered nodes"):
        publish(root=root, evaluation=run)


def test_modified_results_or_changed_source_cannot_activate_a_candidate(tmp_path):
    root = workspace(tmp_path)
    release = publish(root=root, offline=True)
    run, _ = evaluate_all(release, tmp_path / "evaluation")
    result = run / "results.jsonl"
    result.write_bytes(result.read_bytes() + b"\n")
    with pytest.raises(PromptReleaseError, match="changed"):
        verify_evaluation(release, run)
    fresh, _ = evaluate_all(release, tmp_path / "fresh")
    pointer = (root / "current.json").read_bytes()
    source = root / "src/nodes/context_summary.md"
    source.write_text(source.read_text("utf-8") + "\nAnother revision.\n", encoding="utf-8")
    with pytest.raises(PromptReleaseError, match="candidate version"):
        publish(root=root, evaluation=fresh)
    assert (root / "current.json").read_bytes() == pointer


def test_validation_retries_keep_initial_failure_evidence(tmp_path):
    release = stage(root=workspace(tmp_path))

    class Correctable(FixtureLLM):
        def complete(self, prompt, *, correction=""):
            if not correction:
                self.calls.append((prompt, correction))
                return "not JSON"
            return super().complete(prompt, correction=correction)

    client = Correctable(release)
    run = run_evaluation(
        release,
        None,
        tmp_path / "evaluation",
        nodes=["context_summary"],
        llm=client,
        llm_config=LLMConfig(model="offline-fixture"),
        validation_retries=1,
    )
    record = json.loads((run / "results.jsonl").read_text("utf-8"))
    assert len(record["attempts"]) == 2
    assert not record["attempts"][0]["structural_check"]["passed"]
    assert record["attempts"][1]["structural_check"]["passed"]
    report = json.loads((run / "run.json").read_text("utf-8"))
    assert report["first_attempt_pass_count"] == 0 and report["structural_pass_count"] == 1


def test_missing_suite_coverage_fails_before_any_model_call(tmp_path):
    release = stage(root=workspace(tmp_path))
    case = read_cases(release.project_root / "evaluations/suites/default.jsonl")[0]
    partial = tmp_path / "partial.jsonl"
    partial.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    client = FixtureLLM(release)
    with pytest.raises(PromptReleaseError, match="Missing evaluation cases"):
        run_evaluation(
            release,
            partial,
            tmp_path / "evaluation",
            llm=client,
            llm_config=LLMConfig(model="offline-fixture"),
            require_coverage=True,
        )
    assert not client.calls
    assert build(root=release.project_root, check=True) == []


def edit_report(run, **updates):
    path = run / "run.json"
    report = json.loads(path.read_text("utf-8"))
    report.update(updates)
    path.write_text(json.dumps(report), encoding="utf-8")


def test_full_node_coverage_cannot_replace_the_authoritative_cases(tmp_path):
    release = stage(root=workspace(tmp_path))
    cases = read_cases(release.project_root / "evaluations/suites/default.jsonl")
    for case in cases:
        case["id"] += "-easy-substitute"
    custom = tmp_path / "easy.jsonl"
    custom.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    run = run_evaluation(
        release,
        custom,
        tmp_path / "evaluation",
        llm=FixtureLLM(release),
        llm_config=LLMConfig(model="offline-fixture"),
    )
    with pytest.raises(PromptReleaseError, match="unchanged default suite"):
        verify_evaluation(release, run)


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_attempt_pass_count", 0),
        ("nodes", {}),
        ("selected_nodes", []),
        ("coverage_complete", False),
        ("missing_nodes", ["context_summary"]),
    ],
)
def test_report_statistics_are_recomputed(tmp_path, field, value):
    release = stage(root=workspace(tmp_path))
    run, _ = evaluate_all(release, tmp_path / "evaluation")
    edit_report(run, **{field: value})
    with pytest.raises(PromptReleaseError, match="summary is inconsistent"):
        verify_evaluation(release, run)


@pytest.mark.parametrize("tamper", ["check", "continued", "final", "missing"])
def test_attempt_history_cannot_disagree_with_the_results(tmp_path, tamper):
    release = stage(root=workspace(tmp_path))
    run, _ = evaluate_all(release, tmp_path / "evaluation")
    path = run / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    record = rows[0]
    if tamper == "check":
        record["attempts"][0]["structural_check"]["passed"] = False
    elif tamper == "continued":
        record["attempts"].append(record["attempts"][0].copy())
        edit_report(run, validation_retries=1)
    elif tamper == "final":
        record["raw_output"] = "different output"
    else:
        record["attempts"] = []
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    edit_report(run, results_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(PromptReleaseError, match="attempt|retries|final"):
        verify_evaluation(release, run)


def test_provider_error_keeps_preceding_failed_attempts(tmp_path):
    release = stage(root=workspace(tmp_path))

    class FailingRetry(FixtureLLM):
        def complete(self, prompt, *, correction=""):
            if correction:
                raise RuntimeError("provider unavailable")
            return "not JSON"

    run = run_evaluation(
        release,
        None,
        tmp_path / "evaluation",
        nodes=["context_summary"],
        llm=FailingRetry(release),
        llm_config=LLMConfig(model="offline-fixture"),
        validation_retries=1,
    )
    record = json.loads((run / "results.jsonl").read_text("utf-8"))
    assert "provider unavailable" in record["error"]
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["raw_output"] == "not JSON"
