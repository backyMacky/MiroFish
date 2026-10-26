"""Canonical whole-hour schedules without guessing ranges or rewriting source files."""

from copy import deepcopy
import math
import re


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
