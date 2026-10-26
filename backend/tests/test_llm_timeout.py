from types import SimpleNamespace

import pytest

from app.utils import llm_client


def test_explicit_timeout_overrides_the_configured_model_bound(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "900")
    monkeypatch.setattr(llm_client, "OpenAI", lambda **kwargs: SimpleNamespace(**kwargs))
    configured = llm_client.LLMClient(api_key="synthetic", base_url="http://127.0.0.1/v1")
    explicit = llm_client.LLMClient(api_key="synthetic", base_url="http://127.0.0.1/v1", timeout=20)
    assert configured.client.timeout == 900
    assert configured.client.max_retries == 0
    assert explicit.client.timeout == 20


@pytest.mark.parametrize("bound", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_cannot_start_a_provider_request(monkeypatch, bound):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", bound)
    with pytest.raises(ValueError, match="finite positive"):
        llm_client.LLMClient(api_key="synthetic", base_url="http://127.0.0.1/v1")
