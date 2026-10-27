"""Validate simulation schedules and model budgets without rewriting saved inputs."""

from copy import deepcopy
import math
import os
import re


def simulation_model_options():
    """Bound OASIS/CAMEL calls using the same explicit application deadline."""
    timeout = float(os.getenv('LLM_TIMEOUT_SECONDS', '300'))
    if not math.isfinite(timeout) or not 1 <= timeout <= 7200:
        raise ValueError('Simulation model timeout must be from 1 to 7200 seconds')
    max_tokens = int(os.getenv('LLM_SIMULATION_MAX_TOKENS', '2048'))
    if not 256 <= max_tokens <= 32768:
        raise ValueError('LLM_SIMULATION_MAX_TOKENS must be from 256 to 32768')
    return {'timeout': timeout, 'max_retries': 0,
            'model_config_dict': {'max_tokens': max_tokens}}


def create_simulation_model(factory, **parameters):
    options = simulation_model_options()
    model = factory(**parameters, **options)
    # The installed CAMEL OpenAIModel omits max_retries when calling its base
    # constructor, resetting it to three. Pin both actual SDK clients as well.
    # with_options shares their HTTP transports, including any instrumentation.
    model._max_retries = 0
    model._client = model._client.with_options(timeout=options['timeout'], max_retries=0)
    model._async_client = model._async_client.with_options(timeout=options['timeout'], max_retries=0)
    return model


def normalize_hours(values):
    if not isinstance(values, list):
        raise ValueError('Simulation hours must be a list of whole hours from 0 to 23')
    hours = []
    for value in values:
        if type(value) is int:
            hour = value
        elif type(value) is float and math.isfinite(value) and value.is_integer():
            hour = int(value)
        elif isinstance(value, str) and re.fullmatch(r'(?:[01]?\d|2[0-3])(?::00)?', value.strip()):
            hour = int(value.strip().split(':')[0])
        else:
            raise ValueError('Simulation hours must be whole hours from 0 to 23; ranges and partial hours are unsupported')
        if not 0 <= hour <= 23:
            raise ValueError('Simulation hours must be from 0 to 23')
        if hour not in hours:
            hours.append(hour)
    return hours


def normalize_config_hours(config):
    if not isinstance(config, dict):
        raise ValueError('Simulation configuration must be an object')
    result = deepcopy(config)
    agents = result.get('agent_configs', [])
    if not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents):
        raise ValueError('Agent configurations must be a list of objects')
    for agent in agents:
        if 'active_hours' in agent:
            agent['active_hours'] = normalize_hours(agent['active_hours'])
    time_config = result.get('time_config', {})
    if not isinstance(time_config, dict):
        raise ValueError('Time configuration must be an object')
    for field in ('peak_hours', 'off_peak_hours', 'morning_hours', 'work_hours'):
        if field in time_config:
            time_config[field] = normalize_hours(time_config[field])
    return result
