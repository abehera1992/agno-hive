"""Tests: a run's reported failure must be the CAUSE, not the teardown artifact.

When a run dies mid-flight it tears down its MCP connections, and anyio can raise
during that teardown. Python lets an exception raised in `__aexit__` REPLACE the
in-flight one, so the worker's old `f"{type(exc).__name__}: {exc}"` reported the
symptom and discarded the diagnosis.

Measured 2026-08-20. A run_docker('logs ekamapp-postgres-1') returned 1,762,993 chars
(the postgres container's entire log), the next model call died with

    litellm.ContextWindowExceededError: This model's maximum context length is 262144
    tokens. However, you requested 4096 output tokens and your prompt contains at
    least 258049 input tokens

and what actually reached the caller was

    RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's
    current cancel scope

-- true, useless, and with the real cause nowhere in it. Confirmed from that run's own
logs that NO RunError/TeamRunError stream event was emitted, so _BackendRunError's
fail-fast path never engaged and the teardown exception was genuinely the only one to
escape.
"""
from main import _describe_failure, _is_teardown_artifact


def test_chained_real_cause_is_preferred_over_the_teardown_error():
    """The 2026-08-20 shape, when the chain survives."""
    try:
        try:
            raise ValueError(
                "litellm.ContextWindowExceededError: maximum context length is 262144 tokens"
            )
        except Exception:
            raise RuntimeError(
                "Attempted to exit a cancel scope that isn't the current tasks's "
                "current cancel scope"
            )
    except Exception as exc:
        desc = _describe_failure(exc)

    assert "ContextWindowExceededError" in desc
    assert "262144" in desc, "the actionable number must survive"
    assert "surfaced during teardown" in desc, "keep the teardown context, subordinated"


def test_unchained_teardown_error_is_labelled_not_presented_as_the_diagnosis():
    """The real cause frequently dies in a DIFFERENT task and is never chained -- an
    async generator finalizer is exactly that case. Reporting the teardown error is
    then unavoidable, but it must not read as the diagnosis."""
    try:
        raise RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )
    except Exception as exc:
        desc = _describe_failure(exc)

    assert "teardown artifact" in desc
    assert "earlier in this run's logs" in desc


def test_ordinary_failures_are_reported_exactly_as_before():
    """No behaviour change for the overwhelming majority of failures."""
    try:
        raise TimeoutError("hive-mcp did not respond")
    except Exception as exc:
        assert _describe_failure(exc) == "TimeoutError: hive-mcp did not respond"


def test_cause_inside_an_exception_group_is_found():
    """anyio wraps failures in BaseExceptionGroup, so the real error is often a GROUP
    MEMBER rather than anywhere on the __cause__/__context__ chain."""
    inner = ValueError("litellm.ContextWindowExceededError: prompt too long")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    try:
        try:
            raise group
        except BaseException:
            raise RuntimeError("Attempted to exit a cancel scope")
    except Exception as exc:
        desc = _describe_failure(exc)

    assert "ContextWindowExceededError" in desc


def test_teardown_artifact_detection_is_specific():
    """Must not swallow a genuine RuntimeError that merely happens to be a RuntimeError
    -- only the anyio unwinding shapes count."""
    assert _is_teardown_artifact(RuntimeError("Attempted to exit a cancel scope"))
    assert _is_teardown_artifact(
        BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                           [ValueError("x")])
    )
    assert not _is_teardown_artifact(RuntimeError("model returned no content"))
    assert not _is_teardown_artifact(ValueError("bad schema"))


def test_no_infinite_loop_on_a_self_referential_chain():
    """A bookkeeping helper must never be the thing that hangs a failing run."""
    a = RuntimeError("Attempted to exit a cancel scope")
    b = RuntimeError("also a cancel scope problem")
    a.__context__ = b
    b.__context__ = a

    desc = _describe_failure(a)
    assert "teardown artifact" in desc
