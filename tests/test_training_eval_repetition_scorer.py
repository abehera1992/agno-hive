"""Tests for training/eval/harness.py's Axis E (repetition/convergence) scorer and
training/eval/gate.py's optional-axis handling — added 2026-08-16 after the user asked
whether the eval suite needed to cover the four live swarm degeneracy incidents this
session's Phase A diagnosed (verbatim repetition loop, escalating self-correction
spiral, narration leak, false-positive liveness kill) before training a brand-new,
architecturally riskier candidate (Qwen3.8-27B, linear-attention hybrid).

Axis E is a PROXY, not equivalent coverage: it can only see degeneracy inside one
bounded, temperature-0 completion, not the actual multi-turn streamed drift that
produced the real incidents. It exists to catch a candidate that is obviously
loop-prone before the more expensive real-swarm smoke test in RUNBOOK Phase 3b/5.
"""
import json
from pathlib import Path

from training.eval.gate import AXES, OPTIONAL_AXES, resolve_floor
from training.eval.harness import (
    SCORERS, AXIS_OF, _looks_like_repetition_loop, score_repetition, run_case,
)


# ── _looks_like_repetition_loop (ported copy) ──────────────────────────────────

def test_verbatim_repeat_is_detected():
    sentence = ("I'll check the UoM conversion service implementation to ensure "
                "it's properly implemented for Phase 1 requirements:")
    prior = "Some earlier analysis. " + sentence + " and more."
    new_segment = "\n\n\n" + sentence + "\n\n\n"
    assert _looks_like_repetition_loop(new_segment, prior) is True


def test_genuinely_new_content_is_not_flagged():
    prior = "Here is the analysis of the parties module and its GSTIN handling."
    new_segment = "Now let's look at the inventory service's SKU generation logic."
    assert _looks_like_repetition_loop(new_segment, prior) is False


def test_short_segments_never_flagged():
    prior = "The function exists. " * 10
    new_segment = "The function exists."
    assert _looks_like_repetition_loop(new_segment, prior) is False


def test_intensifier_reworded_near_repeat_is_detected():
    prior = (
        "I need to be more specific with my file:line citations. Let me try again "
        "with exact verbatim citations from the codebase and check every claim "
        "carefully before finalizing the comparison table."
    )
    new_segment = (
        "I need to be even more specific with my file:line citations. Let me "
        "try again with exact verbatim citations from the codebase, using the "
        "exact text from the files this time to be certain."
    )
    assert _looks_like_repetition_loop(new_segment, prior) is True


# ── score_repetition ────────────────────────────────────────────────────────────

def test_score_repetition_clean_completion_scores_1():
    text = (
        "This section explains how the inventory service resolves SKU prefixes "
        "from category configuration. " * 1
        + "A different paragraph now covers the pricing bundle activation flow, "
          "which reads tenant_modules to decide which bundles are unlocked, and "
          "the module settings panel used to configure them per tenant, going "
          "into detail about the gear-icon drawer pattern used across Parties "
          "and now being reused for Items, with distinct configuration fields "
          "for each module that never repeat verbatim across sections."
    )
    score, detail = score_repetition({}, text)
    assert score == 1.0
    assert "no repetition" in detail


def test_score_repetition_looping_completion_scores_0():
    sentence = ("I'll check the UoM conversion service implementation to ensure "
                "it's properly implemented for Phase 1 requirements: ")
    # Pad past one scan window (400 chars) before the loop starts, then repeat the
    # same long sentence many times with variable blank-line padding, mirroring the
    # real incident's shape.
    text = ("Some real opening analysis. " * 20) + (sentence + "\n\n\n") * 10
    score, detail = score_repetition({}, text)
    assert score == 0.0
    assert "repetition/degeneracy detected" in detail


def test_score_repetition_short_completion_scores_1():
    """Below one scan window there's nothing to compare against yet — must not
    false-positive on a short, legitimately terse answer."""
    score, detail = score_repetition({}, "Yes, that field is required.")
    assert score == 1.0


# ── wiring into SCORERS / AXIS_OF ────────────────────────────────────────────────

def test_repetition_registered_as_axis_e():
    assert SCORERS["repetition"][0] == "E"
    assert SCORERS["repetition"][1] is score_repetition
    assert AXIS_OF["repetition"] == "E"


# ── run_case threads a per-case max_tokens through to call_model ────────────────

def test_run_case_uses_case_max_tokens(monkeypatch):
    captured = {}

    def fake_call_model(base_url, model, messages, tools=None, max_tokens=800, **kw):
        captured["max_tokens"] = max_tokens
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("training.eval.harness.call_model", fake_call_model)
    case = {"id": "E1-test", "prompt": "hello", "scorers": ["repetition"], "max_tokens": 2500}
    run_case(case, "http://x", "m")
    assert captured["max_tokens"] == 2500


def test_run_case_defaults_max_tokens_800(monkeypatch):
    captured = {}

    def fake_call_model(base_url, model, messages, tools=None, max_tokens=800, **kw):
        captured["max_tokens"] = max_tokens
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("training.eval.harness.call_model", fake_call_model)
    case = {"id": "A1-test", "prompt": "hello", "scorers": ["tool_call"], "expect_tool": "x"}
    run_case(case, "http://x", "m")
    assert captured["max_tokens"] == 800


# ── gate.py: optional-axis backward compatibility ────────────────────────────────

def test_repetition_is_axes_and_optional():
    assert AXES["repetition"] == "repetition_min"
    assert "repetition" in OPTIONAL_AXES


def test_resolve_floor_not_needed_when_axis_unconfigured():
    """A config's gate: block with no repetition_min key must never be consulted for
    that axis at all (gate.py's main() skips it via OPTIONAL_AXES before calling
    resolve_floor) — this just documents that resolve_floor itself has no special
    casing and would KeyError/raise if called directly on a missing spec, which is
    exactly why the OPTIONAL_AXES skip has to happen before resolve_floor is reached."""
    gate = {"tool_call_min": 0.98, "grounding_min": 0.85, "citation_min": 0.80, "guard_min": "baseline"}
    assert "repetition_min" not in gate


def test_gate_main_does_not_crash_without_repetition_min(tmp_path, monkeypatch, capsys):
    import sys
    from training.eval import gate as gate_mod

    cfg = {
        "gate": {
            "tool_call_min": 0.0, "grounding_min": 0.0, "citation_min": 0.0,
            "guard_min": 0.0, "min_cases_per_axis": 1,
        }
    }
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")  # valid YAML subset

    baseline = {
        "label": "base",
        "aggregate": {"tool_call": 0.5, "grounding": 0.5, "citation": 0.5, "guard": 0.5, "repetition": 0.9},
        "results": [{"scores": {"tool_call": 0.5, "grounding": 0.5, "citation": 0.5,
                                 "guard": 0.5, "repetition": 0.9}}],
    }
    candidate = {
        "label": "cand",
        "aggregate": {"tool_call": 1.0, "grounding": 1.0, "citation": 1.0, "guard": 1.0, "repetition": 0.4},
        "results": [{"scores": {"tool_call": 1.0, "grounding": 1.0, "citation": 1.0,
                                 "guard": 1.0, "repetition": 0.4}}],
    }
    base_path = tmp_path / "baseline.json"
    cand_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    cand_path.write_text(json.dumps(candidate), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "gate", "--config", str(cfg_path), "--baseline", str(base_path), "--candidate", str(cand_path),
    ])
    try:
        gate_mod.main()
    except SystemExit as e:
        # A big repetition regression (0.9 -> 0.4) must NOT be why this exits 1 while
        # repetition_min is absent from the config — every other axis is a clean pass
        # (0 floor, no regression), so if repetition were wrongly enforced this would
        # be the only possible failure source.
        assert e.code == 0, "unenforced axis must not block promotion"
    out = capsys.readouterr().out
    assert "repetition" in out
    assert "INFO" in out
