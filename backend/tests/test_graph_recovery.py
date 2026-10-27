"""Graph retry claims and failure responses with real project/task persistence."""

from flask import Flask

from app.api import graph as graph_api
from app.models.project import ProjectManager, ProjectStatus
from app.models.task import TaskManager, TaskStatus


def test_retry_retains_ambiguous_graph_and_sanitizes_the_next_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api.Config, "GRAPH_BACKEND", "local")
    project = ProjectManager.create_project("Synthetic recovery")
    project.status = ProjectStatus.FAILED
    project.ontology = {"entity_types": [{"name": "Person"}], "edge_types": []}
    project.graph_id = "retained-graph"
    project.zep_batch_id = "ambiguous-batch"
    project.error = "Prior write was not acknowledged"
    ProjectManager.save_extracted_text(project.project_id, "Avery works at Harbor Labs.")
    ProjectManager.save_project(project)
    targets = []

    class Thread:
        def __init__(self, *, target, daemon):
            targets.append(target)

        def start(self):
            pass

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def delete_graph(self, _graph_id):
            raise AssertionError("Retry must not delete the ambiguous graph")

        def validate_batch_chunks(self, *_args, **_kwargs):
            raise RuntimeError("PRIVATE UPSTREAM BODY")

    monkeypatch.setattr(graph_api.threading, "Thread", Thread)
    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={
            "project_id": project.project_id,
            "preserve_failed_graph": True,
        },
    ):
        response = graph_api.build_graph()
        assert response.status_code == 200
        task_id = response.json["data"]["task_id"]
        retained = ProjectManager.get_project(project.project_id)
        assert retained.graph_id is None
        assert retained.previous_graph_attempts[0]["graph_id"] == "retained-graph"
        assert retained.previous_graph_attempts[0]["batch_id"] == "ambiguous-batch"
        targets[0]()
    task = TaskManager().get_task(task_id)
    assert task.status == TaskStatus.FAILED
    assert "PRIVATE" not in task.error and "Traceback" not in task.error
    failed = ProjectManager.get_project(project.project_id)
    assert failed.error == task.error
    assert failed.previous_graph_attempts[0]["graph_id"] == "retained-graph"
