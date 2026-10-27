"""Background HTTP lifecycle with real persistence/threads; LLM boundary controlled."""

import io
import threading
import time

import pytest

from app import create_app
from app.api import graph as graph_api
from app.models.project import ProjectManager, ProjectStatus
from app.models.task import TaskManager, TaskStatus


@pytest.fixture
def background(tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    answers = {"error": None}

    class Generator:
        def generate(self, **_kwargs):
            entered.set()
            try:
                assert release.wait(5), "Test did not release controlled inference"
                if answers["error"]:
                    raise answers["error"]
                return {
                    "entity_types": [{"name": "Person"}],
                    "edge_types": [],
                    "analysis_summary": "Synthetic",
                }
            finally:
                finished.set()

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", Generator)
    app = create_app()
    app.config.update(TESTING=True)
    yield app.test_client(), entered, release, answers
    release.set()
    if entered.is_set():
        assert finished.wait(3)


def submit(client, **extra):
    return client.post(
        "/api/graph/ontology/generate",
        data={
            "async_ontology": "true",
            "simulation_requirement": "Synthetic discussion",
            "files": (io.BytesIO(b"Avery works for Harbor Labs."), "fixture.txt"),
            **extra,
        },
        content_type="multipart/form-data",
    )


def terminal(task_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = TaskManager().get_task(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return task
        time.sleep(0.01)
    pytest.fail("Background task never reached a terminal state")


def test_acceptance_is_durable_before_inference_and_blocks_conflicting_mutations(background):
    client, entered, release, _ = background
    response = submit(client, project_name="Preserved project")
    assert response.status_code == 202 and entered.wait(1)
    data = response.json["data"]
    project = ProjectManager.get_project(data["project_id"])
    assert project.ontology_task_id == data["task_id"] and project.ontology is None
    for path, method in [
        (f"/api/graph/project/{project.project_id}", "delete"),
        (f"/api/graph/project/{project.project_id}/reset", "post"),
    ]:
        assert getattr(client, method)(path).status_code == 409
    assert submit(client, project_id=project.project_id).status_code == 409
    assert len(ProjectManager.get_project(project.project_id).files) == 1
    release.set()
    task = terminal(data["task_id"])
    assert task.status == TaskStatus.COMPLETED
    assert task.result["ontology"]["entity_types"][0]["name"] == "Person"
    assert ProjectManager.get_project(project.project_id).status == ProjectStatus.ONTOLOGY_GENERATED


def test_background_provider_failure_is_safe_and_persisted(background):
    client, entered, release, answers = background

    class ProviderError(RuntimeError):
        status_code = 429
        request_id = "synthetic-request"

    answers["error"] = ProviderError("PRIVATE PROVIDER RESPONSE")
    response = submit(client)
    assert response.status_code == 202 and entered.wait(1)
    release.set()
    task = terminal(response.json["data"]["task_id"])
    assert task.status == TaskStatus.FAILED and "HTTP 429" in task.error
    assert "PRIVATE" not in task.error
    project = ProjectManager.get_project(response.json["data"]["project_id"])
    assert project.status == ProjectStatus.FAILED and project.error == task.error
    answers["error"] = None
    retry = client.post(
        "/api/graph/ontology/generate",
        data={
            "project_id": project.project_id,
            "async_ontology": "true",
        },
    )
    assert retry.status_code == 202
    assert terminal(retry.json["data"]["task_id"]).status == TaskStatus.COMPLETED
    recovered = ProjectManager.get_project(project.project_id)
    assert recovered.name == project.name
    assert len(recovered.files) == 1
    assert recovered.status == ProjectStatus.ONTOLOGY_GENERATED


def test_failed_metadata_publish_keeps_the_previous_readable_project(tmp_path, monkeypatch):
    from app.models import project as project_module

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    project = ProjectManager.create_project("Original")
    project.name = "Uncommitted change"

    def full_disk(*_args):
        raise OSError("Synthetic disk write failure")

    monkeypatch.setattr(project_module.os, "replace", full_disk)
    with pytest.raises(OSError, match="Synthetic"):
        ProjectManager.save_project(project)
    assert ProjectManager.get_project(project.project_id).name == "Original"
    assert not list(tmp_path.rglob(".project-*.json"))


def test_new_projects_use_configured_chunk_sizes(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "DEFAULT_CHUNK_SIZE", 1600)
    monkeypatch.setattr(Config, "DEFAULT_CHUNK_OVERLAP", 160)
    project = ProjectManager.create_project("Configured chunking")
    restored = ProjectManager.get_project(project.project_id)
    assert (restored.chunk_size, restored.chunk_overlap) == (1600, 160)
