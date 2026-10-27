"""Explicit failed-run recovery with durable real local catalog receipts."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services import zep_graph_memory_updater as memory
from app.services.local_graph_store import LocalGraphStore
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import (
    RunnerStatus, SimulationRunState, SimulationRunner, SimulationStopPending,
)


@pytest.fixture
def failed_run(monkeypatch, tmp_path):
    identifier = 'sim-failed-recovery'
    store = LocalGraphStore(tmp_path / 'catalog')
    store.create_graph('graph-recovery', 'Recovery', 'Owned recovery fixture')
    episode = store.add_episode('graph-recovery', 'Retained failed activity',
                                metadata={'simulation_id': identifier})
    store.claim()
    store.finish(episode['uuid'], 'Provider failed')
    monkeypatch.setattr(memory.Config, 'GRAPH_BACKEND', 'local')
    monkeypatch.setattr(memory, 'get_zep_client', lambda _key: SimpleNamespace(store=store))
    updater = memory.ZepGraphMemoryUpdater('graph-recovery', simulation_id=identifier)
    updater._pending_episode_uuids = [episode['uuid']]
    monkeypatch.setattr(memory.ZepGraphMemoryManager, '_updaters', {identifier: updater})
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path / 'runs'))
    monkeypatch.setattr(SimulationRunner, '_run_states', {})
    monkeypatch.setattr(SimulationRunner, '_processes', {})
    monkeypatch.setattr(SimulationRunner, '_monitor_threads', {})
    monkeypatch.setattr(SimulationRunner, '_stop_requests', {})
    monkeypatch.setattr(SimulationRunner, '_graph_memory_enabled', {identifier: True})
    monkeypatch.setattr(SimulationRunner, '_manual_stop_requests', {identifier})
    state = SimulationRunState(simulation_id=identifier, runner_status=RunnerStatus.FAILED,
                               error='Original graph ingestion failure')
    SimulationRunner._run_states[identifier] = state
    SimulationRunner._save_run_state(state)
    directory = Path(SimulationRunner.RUN_STATE_DIR) / identifier
    (directory / 'simulation_config.json').write_text('{"unchanged":true}')
    return SimpleNamespace(identifier=identifier, store=store, episode=episode,
                           updater=updater, state=state, directory=directory)


def test_failed_restart_archives_receipts_and_original_failure(failed_run):
    run = failed_run
    original = (run.directory / 'run_state.json').read_bytes()
    SimulationRunner.acknowledge_failed_restart(run.identifier)
    assert memory.ZepGraphMemoryManager.get_updater(run.identifier) is None
    assert run.state.runner_status == RunnerStatus.FAILED
    assert run.state.error == 'Original graph ingestion failure'
    assert run.identifier not in SimulationRunner._manual_stop_requests
    assert run.identifier not in SimulationRunner._graph_memory_enabled
    result = SimulationRunner.cleanup_simulation_logs(run.identifier)
    assert result['success']
    archive = Path(result['archive_directory'])
    assert (archive / 'run_state.json').read_bytes() == original
    receipt = json.loads((archive / 'graph_memory_failure.json').read_text())
    assert receipt['episodes'][0]['uuid'] == run.episode['uuid']
    assert receipt['episodes'][0]['data'] == 'Retained failed activity'
    assert receipt['episodes'][0]['status'] == 'failed'
    assert run.store.episode(run.episode['uuid'])['status'] == 'failed'
    assert run.store.claim() is None
    assert (run.directory / 'simulation_config.json').read_text() == '{"unchanged":true}'


@pytest.mark.parametrize('blocker', ['queued', 'running', 'producer', 'buffer', 'unconfirmed', 'cloud'])
def test_failed_restart_refuses_unconfirmed_or_active_work(failed_run, monkeypatch, blocker):
    run = failed_run
    if blocker in {'queued', 'running'}:
        run.store.add_episode('graph-recovery', 'Another accepted activity')
        if blocker == 'running':
            run.store.claim()
    elif blocker == 'producer':
        run.updater._running = True
    elif blocker == 'buffer':
        run.updater._platform_buffers['twitter'].append(object())
    elif blocker == 'unconfirmed':
        run.updater._pending_episode_uuids.append('unknown-receipt')
    else:
        monkeypatch.setattr(memory.Config, 'GRAPH_BACKEND', 'zep')
    with pytest.raises(RuntimeError):
        SimulationRunner.acknowledge_failed_restart(run.identifier)
    assert memory.ZepGraphMemoryManager.get_updater(run.identifier) is run.updater
    assert not (run.directory / 'graph_memory_failure.json').exists()
    assert run.state.runner_status == RunnerStatus.FAILED


@pytest.mark.parametrize('blocker', ['process', 'monitor', 'stop', 'lock'])
def test_failed_restart_waits_for_runner_ownership(failed_run, blocker):
    run = failed_run
    lock = SimulationRunner._finalization_lock(run.identifier)
    if blocker == 'process':
        SimulationRunner._processes[run.identifier] = SimpleNamespace(poll=lambda: None)
    elif blocker == 'monitor':
        SimulationRunner._monitor_threads[run.identifier] = SimpleNamespace(is_alive=lambda: True)
    elif blocker == 'stop':
        SimulationRunner._stop_requests[run.identifier] = object()
    else:
        lock.acquire()
    try:
        with pytest.raises(SimulationStopPending):
            SimulationRunner.acknowledge_failed_restart(run.identifier)
    finally:
        if blocker == 'lock':
            lock.release()
    assert memory.ZepGraphMemoryManager.get_updater(run.identifier) is run.updater
    assert not (run.directory / 'graph_memory_failure.json').exists()


def test_receipt_write_failure_keeps_failed_updater_registered(failed_run, monkeypatch):
    run = failed_run
    monkeypatch.setattr(memory.os, 'replace', lambda *_args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        SimulationRunner.acknowledge_failed_restart(run.identifier)
    assert memory.ZepGraphMemoryManager.get_updater(run.identifier) is run.updater
    assert run.state.runner_status == RunnerStatus.FAILED


def test_restart_api_preserves_failure_before_launch_and_rejects_concurrent_start(failed_run, monkeypatch):
    run = failed_run
    simulation = SimpleNamespace(status=SimulationStatus.FAILED, project_id='project-recovery')
    monkeypatch.setattr(simulation_api, 'SimulationManager', lambda: SimpleNamespace(
        get_simulation=lambda _id: simulation, _save_simulation_state=lambda _value: None))
    monkeypatch.setattr(simulation_api, '_check_simulation_prepared', lambda _id: (True, {}))
    entered, release = threading.Event(), threading.Event()
    launches = []

    def launch(_cls, **kwargs):
        archive, = (run.directory / 'run_attempts').iterdir()
        assert json.loads((archive / 'run_state.json').read_text())['runner_status'] == 'failed'
        assert (archive / 'graph_memory_failure.json').exists()
        launches.append(kwargs)
        entered.set()
        assert release.wait(5)
        return SimulationRunState(simulation_id=run.identifier)

    monkeypatch.setattr(SimulationRunner, 'start_simulation', classmethod(launch))
    app = Flask(__name__)
    app.register_blueprint(simulation_api.simulation_bp, url_prefix='/simulation')
    payload = {'simulation_id': run.identifier, 'force': True}
    responses = []
    worker = threading.Thread(target=lambda: responses.append(
        app.test_client().post('/simulation/start', json=payload)))
    worker.start()
    try:
        assert entered.wait(5)
        concurrent = app.test_client().post('/simulation/start', json=payload)
        assert concurrent.status_code == 409
        assert concurrent.json['pending'] is True
    finally:
        release.set()
        worker.join(5)
    assert responses[0].status_code == 200, responses[0].json
    assert len(launches) == 1
