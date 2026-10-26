"""Use the real filesystem IPC boundary; no model or graph result is simulated."""

import uuid
import time
from pathlib import Path

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services.simulation_ipc import CommandType, SimulationIPCClient, SimulationIPCServer
from app.services.simulation_runner import SimulationRunner


@pytest.fixture
def environment(tmp_path, monkeypatch):
    directory = tmp_path / 'sim_interview'
    directory.mkdir()
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    server = SimulationIPCServer(str(directory))
    server.start()
    app = Flask(__name__)
    app.register_blueprint(simulation_api.simulation_bp, url_prefix='/simulation')
    return directory, server, app.test_client()


def submit(client):
    response = client.post('/simulation/interview/batch', json={
        'simulation_id': 'sim_interview', 'async': True, 'platform': 'reddit',
        'interviews': [{'agent_id': 0, 'prompt': 'Synthetic plumbing question'}],
    })
    assert response.status_code == 202
    return response.json['data']['command_id']


def test_command_survives_new_clients_and_repeated_reads(environment):
    directory, server, client = environment
    command_id = submit(client)
    command = server.poll_commands()
    assert command.command_id == command_id
    assert command.command_type == CommandType.BATCH_INTERVIEW
    assert command.args['platform'] == 'reddit'
    assert command.args['interviews'][0]['agent_id'] == 0
    url = f'/simulation/interview/status/sim_interview/{command_id}'
    for _ in range(3):
        assert client.get(url).json['data']['status'] == 'pending'
    assert len(list((directory / 'ipc_commands').glob('*.json'))) == 1
    result = {'results': {'reddit_0': {'agent_id': 0, 'response': 'IPC transport fixture'}}}
    server.send_success(command_id, result)
    for _ in range(3):
        response = client.get(url)
        assert response.json['data']['result']['result'] == result
        assert SimulationIPCClient(str(directory)).read_command(command_id)['status'] == 'completed'
    assert (directory / 'ipc_responses' / f'{command_id}.json').exists()
    assert list((directory / 'ipc_commands').glob('*.json')) == []


def test_failed_command_remains_failed(environment):
    _directory, server, client = environment
    command_id = submit(client)
    server.send_error(command_id, 'Provider unavailable')
    response = client.get(f'/simulation/interview/status/sim_interview/{command_id}')
    assert response.json['data']['status'] == 'failed'
    assert response.json['data']['error'] == 'Provider unavailable'


def test_closed_environment_cannot_submit_and_pending_result_is_not_success(environment):
    _directory, server, client = environment
    command_id = submit(client)
    server.stop()
    response = client.get(f'/simulation/interview/status/sim_interview/{command_id}')
    assert response.json['data']['status'] == 'failed'
    response = client.post('/simulation/interview/batch', json={
        'simulation_id': 'sim_interview', 'async': True,
        'interviews': [{'agent_id': 0, 'prompt': 'Synthetic question'}],
    })
    assert response.status_code == 400


def test_partial_response_is_read_again_without_replaying_command(environment):
    directory, server, client = environment
    command_id = submit(client)
    path = directory / 'ipc_responses' / f'{command_id}.json'
    path.write_text('{', encoding='utf-8')
    assert SimulationIPCClient(str(directory)).read_command(command_id)['status'] == 'pending'
    server.send_success(command_id, {'results': {'reddit_0': {'response': 'IPC fixture'}}})
    assert SimulationIPCClient(str(directory)).read_command(command_id)['status'] == 'completed'


@pytest.mark.parametrize('results', [{}, {'reddit_0': {'agent_id': 0, 'response': ''}},
                                  {'twitter_0': {'agent_id': 0, 'response': 'wrong platform'}},
                                  {'reddit_1': {'agent_id': 1, 'response': 'wrong actor'}}])
def test_empty_or_wrong_actor_results_cannot_acknowledge_interview(environment, results):
    _directory, server, client = environment
    command_id = submit(client)
    server.send_success(command_id, {'results': results})
    response = client.get(f'/simulation/interview/status/sim_interview/{command_id}')
    assert response.status_code == 400


def test_deadline_does_not_delete_or_replay_request(environment, monkeypatch):
    directory, _server, client = environment
    monkeypatch.setenv('INTERVIEW_COMMAND_TIMEOUT_SECONDS', '1')
    command_id = submit(client)
    now = time.time()
    monkeypatch.setattr('app.services.simulation_runner.time.time', lambda: now + 2)
    response = client.get(f'/simulation/interview/status/sim_interview/{command_id}')
    assert response.json['data']['status'] == 'failed'
    assert (directory / 'ipc_commands' / f'{command_id}.json').exists()
    assert (directory / 'ipc_commands' / f'{command_id}.request').exists()


@pytest.mark.parametrize('bound', ['0', '7201', 'nan', 'inf'])
def test_invalid_wait_bounds_do_not_publish_commands(environment, monkeypatch, bound):
    directory, _server, client = environment
    monkeypatch.setenv('INTERVIEW_COMMAND_TIMEOUT_SECONDS', bound)
    response = client.post('/simulation/interview/batch', json={
        'simulation_id': 'sim_interview', 'async': True,
        'interviews': [{'agent_id': 0, 'prompt': 'Synthetic question'}],
    })
    assert response.status_code == 400
    assert list((directory / 'ipc_commands').glob('*')) == []


def test_explicit_restart_archives_pending_commands_instead_of_replaying_them(environment):
    directory, server, client = environment
    command_id = submit(client)
    server.stop()
    result = SimulationRunner.cleanup_simulation_logs('sim_interview')
    assert result['success']
    archive = Path(result['archive_directory'])
    assert (archive / 'ipc_commands' / f'{command_id}.json').exists()
    assert (archive / 'ipc_commands' / f'{command_id}.request').exists()
    replacement = SimulationIPCServer(str(directory))
    assert replacement.poll_commands() is None


@pytest.mark.parametrize('identifier', ['../outside', '/absolute', '', 'sim_../outside'])
def test_simulation_paths_are_rejected(environment, identifier):
    with pytest.raises(ValueError, match='Invalid simulation'):
        SimulationRunner.interview_command_client(identifier)


@pytest.mark.parametrize('identifier', ['../outside', 'not-a-uuid', str(uuid.uuid4())])
def test_unknown_or_invalid_commands_fail_without_creating_work(environment, identifier):
    directory, _server, _client = environment
    with pytest.raises(ValueError):
        SimulationRunner.read_interview_command('sim_interview', identifier)
    assert list((directory / 'ipc_commands').glob('*.json')) == []


@pytest.mark.parametrize('entry', [None, {'agent_id': True, 'prompt': 'text'},
                                 {'agent_id': -1, 'prompt': 'text'},
                                 {'agent_id': 0, 'prompt': ''}, {'agent_id': 0, 'prompt': 3}])
def test_invalid_inputs_do_not_queue_commands(environment, entry):
    directory, _server, client = environment
    response = client.post('/simulation/interview/batch', json={
        'simulation_id': 'sim_interview', 'async': True, 'interviews': [entry],
    })
    assert response.status_code == 400
    assert list((directory / 'ipc_commands').glob('*.json')) == []
