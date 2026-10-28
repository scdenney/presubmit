"""The Claude client: server tools, paused turns, and no sampling parameters."""

from __future__ import annotations

from types import SimpleNamespace

from presubmit import core


def _resp(stop_reason, *texts):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=t) for t in texts],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _FakeClient:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _patch(monkeypatch, responses):
    client = _FakeClient(responses)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(core.anthropic, "Anthropic", lambda **kw: client)
    return client


def test_search_attaches_web_search_and_resumes_paused_turn(monkeypatch):
    client = _patch(monkeypatch, [_resp("pause_turn", "Searching. "), _resp("end_turn", '{"found": true}')])
    out = core.call_claude("Find this reference.", model_type="forensic", use_search=True,
                           temperature=0.0, step="reference_grounded")
    assert out == 'Searching. {"found": true}'
    assert len(client.calls) == 2
    first = client.calls[0]
    assert first["tools"][0]["name"] == "web_search"
    assert first["tools"][0]["type"] == "web_search_20260209"  # Sonnet 5 takes the dynamic variant
    assert "temperature" not in first
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_haiku_gets_basic_search_and_code_exec_by_env(monkeypatch):
    client = _patch(monkeypatch, [_resp("end_turn", "ok")])
    core.call_claude("x", model_type="mechanical", use_search=True, step="s")
    assert client.calls[0]["tools"][0]["type"] == "web_search_20250305"

    client = _patch(monkeypatch, [_resp("end_turn", "ok")])
    monkeypatch.setenv("CODE_EXEC_STEPS", "01fe_math_blue")
    core.call_claude("x", model_type="synthesis", step="01fe_math_blue")
    assert client.calls[0]["tools"] == [core._CODE_EXEC_TOOL]


def test_plain_call_sends_no_tools(monkeypatch):
    client = _patch(monkeypatch, [_resp("end_turn", "ok")])
    core.call_claude("x", model_type="adversarial", thinking_level="high", step="s")
    call = client.calls[0]
    assert "tools" not in call and "temperature" not in call
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "high"}
