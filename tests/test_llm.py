from bellwether.signals.llm import (
    OpenAICompatibleClient,
    build_client,
    extract_json,
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


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
    assert extract_json('{"a": 1}') == '{"a": 1}'
