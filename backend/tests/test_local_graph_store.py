"""Durable local ingestion semantics, without a fake semantic graph engine."""

from types import SimpleNamespace

import pytest
from zep_cloud import NotFoundError

from app.services.local_graph_store import LocalGraphStore
from app.services.local_graph_client import BatchApi, object_view


@pytest.fixture
def store(tmp_path):
    value = LocalGraphStore(tmp_path)
    value.create_graph("graph-a", "A", "First graph")
    return value


def test_missing_graph_is_a_real_not_found(store):
    with pytest.raises(NotFoundError):
        store.graph("missing")


def test_ontology_persists_across_reopen(store):
    store.set_ontology(
        "graph-a", {"entities": {"Vendor": {"description": "A supplier"}}}
    )
    reopened = LocalGraphStore(store.directory)
    assert (
        reopened.ontology("graph-a")["entities"]["Vendor"]["description"]
        == "A supplier"
    )


def test_batch_metadata_remains_a_mapping_for_reconciliation(store):
    api = BatchApi(SimpleNamespace(store=store))
    batch = api.create(
        metadata={"graph_id": "graph-a", "mirofish_operation_id": "operation-a"}
    )
    assert batch.metadata.get("mirofish_operation_id") == "operation-a"
    assert api.list().batches[0].metadata["graph_id"] == "graph-a"


def test_batch_processing_acknowledges_only_confirmed_semantic_writes(store):
    batch = store.create_batch({"graph_id": "graph-a"})
    episode = store.add_episode(
        "graph-a", "Avery works at Harbor Labs", batch_id=batch["batch_id"]
    )
    assert episode["processed"] is False
    store.process_batch(batch["batch_id"])
    claimed = store.claim()
    assert claimed["uuid"] == episode["uuid"] and claimed["status"] == "running"
    assert store.batch(batch["batch_id"])["status"] != "succeeded"
    store.finish(episode["uuid"])
    assert store.episode(episode["uuid"])["processed"] is True
    assert store.batch(batch["batch_id"])["status"] == "succeeded"
    assert store.batch(batch["batch_id"])["progress"]["percent_complete"] == 100


def test_interrupted_jobs_are_failed_without_ambiguous_replay(store):
    episode = store.add_episode("graph-a", "Avery works at Harbor Labs")
    store.claim()
    LocalGraphStore(store.directory).recover_interrupted()
    assert store.claim() is None
    failed = store.episode(episode["uuid"])
    assert failed["processed"] is False and failed["status"] == "failed"
    assert "interrupted" in failed["error"]


def test_batch_item_pagination_has_stable_sequence_and_cursor(store):
    batch = store.create_batch({})
    for index in range(5):
        store.add_episode(
            "graph-a", f"Event number {index}", batch_id=batch["batch_id"]
        )
    page = store.list_items(batch["batch_id"], 2, None)
    assert [item["sequence_index"] for item in page["items"]] == [0, 1]
    page = store.list_items(batch["batch_id"], 2, page["next_cursor"])
    assert [item["sequence_index"] for item in page["items"]] == [2, 3]
    page = store.list_items(batch["batch_id"], 2, page["next_cursor"])
    assert [item["sequence_index"] for item in page["items"]] == [4]
    assert page["next_cursor"] is None


def test_reprocessing_does_not_requeue_a_confirmed_episode(store):
    batch = store.create_batch({})
    episode = store.add_episode(
        "graph-a", "Harbor Labs purchases equipment", batch_id=batch["batch_id"]
    )
    store.process_batch(batch["batch_id"])
    store.claim()
    store.finish(episode["uuid"])
    store.process_batch(batch["batch_id"])
    assert store.claim() is None
    assert store.episode(episode["uuid"])["processed"] is True


def test_active_ontology_cannot_change_beneath_an_ingestion_job(store):
    store.add_episode("graph-a", "Harbor Labs purchases equipment")
    with pytest.raises(ValueError, match="active"):
        store.set_ontology("graph-a", {"entities": {}})


def test_empty_or_oversized_episode_is_rejected(store):
    for text in ("", " ", "a" * 10001):
        with pytest.raises(ValueError):
            store.add_episode("graph-a", text)


def test_attributes_and_metadata_keep_their_native_mapping_types():
    node = object_view(
        {"attributes": {"organization": "Harbor Labs"}, "metadata": {"source": "local"}}
    )
    assert node.attributes.get("organization") == "Harbor Labs"
    assert node.metadata["source"] == "local"
