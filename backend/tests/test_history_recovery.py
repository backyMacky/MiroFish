"""Pending history remains identifiable before model configuration exists."""

import json

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.models.project import ProjectManager
from app.services.simulation_manager import SimulationManager, SimulationState
from app.services.simulation_runner import SimulationRunner


@pytest.mark.parametrize("configured_requirement", [None, "", "Prepared requirement"])
def test_history_retains_requirement_before_preparation(
    tmp_path, monkeypatch, configured_requirement
):
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path / "simulations"))
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(simulation_api, "_get_report_id_for_simulation", lambda _id: None)
    project = ProjectManager.create_project("Synthetic history recovery")
    project.simulation_requirement = "Original synthetic procurement requirement"
    ProjectManager.save_project(project)
    state = SimulationState(
        simulation_id="sim_history", project_id=project.project_id, graph_id="synthetic_graph"
    )
    manager = SimulationManager()
    manager._save_simulation_state(state)
    if configured_requirement is not None:
        (tmp_path / "simulations" / state.simulation_id / "simulation_config.json").write_text(
            json.dumps({"simulation_requirement": configured_requirement}), encoding="utf-8"
        )
    app = Flask(__name__)
    with app.test_request_context("/api/simulation/history"):
        response = simulation_api.get_simulation_history()
    assert response.status_code == 200
    assert response.json["count"] == 1
    assert response.json["data"][0]["simulation_requirement"] == (
        configured_requirement or project.simulation_requirement
    )
