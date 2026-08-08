"""Tests for swarm/team.py's _cloud_provider_error_message (AGNOHive 2.3.2 graceful
degradation on cloud rate-limit/quota errors).

agno-hive talks to LiteLLM over the OpenAI protocol (agno's OpenAILike is built on
the `openai` package), so a 429 from LiteLLM's gateway -- regardless of which of the
5 cloud providers actually rejected the underlying request -- surfaces client-side as
`openai.RateLimitError`, not a `litellm.*` exception (that type only exists inside the
separately-running LiteLLM proxy process). These tests construct a real
openai.RateLimitError the same way the openai SDK itself would on an actual 429."""
import httpx
import openai

from swarm.team import _cloud_provider_error_message


def _make_rate_limit_error(message: str = "rate limited") -> openai.RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "http://litellm-host:4000/chat/completions"))
    return openai.RateLimitError(message, response=response, body=None)


def test_rate_limit_error_returns_a_clear_actionable_message():
    exc = _make_rate_limit_error("You exceeded your current quota")

    msg = _cloud_provider_error_message(exc)

    assert msg is not None
    assert "rate limit" in msg.lower() or "quota" in msg.lower()
    assert "retry" in msg.lower() or "switch" in msg.lower()


def test_rate_limit_error_message_includes_the_original_error():
    exc = _make_rate_limit_error("You exceeded your current quota")

    msg = _cloud_provider_error_message(exc)

    assert "You exceeded your current quota" in msg


def test_unrelated_exception_returns_none():
    assert _cloud_provider_error_message(ValueError("some other failure")) is None


def test_unrelated_openai_error_returns_none():
    response = httpx.Response(401, request=httpx.Request("POST", "http://litellm-host:4000/chat/completions"))
    auth_error = openai.AuthenticationError("invalid api key", response=response, body=None)

    assert _cloud_provider_error_message(auth_error) is None
