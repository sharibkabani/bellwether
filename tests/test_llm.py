import requests

from bellwether.signals.llm import (
    FallbackChainClient,
    LLMClient,
    OpenAICompatibleClient,
    build_client,
    extract_json,
    is_rate_limit_error,
)


def test_ollama_builds_without_key():
    client = build_client("ollama")
    assert isinstance(client, OpenAICompatibleClient)
    assert client.name == "ollama"


def test_groq_requires_key():
    assert build_client("groq", api_key="") is None          # no key -> not built
    assert build_client("groq", api_key="x") is not None


def test_groq_default_model_is_best_free():
    # The chosen "best free model" default.
    client = build_client("groq", api_key="x")
    assert client._model == "openai/gpt-oss-120b"
    assert "groq.com" in client._url


def test_provider_defaults_fill_in():
    client = build_client("openrouter", api_key="x")
    assert isinstance(client, OpenAICompatibleClient)
    # Default model/base url are applied when blank.
    assert "openrouter.ai" in client._url


def test_cerebras_is_a_keyed_free_provider():
    assert build_client("cerebras", api_key="") is None
    client = build_client("cerebras", api_key="x")
    assert "api.cerebras.ai" in client._url and client._model == "gpt-oss-120b"


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
    assert extract_json('{"a": 1}') == '{"a": 1}'


# --- fallback chain ---------------------------------------------------------

class _StubClient(LLMClient):
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc
        self.calls = 0

    def complete_json(self, system, user, schema=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result

    def complete_text(self, system, user):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def _http_429():
    resp = requests.Response()
    resp.status_code = 429
    return requests.HTTPError("429 Too Many Requests", response=resp)


def test_is_rate_limit_error_detects_429_and_messages():
    assert is_rate_limit_error(_http_429()) is True
    assert is_rate_limit_error(Exception("quota exceeded")) is True
    assert is_rate_limit_error(Exception("connection reset")) is False


def test_chain_fails_over_on_rate_limit():
    primary = _StubClient("groq", exc=_http_429())
    backup = _StubClient("cerebras", result='{"ok": 1}')
    chain = FallbackChainClient([primary, backup])
    assert chain.complete_json("s", "u") == '{"ok": 1}'
    assert primary.calls == 1 and backup.calls == 1


def test_chain_cools_down_rate_limited_provider():
    primary = _StubClient("groq", exc=_http_429())
    backup = _StubClient("cerebras", result='{"ok": 1}')
    chain = FallbackChainClient([primary, backup], cooldown_sec=999)
    chain.complete_json("s", "u")            # trips the cooldown on `primary`
    chain.complete_json("s", "u")            # primary skipped → backup only
    assert primary.calls == 1                # not retried while cooling
    assert backup.calls == 2


def test_chain_raises_only_when_all_fail():
    a = _StubClient("groq", exc=_http_429())
    b = _StubClient("cerebras", exc=Exception("down"))
    chain = FallbackChainClient([a, b])
    try:
        chain.complete_json("s", "u")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "all LLM providers failed" in str(exc)


def test_factory_builds_chain_skipping_keyless_providers(monkeypatch):
    from bellwether.config import Config, LLMConfig
    from bellwether.factory import _build_llm_client

    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("CEREBRAS_API_KEY", "c")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # no key → skipped

    cfg = Config()
    cfg.llm = LLMConfig(providers=["groq", "cerebras", "openrouter"])
    client = _build_llm_client(cfg)
    assert isinstance(client, FallbackChainClient)
    assert [c.name for c in client._clients] == ["groq", "cerebras"]


def test_factory_single_provider_when_no_chain(monkeypatch):
    from bellwether.config import Config, LLMConfig
    from bellwether.factory import _build_llm_client

    monkeypatch.setenv("GROQ_API_KEY", "g")
    cfg = Config()
    cfg.llm = LLMConfig(provider="groq")
    client = _build_llm_client(cfg)
    assert isinstance(client, OpenAICompatibleClient) and client.name == "groq"
