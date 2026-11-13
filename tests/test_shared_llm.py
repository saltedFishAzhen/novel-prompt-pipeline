from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm import LLMConfig, OpenAILLM
from pipeline.llm import OpenAILLM as CompatibilityLLM
from prompt_project import PromptRelease
from prompt_project.evaluate import run_evaluation, validate_output


def test_pipeline_and_evaluation_share_one_llm_adapter():
    assert CompatibilityLLM is OpenAILLM


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_evaluation_uses_configured_adapter_for_both_api_modes(tmp_path, api):
    calls = []
    raw = json.dumps({"story": "A factual story.", "chapter": "A factual chapter."})

    def create(**kwargs):
        calls.append(kwargs)
        if api == "responses":
            return SimpleNamespace(
                status="completed", output_text=raw, _request_id="response-request"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=raw, refusal=None)
                )
            ],
            _request_id="chat-request",
        )

    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create),
    )
    config = LLMConfig(
        model="shared-config-model",
        api=api,
        timeout=37,
        max_retries=1,
        extra={"max_output_tokens": 300} if api == "responses" else {"max_completion_tokens": 300},
    )
    adapter = OpenAILLM(config, client=sdk)
    release = PromptRelease.load(Path(__file__).resolve().parents[1] / "prompts")
    variables = {
        name: "The provided text."
        for name in release.manifest["nodes"]["context_summary"]["variables"]
    }
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps({"node": "context_summary", "variables": variables}), encoding="utf-8"
    )
    result = run_evaluation(release, cases, tmp_path / "out", llm=adapter, llm_config=config)
    assert calls[0]["model"] == "shared-config-model"
    assert calls[0][next(iter(config.extra))] == 300
    summary = json.loads((result / "run.json").read_text(encoding="utf-8"))
    assert summary["api"] == api and summary["structural_pass_count"] == 1


def test_evaluation_rejects_duplicate_json_keys_like_production():
    valid, _ = validate_output(
        '{"story":"first","story":"second","chapter":"one"}',
        {"format": "json_object", "required_fields": ["story", "chapter"]},
    )
    assert not valid


@pytest.mark.parametrize("mode", ["character", "scene", "prop"])
def test_evaluation_rejects_empty_asset_aliases_like_production(mode):
    valid, message = validate_output(
        json.dumps([{"name": "Asset", "aliases": "", "description": "A visual asset."}]),
        {"format": "json_asset_array"},
        {"小说原文": "Asset"},
        node_id=f"asset.{mode}",
    )
    assert not valid
    assert "nonempty" in message


@pytest.mark.parametrize(
    "selection",
    [
        {"asset_ids": ["a", "a"], "first_frame_asset_ids": []},
        {"asset_ids": ["a"], "first_frame_asset_ids": ["a", "a"]},
        {"asset_ids": ["a"], "first_frame_asset_ids": ["b"]},
    ],
)
def test_evaluation_rejects_invalid_binding_without_candidate_context(selection):
    valid, _ = validate_output(
        json.dumps(selection),
        {"format": "json_object", "required_fields": ["asset_ids", "first_frame_asset_ids"]},
    )
    assert not valid


def test_legacy_prompt_cli_delegates_configuration(monkeypatch):
    from pipeline import cli
    from prompt_project.__main__ import main

    received = []
    monkeypatch.setattr(cli, "main", lambda args: received.append(args) or 0)
    assert main(["--config", "configs/custom.json", "evaluate", "cases.jsonl"]) == 0
    assert received == [["prompts", "--config", "configs/custom.json", "evaluate", "cases.jsonl"]]


def test_evaluation_rejects_cut_beyond_duration_like_production():
    from pipeline import protocols

    video = "\n".join(
        [
            "subject_definitions:",
            "<Picture 1> is a reference.",
            "summary:",
            "A room.",
            "retention_analysis:",
            "The same room.",
            "detailed_description:",
            "[Shot 1] The room. [Shot 2] At 00:10.000 cut to the door.",
            "overall_soundscape:",
            "N/A",
            "non_diegetic_music:",
            "N/A",
            "\u65f6\u957f\uff1a6\u79d2",
        ]
    )
    record = "0" + protocols.FIELD + "A room." + protocols.FIELD + video
    valid, _ = validate_output(
        record, {"format": "record_delimited_text", "record_schema": "storyboard"}
    )
    assert not valid


def test_evaluation_redacts_configured_key_on_provider_error(tmp_path, monkeypatch):
    config = LLMConfig(model="shared", api_key_env="TEST_EVALUATION_KEY")
    monkeypatch.setenv("TEST_EVALUATION_KEY", "private-secret-value")

    class BrokenLLM:
        def complete(self, prompt):
            raise RuntimeError("provider echoed private-secret-value")

    release = PromptRelease.load(Path(__file__).resolve().parents[1] / "prompts")
    variables = {
        name: "Input." for name in release.manifest["nodes"]["context_summary"]["variables"]
    }
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps({"node": "context_summary", "variables": variables}), encoding="utf-8"
    )
    result = run_evaluation(release, cases, tmp_path / "out", llm=BrokenLLM(), llm_config=config)
    text = (result / "results.jsonl").read_text(encoding="utf-8")
    assert "private-secret-value" not in text and "[REDACTED]" in text
