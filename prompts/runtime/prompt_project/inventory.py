"""One inventory and offline contract gate for every registered prompt node."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .asset_compiler import safe_path
from .contracts import validate_output
from .release import PromptReleaseError

DEFAULT_SUITE = "evaluations/suites/default.jsonl"
CONTRACT_CASES = "tests/fixtures/node_cases.jsonl"


def inventory(root: str | Path) -> dict[str, dict[str, Any]]:
    from .compiler import _node_sources, _read_index

    root = Path(root).resolve()
    index = _read_index(root)
    _node_sources(index, root)
    nodes = {
        name: {
            "source": [f"src/nodes/{entry['file']}"],
            "compiled": f"compiled/{name}.txt",
            "variables": entry["variables"],
            "output_contract": entry["output_contract"],
        }
        for name, entry in index["nodes"].items()
    }
    for mode in ("character", "scene", "prop"):
        settings = json.loads(safe_path(root, "src", "modes", f"{mode}.json").read_text("utf-8"))
        ratio = settings.get("image_aspect_ratio")
        nodes[f"asset.{mode}"] = {
            "source": ["src/common.json", f"src/modes/{mode}.json"],
            "compiled": f"compiled/asset.{mode}.txt",
            "variables": ["小说原文", "推文文案", "故事情节"],
            "output_contract": {
                "format": "json_asset_array",
                "entry_fields": ["name", "aliases", "description"],
                "description": "JSON array of scene, character, or prop asset cards.",
                **({"image_aspect_ratio": ratio} if ratio else {}),
            },
        }
    return nodes


def read_cases(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PromptReleaseError(f"Cannot read prompt cases: {path}") from exc
    cases = []
    ids = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PromptReleaseError(f"Invalid prompt case JSON at line {number}") from exc
        if not isinstance(case, dict):
            raise PromptReleaseError(f"Prompt case at line {number} must be an object")
        identity = case.get("id", f"line-{number}")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise PromptReleaseError(f"Empty or duplicate prompt case ID at line {number}")
        ids.add(identity)
        cases.append(dict(case, id=identity, _line=number))
    if not cases:
        raise PromptReleaseError("Prompt case suite is empty")
    return cases


def validate_case(case: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> None:
    name = case.get("node")
    if not isinstance(name, str) or name not in nodes:
        raise PromptReleaseError(f"Unknown prompt node in case {case.get('id')}: {name}")
    variables = case.get("variables")
    if not isinstance(variables, dict) or set(variables) != set(nodes[name]["variables"]):
        raise PromptReleaseError(f"Case {case.get('id')} variables do not match node {name}")


def coverage(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    nodes = inventory(root)
    suites = read_cases(safe_path(root, DEFAULT_SUITE))
    fixtures = read_cases(safe_path(root, CONTRACT_CASES))
    for case in suites + fixtures:
        validate_case(case, nodes)
    live = Counter(case["node"] for case in suites)
    offline = Counter(case["node"] for case in fixtures)
    return {
        "node_count": len(nodes),
        "default_suite": DEFAULT_SUITE,
        "contract_cases": CONTRACT_CASES,
        "nodes": {
            name: {
                **entry,
                "default_case_count": live[name],
                "contract_case_count": offline[name],
            }
            for name, entry in nodes.items()
        },
        "missing_default_cases": sorted(set(nodes) - set(live)),
        "missing_contract_cases": sorted(set(nodes) - set(offline)),
    }


def check_contracts(root: str | Path) -> list[str]:
    """Validate coverage and positive/negative data without invoking an LLM."""
    root = Path(root).resolve()
    try:
        report = coverage(root)
        errors = []
        for field in ("missing_default_cases", "missing_contract_cases"):
            if report[field]:
                errors.append(f"{field}: {', '.join(report[field])}")
        for case in read_cases(safe_path(root, CONTRACT_CASES)):
            name = case["node"]
            contract = report["nodes"][name]["output_contract"]
            positive = case.get("valid_output")
            negatives = case.get("invalid_outputs")
            if not isinstance(positive, str) or not isinstance(negatives, list) or not negatives:
                errors.append(f"{case['id']}: valid_output and nonempty invalid_outputs required")
                continue
            valid, note = validate_output(positive, contract, case["variables"], name)
            if not valid:
                errors.append(f"{case['id']}: positive output rejected: {note}")
            for number, negative in enumerate(negatives, 1):
                if not isinstance(negative, str):
                    errors.append(f"{case['id']}: negative {number} is not a string")
                elif validate_output(negative, contract, case["variables"], name)[0]:
                    errors.append(f"{case['id']}: negative {number} incorrectly accepted")
        return errors
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return [f"Unified prompt contract gate: {exc}"]
