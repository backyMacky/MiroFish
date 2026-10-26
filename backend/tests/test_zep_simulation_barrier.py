from types import SimpleNamespace
import threading
import json
import subprocess
import sys
import time

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services import simulation_runner as runner_module
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
    SimulationStopPending,
)


@pytest.mark.parametrize('closed', [True, False])
@pytest.mark.parametrize('status', [SimulationStatus.FAILED, SimulationStatus.STOPPING, SimulationStatus.COMPLETED])
def test_close_environment_preserves_run_result(monkeypatch, closed, status):
    state = SimpleNamespace(status=status, error='retained ingestion diagnostic')
    saved = []
    monkeypatch.setattr(
        simulation_api, 'SimulationManager',
        lambda: SimpleNamespace(
            get_simulation=lambda _identifier: state,
            _save_simulation_state=lambda value: saved.append(value.status),
        ),
    )
    monkeypatch.setattr(
        SimulationRunner, 'close_simulation_env',
        classmethod(lambda _cls, simulation_id, **_kwargs: {'success': closed}),
    )
    app = Flask(__name__)
    app.register_blueprint(simulation_api.simulation_bp, url_prefix='/simulation')
    response = app.test_client().post('/simulation/close-env', json={'simulation_id': 'sim-close'})
    assert response.status_code == 200
    assert response.json['success'] is closed
    assert state.status == status
    assert state.error == 'retained ingestion diagnostic'
    assert saved == []


def test_finished_loops_drain_while_real_interview_process_stays_alive(monkeypatch, tmp_path):
    """The process deliberately waits for IPC after its producer-end receipt."""
    simulation_id = 'sim-live-environment'
    directory = tmp_path / simulation_id / 'twitter'
    directory.mkdir(parents=True)
    (directory / 'actions.jsonl').write_text(
        '{"event_type":"simulation_end","total_rounds":2,"total_actions":0}\n', encoding='utf-8',
    )
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(SimulationRunner, '_sync_simulation_status', classmethod(lambda *_a, **_k: None))
    state = SimulationRunState(simulation_id=simulation_id, runner_status=RunnerStatus.RUNNING, twitter_running=True)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    SimulationRunner._processes[simulation_id] = process
    SimulationRunner._run_states[simulation_id] = state
    SimulationRunner._graph_memory_enabled[simulation_id] = True
    drained = []
    def drain(_cls, identifier):
        assert identifier == simulation_id
        assert process.poll() is None
        assert state.runner_status == RunnerStatus.STOPPING
        drained.append(True)
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, 'stop_updater', classmethod(drain))
    try:
        SimulationRunner._monitor_simulation(simulation_id)
        assert drained == [True]
        assert process.poll() is None
        assert state.runner_status == RunnerStatus.COMPLETED
        assert SimulationRunner._processes[simulation_id] is process
        saved = json.loads((tmp_path / simulation_id / 'run_state.json').read_text())
        assert saved['runner_status'] == 'completed'
    finally:
        process.terminate()
        process.wait(timeout=10)
        for _ in range(100):
            if simulation_id not in SimulationRunner._processes:
                break
            time.sleep(0.01)
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._run_states.pop(simulation_id, None)
        SimulationRunner._graph_memory_enabled.pop(simulation_id, None)


def test_missing_second_platform_log_is_not_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    directory = tmp_path / 'sim-dual' / 'twitter'
    directory.mkdir(parents=True)
    (directory / 'actions.jsonl').write_text('', encoding='utf-8')
    state = SimulationRunState(simulation_id='sim-dual', twitter_completed=True, reddit_running=True)
    assert not SimulationRunner._check_all_platforms_completed(state)


def test_explicit_restart_archives_previous_run_without_touching_preparation(monkeypatch, tmp_path):
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    directory = tmp_path / 'sim-archive'
    (directory / 'twitter').mkdir(parents=True)
    (directory / 'run_state.json').write_text('{"runner_status":"completed"}')
    (directory / 'twitter' / 'actions.jsonl').write_text('synthetic action\n')
    (directory / 'simulation_config.json').write_text('synthetic configuration')
    result = SimulationRunner.cleanup_simulation_logs('sim-archive')
    assert result['success']
    archive = __import__('pathlib').Path(result['archive_directory'])
    assert (archive / 'twitter' / 'actions.jsonl').read_text() == 'synthetic action\n'
    assert (directory / 'simulation_config.json').read_text() == 'synthetic configuration'
    assert not (directory / 'run_state.json').exists()
    with pytest.raises(ValueError, match='Invalid simulation directory'):
        SimulationRunner.cleanup_simulation_logs('../outside')


@pytest.mark.parametrize('force', [False, True])
def test_ready_setup_cannot_bypass_prior_run_archive(monkeypatch, tmp_path, force):
    simulation_id = 'sim-ready-restart'
    directory = tmp_path / simulation_id
    (directory / 'twitter').mkdir(parents=True)
    originals = {
        'twitter/actions.jsonl': b'old simulation_end event\n',
        'twitter_simulation.db': b'old database',
        'twitter_simulation.db-wal': b'old wal',
        'run_state.json': b'{"runner_status":"completed"}',
    }
    for name, content in originals.items():
        (directory / name).write_bytes(content)
    config = directory / 'simulation_config.json'
    config.write_bytes(b'prepared config retained verbatim')
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    state = SimpleNamespace(status=SimulationStatus.READY, project_id='proj-ready')
    monkeypatch.setattr(simulation_api, 'SimulationManager', lambda: SimpleNamespace(
        get_simulation=lambda _id: state, _save_simulation_state=lambda _state: None))
    monkeypatch.setattr(simulation_api, '_check_simulation_prepared', lambda _id: (True, {}))
    monkeypatch.setattr(SimulationRunner, 'get_run_state', classmethod(lambda *_args: None))
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, 'get_updater', classmethod(lambda *_args: None))
    launches = []

    def launch(_cls, **kwargs):
        assert not SimulationRunner.has_run_artifacts(simulation_id)
        archives = list((directory / 'run_attempts').iterdir())
        assert len(archives) == 1
        for name, content in originals.items():
            assert (archives[0] / name).read_bytes() == content
        assert config.read_bytes() == b'prepared config retained verbatim'
        launches.append(kwargs)
        return SimulationRunState(simulation_id=simulation_id)

    monkeypatch.setattr(SimulationRunner, 'start_simulation', classmethod(launch))
    app = Flask(__name__)
    app.register_blueprint(simulation_api.simulation_bp, url_prefix='/simulation')
    response = app.test_client().post('/simulation/start', json={
        'simulation_id': simulation_id, 'force': force, 'enable_graph_memory_update': False})
    if force:
        assert response.status_code == 200, response.json
        assert response.json['data']['force_restarted'] is True
        assert len(launches) == 1
    else:
        assert response.status_code == 409
        assert launches == []
        assert not (directory / 'run_attempts').exists()
        for name, content in originals.items():
            assert (directory / name).read_bytes() == content


def test_runner_refuses_unarchived_events_before_launch(monkeypatch, tmp_path):
    directory = tmp_path / 'sim-unarchived'
    directory.mkdir()
    (directory / 'simulation_config.json').write_text('{}')
    (directory / 'run_state.json').write_text('{}')
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    with pytest.raises(ValueError, match='must be archived'):
        SimulationRunner.start_simulation('sim-unarchived')


def test_stop_request_is_bounded_and_deduplicated_behind_real_drain_lock(monkeypatch, tmp_path):
    simulation_id = 'sim-locked-stop'
    state = SimulationRunState(simulation_id=simulation_id, runner_status=RunnerStatus.STOPPING)
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(SimulationRunner, 'get_run_state', classmethod(lambda *_args: state))
    monkeypatch.setattr(SimulationRunner, '_sync_simulation_status', classmethod(lambda *_args: None))
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, 'get_updater', classmethod(lambda *_args: None))
    lock = SimulationRunner._finalization_lock(simulation_id)
    lock.acquire()
    original_stop = SimulationRunner.stop_simulation
    entered = threading.Event()
    calls = []

    def stop(_cls, identifier):
        calls.append(identifier)
        entered.set()
        return original_stop(identifier)

    monkeypatch.setattr(SimulationRunner, 'stop_simulation', classmethod(stop))
    try:
        start = time.monotonic()
        with pytest.raises(SimulationStopPending):
            SimulationRunner.request_stop(simulation_id, wait_timeout=0.02)
        assert entered.wait(1)
        with pytest.raises(SimulationStopPending):
            SimulationRunner.request_stop(simulation_id, wait_timeout=0.02)
        assert time.monotonic() - start < 1
        assert calls == [simulation_id]
        future = SimulationRunner._stop_requests[simulation_id]
    finally:
        lock.release()
    try:
        assert future.result(timeout=3).runner_status == RunnerStatus.STOPPED
        assert SimulationRunner.request_stop(simulation_id).runner_status == RunnerStatus.STOPPED
        assert calls == [simulation_id]
    finally:
        SimulationRunner._manual_stop_requests.discard(simulation_id)


@pytest.mark.parametrize('locale, expected', [('en', 'Graph ingestion did not complete'), ('zh', '图谱写入未完整完成')])
def test_manual_stop_surfaces_graph_ingestion_failure(monkeypatch, locale, expected):
    state = SimulationRunState(
        simulation_id="sim-1",
        runner_status=RunnerStatus.RUNNING,
    )
    saved = []
    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, value: saved.append(value.runner_status)),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "stop_updater",
        classmethod(
            lambda _cls, _simulation_id: (_ for _ in ()).throw(
                RuntimeError("ingestion incomplete")
            )
        ),
    )
    SimulationRunner._processes.pop("sim-1", None)
    SimulationRunner._graph_memory_enabled["sim-1"] = True

    previous_locale = runner_module.get_locale()
    runner_module.set_locale(locale)
    try:
        with pytest.raises(RuntimeError, match="ingestion incomplete"):
            SimulationRunner.stop_simulation("sim-1")

        assert state.runner_status == RunnerStatus.FAILED
        assert "ingestion incomplete" in state.error
        assert expected in state.error
        assert 'Zep' not in state.error
        assert saved[-1] == RunnerStatus.FAILED
    finally:
        runner_module.set_locale(previous_locale)
        SimulationRunner._graph_memory_enabled.pop("sim-1", None)
        SimulationRunner._manual_stop_requests.discard("sim-1")


def test_platform_completion_does_not_publish_terminal_success_before_barrier(
    monkeypatch, tmp_path
):
    simulation_id = "sim-1"
    sim_dir = tmp_path / simulation_id / "twitter"
    sim_dir.mkdir(parents=True)
    log_path = sim_dir / "actions.jsonl"
    log_path.write_text(
        '{"event_type":"simulation_end","total_rounds":1,"total_actions":0}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
        twitter_running=True,
    )

    SimulationRunner._read_action_log(str(log_path), 0, state, "twitter")

    assert state.twitter_completed is True
    assert state.runner_status == RunnerStatus.RUNNING


def test_manual_stop_timeout_leaves_monitor_owned_state_stopping(monkeypatch):
    state = SimulationRunState(
        simulation_id="sim-timeout",
        runner_status=RunnerStatus.RUNNING,
    )

    class Monitor:
        def join(self, timeout):
            assert timeout >= 30

        def is_alive(self):
            return True

    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    SimulationRunner._monitor_threads["sim-timeout"] = Monitor()
    SimulationRunner._processes.pop("sim-timeout", None)
    SimulationRunner._graph_memory_enabled.pop("sim-timeout", None)

    try:
        with pytest.raises(TimeoutError, match="仍在停止中"):
            SimulationRunner.stop_simulation("sim-timeout")
        assert state.runner_status == RunnerStatus.STOPPING
    finally:
        SimulationRunner._monitor_threads.pop("sim-timeout", None)
        SimulationRunner._manual_stop_requests.discard("sim-timeout")


def test_failed_ingestion_finalization_can_be_retried(monkeypatch):
    state = SimulationRunState(
        simulation_id="sim-retry",
        runner_status=RunnerStatus.FAILED,
        error="first drain timed out",
    )
    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _simulation_id: object()),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "stop_updater",
        classmethod(lambda _cls, _simulation_id: None),
    )
    SimulationRunner._graph_memory_enabled["sim-retry"] = True
    SimulationRunner._monitor_threads.pop("sim-retry", None)

    try:
        result = SimulationRunner.stop_simulation("sim-retry")
        assert result.runner_status == RunnerStatus.STOPPED
        assert result.error is None
    finally:
        SimulationRunner._graph_memory_enabled.pop("sim-retry", None)
        SimulationRunner._manual_stop_requests.discard("sim-retry")


def test_stop_api_keeps_pending_finalization_out_of_failed_state(monkeypatch):
    simulation = SimpleNamespace(status=SimulationStatus.STOPPING, error=None)
    saved = []
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "request_stop",
        classmethod(
            lambda _cls, _simulation_id, **_kwargs: (_ for _ in ()).throw(
                SimulationStopPending("still draining")
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation,
            _save_simulation_state=lambda state: saved.append(state.status),
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/stop",
        method="POST",
        json={"simulation_id": "sim-pending"},
    ):
        response, status = simulation_api.stop_simulation()

    assert status == 202
    assert response.get_json()["pending"] is True
    assert simulation.status == SimulationStatus.STOPPING
    assert saved == []


@pytest.mark.parametrize(
    "field",
    ["force", "enable_graph_memory_update"],
)
def test_simulation_start_rejects_string_booleans(field):
    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/start",
        method="POST",
        json={"simulation_id": "sim-1", field: "false"},
    ):
        response, status = simulation_api.start_simulation()

    assert status == 400
    assert "JSON boolean" in response.get_json()["error"]


def test_force_restart_does_not_continue_while_old_ingestion_is_pending(monkeypatch):
    simulation = SimpleNamespace(
        simulation_id="sim-1",
        project_id="proj-1",
        graph_id="graph-1",
        status=SimulationStatus.STOPPING,
    )
    cleanup_called = []
    monkeypatch.setattr(
        simulation_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation,
            _save_simulation_state=lambda _state: None,
        ),
    )
    monkeypatch.setattr(
        simulation_api,
        "_check_simulation_prepared",
        lambda _simulation_id: (True, {}),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                runner_status=RunnerStatus.STOPPING
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "request_stop",
        classmethod(
            lambda _cls, _simulation_id: (_ for _ in ()).throw(
                SimulationStopPending("still draining")
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "cleanup_simulation_logs",
        classmethod(
            lambda _cls, _simulation_id: cleanup_called.append(True)
        ),
    )
    monkeypatch.setattr(
        simulation_api.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _simulation_id: object()),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/start",
        method="POST",
        json={"simulation_id": "sim-1", "force": True},
    ):
        response, status = simulation_api.start_simulation()

    assert status == 409
    assert response.get_json()["pending"] is True
    assert cleanup_called == []


def test_monitor_start_failure_terminates_the_spawned_process(monkeypatch, tmp_path):
    simulation_id = "sim-start-failure"
    sim_dir = tmp_path / "runs" / simulation_id
    scripts_dir = tmp_path / "scripts"
    sim_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    (sim_dir / "simulation_config.json").write_text(
        json.dumps({
            "time_config": {
                "total_simulation_hours": 1,
                "minutes_per_round": 60,
            }
        }),
        encoding="utf-8",
    )
    (scripts_dir / "run_twitter_simulation.py").write_text("pass\n", encoding="utf-8")

    class Process:
        pid = 123

        def poll(self):
            return None

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("monitor failed")

    terminated = []
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(SimulationRunner, "SCRIPTS_DIR", str(scripts_dir))
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(runner_module.threading, "Thread", BrokenThread)
    monkeypatch.setattr(
        SimulationRunner,
        "_terminate_process",
        classmethod(lambda _cls, _process, sim_id: terminated.append(sim_id)),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, *_args, **_kwargs: None),
    )

    try:
        with pytest.raises(RuntimeError, match="monitor failed"):
            SimulationRunner.start_simulation(
                simulation_id,
                platform="twitter",
                enable_graph_memory_update=False,
            )
        assert terminated == [simulation_id]
        assert simulation_id not in SimulationRunner._processes
        assert simulation_id not in SimulationRunner._action_queues
        assert simulation_id not in SimulationRunner._stdout_files
    finally:
        SimulationRunner._run_states.pop(simulation_id, None)
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._action_queues.pop(simulation_id, None)
        SimulationRunner._stdout_files.pop(simulation_id, None)
        SimulationRunner._stderr_files.pop(simulation_id, None)
        SimulationRunner._graph_memory_enabled.pop(simulation_id, None)


def test_shutdown_terminates_producer_before_tail_read_and_updater_drain(
    monkeypatch,
):
    simulation_id = "sim-shutdown-order"
    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
    )
    events = []

    class Process:
        pid = 123
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

    process = Process()

    class Monitor:
        alive = True

        def join(self, timeout):
            assert timeout >= 30
            events.extend(["tail-read", "updater-drain"])
            state.runner_status = RunnerStatus.STOPPED
            SimulationRunner._graph_memory_enabled.pop(simulation_id, None)
            self.alive = False

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_terminate_process",
        classmethod(
            lambda _cls, proc, _simulation_id, **_kwargs: (
                events.append("producer-terminate"),
                setattr(proc, "stopped", True),
            )
        ),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "get_simulation_ids",
        classmethod(lambda _cls: [simulation_id]),
    )
    updater = object()
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _simulation_id: updater),
    )

    SimulationRunner._cleanup_done = False
    SimulationRunner._processes[simulation_id] = process
    SimulationRunner._monitor_threads[simulation_id] = Monitor()
    SimulationRunner._graph_memory_enabled[simulation_id] = True
    try:
        SimulationRunner.cleanup_all_simulations()
        assert events == [
            "producer-terminate",
            "tail-read",
            "updater-drain",
        ]
        assert state.runner_status == RunnerStatus.STOPPED
    finally:
        SimulationRunner._cleanup_done = False
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._monitor_threads.pop(simulation_id, None)
        SimulationRunner._graph_memory_enabled.pop(simulation_id, None)
        SimulationRunner._manual_stop_requests.discard(simulation_id)


def test_shutdown_drain_failure_remains_failed_and_retryable(monkeypatch):
    simulation_id = "sim-shutdown-failure"
    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
    )
    updater = object()

    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "get_simulation_ids",
        classmethod(lambda _cls: [simulation_id]),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _simulation_id: updater),
    )
    monkeypatch.setattr(
        runner_module.ZepGraphMemoryManager,
        "stop_updater",
        classmethod(
            lambda _cls, _simulation_id: (_ for _ in ()).throw(
                RuntimeError("drain incomplete")
            )
        ),
    )

    SimulationRunner._cleanup_done = False
    SimulationRunner._graph_memory_enabled[simulation_id] = True
    SimulationRunner._monitor_threads.pop(simulation_id, None)
    SimulationRunner._processes.pop(simulation_id, None)
    try:
        SimulationRunner.cleanup_all_simulations()
        assert state.runner_status == RunnerStatus.FAILED
        assert "drain incomplete" in state.error
        assert SimulationRunner._graph_memory_enabled[simulation_id] is True
        assert SimulationRunner._cleanup_done is False
    finally:
        SimulationRunner._cleanup_done = False
        SimulationRunner._graph_memory_enabled.pop(simulation_id, None)
        SimulationRunner._manual_stop_requests.discard(simulation_id)
