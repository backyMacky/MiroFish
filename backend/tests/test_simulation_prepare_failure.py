import json

import pytest

from app import create_app
from app.config import Config
from app.models.task import TaskManager, TaskStatus
from app.services import simulation_manager as simulation_manager_module
from app.services.simulation_manager import (
    SimulationManager,
    SimulationState,
    SimulationStatus,
)
from app.services.zep_entity_reader import FilteredEntities


def _write_failed_state(root, simulation_id="sim_failed"):
    sim_dir = root / simulation_id
    sim_dir.mkdir(parents=True)
    (sim_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "error": "no usable entities",
                "entities_count": 0,
                "profiles_generated": False,
                "config_generated": False,
            }
        ),
        encoding="utf-8",
    )
    return simulation_id


def test_realtime_endpoints_expose_terminal_failure(tmp_path, monkeypatch):
    simulation_id = _write_failed_state(tmp_path)
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    config_response = client.get(f"/api/simulation/{simulation_id}/config/realtime")
    profile_response = client.get(f"/api/simulation/{simulation_id}/profiles/realtime")

    assert config_response.status_code == 200
    expected_config_state = {
        "status": "failed",
        "error": "no usable entities",
        "generation_stage": "failed",
        "profiles_generated": False,
        "config_generated": False,
        "is_generating": False,
    }
    for key, expected in expected_config_state.items():
        assert config_response.json["data"][key] == expected
    assert profile_response.status_code == 200
    assert profile_response.json["data"]["status"] == "failed"
    assert profile_response.json["data"]["error"] == "no usable entities"
    assert profile_response.json["data"]["is_generating"] is False


def test_zero_entities_persists_failed_state_and_raises(tmp_path, monkeypatch):
    class EmptyReader:
        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=[],
                entity_types=set(),
                total_count=3,
                filtered_count=0,
            )

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(simulation_manager_module, "ZepEntityReader", EmptyReader)

    manager = SimulationManager()
    state = SimulationState(
        simulation_id="sim_empty",
        project_id="project",
        graph_id="graph",
        status=SimulationStatus.READY,
        profiles_generated=True,
        config_generated=True,
        config_reasoning="stale",
        error="stale",
    )
    manager._save_simulation_state(state)

    sim_dir = tmp_path / state.simulation_id
    old_profiles = '[{"name":"Previous attempt"}]'
    (sim_dir / 'reddit_profiles.json').write_text(old_profiles, encoding='utf-8')
    (sim_dir / 'simulation_config.json').write_text('{"old":true}', encoding='utf-8')

    with pytest.raises(ValueError, match="没有找到符合条件的实体"):
        manager.prepare_simulation(
            simulation_id=state.simulation_id,
            simulation_requirement="requirement",
            document_text="document",
        )

    persisted = json.loads(
        (tmp_path / state.simulation_id / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "failed"
    assert persisted["profiles_generated"] is False
    assert persisted["config_generated"] is False
    assert persisted["config_reasoning"] == ""
    assert "没有找到符合条件的实体" in persisted["error"]
    assert persisted['profiles_count'] == 0
    assert not (sim_dir / 'reddit_profiles.json').exists()
    previous = list((sim_dir / 'preparation_attempts').iterdir())
    assert len(previous) == 1
    assert (previous[0] / 'reddit_profiles.json').read_text(encoding='utf-8') == old_profiles


@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.PROCESSING])
def test_prepare_reuses_active_job_even_when_regeneration_is_requested(tmp_path, monkeypatch, status):
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))
    state = SimulationState(
        simulation_id="sim_active_guard", project_id="synthetic", graph_id="graph",
        status=SimulationStatus.PREPARING, entities_count=4,
    )
    SimulationManager()._save_simulation_state(state)
    tasks = TaskManager()
    task_id = tasks.create_task("simulation_prepare", {"simulation_id": state.simulation_id})
    tasks.update_task(task_id, status=status)
    try:
        client = create_app().test_client()
        for force in [False, True]:
            response = client.post('/api/simulation/prepare', json={
                "simulation_id": state.simulation_id, "force_regenerate": force,
            })
            assert response.status_code == 200
            assert response.json['data']['task_id'] == task_id
            assert response.json['data']['expected_entities_count'] == 4
        assert len([t for t in tasks.list_tasks() if t['metadata'].get('simulation_id') == state.simulation_id]) == 1
    finally:
        with tasks._task_lock:
            tasks._tasks.pop(task_id)


@pytest.mark.parametrize("field,value", [
    ("force_regenerate", "false"), ("use_llm_for_profiles", 1),
    ("parallel_profile_count", True), ("parallel_profile_count", 0),
    ("parallel_profile_count", 33), ("parallel_profile_count", "5"),
])
def test_prepare_rejects_invalid_options_before_starting_work(field, value):
    response = create_app().test_client().post('/api/simulation/prepare', json={
        "simulation_id": "sim_invalid", field: value,
    })
    assert response.status_code == 400


@pytest.mark.parametrize('status', [SimulationStatus.RUNNING, SimulationStatus.STOPPING])
def test_preparation_cannot_overwrite_a_running_simulation(tmp_path, monkeypatch, status):
    monkeypatch.setattr(SimulationManager, 'SIMULATION_DATA_DIR', str(tmp_path))
    state = SimulationState(simulation_id='sim_running', project_id='synthetic', graph_id='graph', status=status)
    SimulationManager()._save_simulation_state(state)
    response = create_app().test_client().post('/api/simulation/prepare', json={
        'simulation_id': state.simulation_id, 'force_regenerate': True,
    })
    assert response.status_code == 409
    assert json.loads((tmp_path / state.simulation_id / 'state.json').read_text())['status'] == status.value


def test_interrupted_state_publish_preserves_last_complete_json(tmp_path, monkeypatch):
    from app.utils import fileio
    monkeypatch.setattr(SimulationManager, 'SIMULATION_DATA_DIR', str(tmp_path))
    manager = SimulationManager()
    state = SimulationState(simulation_id='sim_atomic', project_id='synthetic', graph_id='graph')
    manager._save_simulation_state(state)
    before = (tmp_path / state.simulation_id / 'state.json').read_bytes()
    def fail(*_args):
        raise OSError('Synthetic interrupted publish')
    monkeypatch.setattr(fileio.os, 'replace', fail)
    state.profiles_count = 4
    with pytest.raises(OSError, match='interrupted'):
        manager._save_simulation_state(state)
    assert (tmp_path / state.simulation_id / 'state.json').read_bytes() == before
    assert sorted(p.name for p in (tmp_path / state.simulation_id).iterdir()) == ['state.json']
