"""The real CAMEL factory receives explicit, non-replaying model budgets."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.utils.simulation_values import simulation_model_options


@pytest.mark.parametrize('name,value', [
    ('LLM_TIMEOUT_SECONDS', 'nan'), ('LLM_TIMEOUT_SECONDS', 'inf'),
    ('LLM_TIMEOUT_SECONDS', '0'), ('LLM_TIMEOUT_SECONDS', '7201'),
    ('LLM_SIMULATION_MAX_TOKENS', '255'), ('LLM_SIMULATION_MAX_TOKENS', '32769'),
])
def test_invalid_model_budgets_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        simulation_model_options()


@pytest.mark.asyncio
@pytest.mark.parametrize('filename', [
    'run_parallel_simulation.py', 'run_twitter_simulation.py', 'run_reddit_simulation.py',
])
async def test_real_cli_factory_pins_sdk_provider_timeout_retries_and_output(monkeypatch, filename):
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    for name, value in {
        'LLM_API_KEY': 'synthetic-runtime-key',
        'LLM_BASE_URL': 'http://provider.invalid/v1',
        'LLM_MODEL_NAME': 'gpt-4o-mini',
        'LLM_TIMEOUT_SECONDS': '900',
        'LLM_SIMULATION_MAX_TOKENS': '1024',
        'LANGFUSE_ENABLED': 'false',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('LLM_BOOST_API_KEY', raising=False)
    source = Path(__file__).resolve().parents[1] / 'scripts' / filename
    tree = ast.parse(source.read_text(encoding='utf-8'))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name in {'create_model', '_create_model'})
    namespace = {'os': os, 'Dict': Dict, 'Any': Any,
                 'ModelFactory': ModelFactory, 'ModelPlatformType': ModelPlatformType}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
    model = (namespace['create_model']({}) if method.name == 'create_model'
             else namespace['_create_model'](SimpleNamespace(config={})))
    try:
        assert model._client.timeout == 900
        assert model._async_client.timeout == 900
        assert model._client.max_retries == model._async_client.max_retries == 0
        assert str(model._client.base_url) == 'http://provider.invalid/v1/'
        assert model._client.api_key == 'synthetic-runtime-key'
        assert model.model_config_dict['max_tokens'] == 1024
    finally:
        model._client.close()
        await model._async_client.close()
