import ast
import json
from pathlib import Path
import random
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.config import Config
from app.services.simulation_config_generator import AgentActivityConfig, TimeSimulationConfig
from app.services.simulation_manager import SimulationManager
from app.utils.simulation_values import normalize_config_hours, normalize_hours


def configuration():
    return {
        'time_config': {'agents_per_hour_min': 1, 'agents_per_hour_max': 1,
                        'peak_hours': ['09:00'], 'peak_activity_multiplier': 1,
                        'off_peak_hours': [], 'work_hours': ['09:00', '17:00']},
        'agent_configs': [{'agent_id': 0, 'activity_level': 1, 'active_hours': ['09:00', '17:00']}],
    }


def function_from_script(filename, name):
    # Execute the production loader/scheduler without importing OASIS or starting
    # its process-wide signal handlers. Inputs and files are real test fixtures.
    path = Path(__file__).parents[1] / 'scripts' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = {'json': json, 'random': random, 'Dict': Dict, 'Any': Any, 'List': List}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def test_whole_hour_formats_are_normalized_without_expanding_ranges():
    assert normalize_hours([9.0, 9, '09:00', '17:00', '18', 19]) == [9, 17, 18, 19]
    assert normalize_hours([]) == []
    agent = AgentActivityConfig(0, 'owned', 'Avery', 'Person', active_hours=['09:00', '17:00'])
    assert agent.active_hours == [9, 17]
    assert TimeSimulationConfig(peak_hours=['18', '19']).peak_hours == [18, 19]


@pytest.mark.parametrize('hours', [None, '9', [True], [False], [24], [-1], [9.5], [float('nan')], [float('inf')], ['09:30'], ['9-17'], ['24:00']])
def test_invalid_or_ambiguous_hours_fail_instead_of_disabling_agents(hours):
    with pytest.raises(ValueError, match='hours'):
        normalize_hours(hours)


@pytest.mark.parametrize('filename,name', [
    ('run_parallel_simulation.py', 'load_config'),
    ('run_twitter_simulation.py', '_load_config'),
    ('run_reddit_simulation.py', '_load_config'),
])
def test_all_real_config_loaders_activate_the_stated_hours(tmp_path, filename, name):
    raw = configuration()
    path = tmp_path / 'simulation_config.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    original = path.read_bytes()
    loader = function_from_script(filename, name)
    argument = str(path) if name == 'load_config' else SimpleNamespace(config_path=str(path))
    loaded = loader(argument)
    assert loaded['agent_configs'][0]['active_hours'] == [9, 17]
    select = function_from_script('run_parallel_simulation.py', 'get_active_agents_for_round')
    env = SimpleNamespace(agent_graph=SimpleNamespace(get_agent=lambda identifier: f'agent-{identifier}'))
    assert select(env, loaded, 9, 9) == [(0, 'agent-0')]
    assert select(env, loaded, 10, 10) == []
    assert path.read_bytes() == original
    assert raw['agent_configs'][0]['active_hours'] == ['09:00', '17:00']


def test_canonicalization_does_not_mutate_raw_ai_data():
    raw = configuration()
    canonical = normalize_config_hours(raw)
    assert canonical['time_config']['peak_hours'] == [9]
    assert raw['time_config']['peak_hours'] == ['09:00']


@pytest.mark.parametrize('invalid', [False, True])
def test_config_api_readers_use_the_same_schedule_and_preserve_files(tmp_path, monkeypatch, invalid):
    monkeypatch.setattr(Config, 'OASIS_SIMULATION_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(SimulationManager, 'SIMULATION_DATA_DIR', str(tmp_path))
    directory = tmp_path / 'sim_schedule'
    directory.mkdir()
    raw = configuration()
    if invalid:
        raw['agent_configs'][0]['active_hours'] = ['9-17']
    path = directory / 'simulation_config.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    (directory / 'state.json').write_text(json.dumps({'status': 'ready', 'config_generated': True}), encoding='utf-8')
    original = path.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(simulation_api.simulation_bp, url_prefix='/simulation')
    for suffix in ('/config', '/config/realtime'):
        response = app.test_client().get('/simulation/sim_schedule' + suffix)
        assert response.status_code == (422 if invalid else 200)
        if not invalid:
            data = response.json['data']
            config = data['config'] if suffix.endswith('realtime') else data
            assert config['agent_configs'][0]['active_hours'] == [9, 17]
    assert path.read_bytes() == original
