"""MiroFish's graph/batch interface backed by real local Graphiti storage.

This adapter covers the graph surface used by graph building, preparation,
reports and simulation memory. It does not pretend to implement Zep's unrelated
user/thread/account APIs. Local mode is explicit and never a Cloud fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock, Timeout

from ..config import Config
from .local_graph_runtime import LocalGraphRuntime, graph_edge, graph_node
from .local_graph_store import LocalGraphStore

logger = logging.getLogger(__name__)


def object_view(value):
    if isinstance(value, dict):
        return SimpleNamespace(
            **{
                key: (
                    item
                    if key in {"metadata", "attributes", "ontology"}
                    else object_view(item)
                )
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return [object_view(item) for item in value]
    return value


class LocalGraphClient:
    def __init__(self, directory=None):
        directory = Path(
            directory
            or os.getenv("LOCAL_GRAPH_DATA_DIR")
            or Path(Config.UPLOAD_FOLDER) / "local-graph"
        )
        self.store = LocalGraphStore(directory)
        self.runtime = LocalGraphRuntime(str(directory / "models"))
        self.graph = GraphApi(self)
        self.batch = BatchApi(self)
        self.worker_lock = FileLock(directory / "worker.lock")
        self.stopping = False
        self.init_future = asyncio.run_coroutine_threadsafe(
            self.runtime.initialize(), self.runtime.loop
        )
        self.worker_future = asyncio.run_coroutine_threadsafe(
            self._worker(), self.runtime.loop
        )

    def health(self):
        if not self.init_future.done():
            return {"ok": False, "backend": "local", "state": "initializing"}
        if self.init_future.exception() is not None:
            self.init_future = asyncio.run_coroutine_threadsafe(
                self.runtime.initialize(), self.runtime.loop
            )
            return {"ok": False, "backend": "local", "state": "initialization_failed"}
        try:
            self.runtime.call(self.runtime.query("health"), timeout=3)
            return {"ok": True, "backend": "local", "state": "ready"}
        except Exception:
            return {"ok": False, "backend": "local", "state": "storage_unreachable"}

    def groups(self):
        with self.store.db() as connection:
            return [row[0] for row in connection.execute("SELECT group_id FROM graphs")]

    async def _worker(self):
        # The OS lock survives neither process death nor container restart.
        # It prevents competing Flask/CLI processes from replaying shared jobs.
        acquired = False
        try:
            while not self.stopping:
                if not acquired:
                    try:
                        self.worker_lock.acquire(timeout=0)
                        acquired = True
                        self.store.recover_interrupted()
                    except Timeout:
                        await asyncio.sleep(1)
                        continue
                episode = self.store.claim()
                if episode is None:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    graph = self.store.graph(episode["graph_id"])
                    receipt = await self.runtime.ingest(
                        graph["group_id"], episode, json.loads(graph["ontology"])
                    )
                    if receipt.get("episode_uuid") != episode["uuid"]:
                        raise RuntimeError(
                            "Local graph ingestion returned a different episode identity"
                        )
                    self.store.finish(episode["uuid"])
                except asyncio.CancelledError:
                    self.store.finish(
                        episode["uuid"], "Local graph worker stopped during ingestion"
                    )
                    raise
                except Exception as error:
                    # Provider exceptions can include private request metadata.
                    logger.error(
                        "Local graph episode %s failed (%s)",
                        episode["uuid"],
                        type(error).__name__,
                    )
                    self.store.finish(
                        episode["uuid"],
                        "Semantic graph ingestion failed; check local graph service logs",
                    )
        finally:
            if acquired:
                self.worker_lock.release()

    def close(self):
        self.stopping = True
        self.worker_future.cancel()
        self.runtime.close()


class GraphApi:
    def __init__(self, client):
        self.client = client
        self.node = SimpleNamespace(
            get=self.get_node,
            get_edges=self.get_node_edges,
            with_raw_response=SimpleNamespace(get_by_graph_id=self.nodes_page),
        )
        self.edge = SimpleNamespace(
            with_raw_response=SimpleNamespace(get_by_graph_id=self.edges_page)
        )
        self.episode = SimpleNamespace(get=self.get_episode)

    def create(self, graph_id, name, description=""):
        if not graph_id or len(graph_id) > 128:
            raise ValueError("Graph ID must contain 1–128 characters")
        return object_view(self.client.store.create_graph(graph_id, name, description))

    def get(self, graph_id):
        return object_view(self.client.store.graph(graph_id))

    def delete(self, graph_id):
        graph = self.client.store.graph(graph_id)
        with self.client.store.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM episodes WHERE graph_id=? AND status IN ('queued','running')",
                (graph_id,),
            ).fetchone():
                raise ValueError("Cannot delete a graph while ingestion is active")
            self.client.runtime.call(
                self.client.runtime.query("delete_graph", group_id=graph["group_id"])
            )
            connection.execute("DELETE FROM graphs WHERE graph_id=?", (graph_id,))

    def set_ontology(self, *, graph_ids, entities, edges=None):
        def definition(model):
            return {
                "description": model.__doc__ or model.__name__,
                "properties": {
                    key: {"description": field.description or key}
                    for key, field in model.model_fields.items()
                },
            }

        ontology = {
            "entities": {name: definition(model) for name, model in entities.items()},
            "edges": {},
        }
        for name, (model, pairs) in (edges or {}).items():
            ontology["edges"][name] = {
                **definition(model),
                "source_targets": [
                    {"source": pair.source, "target": pair.target} for pair in pairs
                ],
            }
        for graph_id in graph_ids:
            self.client.store.set_ontology(graph_id, ontology)

    def add(
        self,
        *,
        graph_id,
        type,
        data,
        source_description="MiroFish activity",
        metadata=None,
        created_at=None,
    ):
        if type != "text":
            raise ValueError("MiroFish local graph ingestion supports text episodes")
        return object_view(
            self.client.store.add_episode(
                graph_id,
                data,
                source_description=source_description,
                metadata=metadata,
                created_at=created_at,
            )
        )

    def get_episode(self, uuid_):
        episode = self.client.store.episode(uuid_)
        if episode["status"] == "failed":
            raise RuntimeError(episode["error"] or "Local graph episode failed")
        return object_view(episode)

    def _page(self, graph_id, query, convert, limit, cursor):
        if not 1 <= limit <= 100:
            raise ValueError("Graph page limit must be between 1 and 100")
        offset = int(cursor or 0)
        if offset < 0:
            raise ValueError("Invalid graph cursor")
        group = self.client.store.graph(graph_id)["group_id"]
        rows = self.client.runtime.call(
            self.client.runtime.query(
                query,
                groups=[group],
                offset=offset,
                limit=limit + 1,
            )
        )
        headers = {"zep-next-cursor": str(offset + limit)} if len(rows) > limit else {}
        return SimpleNamespace(
            data=[convert(row) for row in rows[:limit]], headers=headers
        )

    def nodes_page(self, graph_id, *, limit=100, cursor=None):
        return self._page(graph_id, "nodes", graph_node, limit, cursor)

    def edges_page(self, graph_id, *, limit=100, cursor=None):
        return self._page(graph_id, "edges", graph_edge, limit, cursor)

    def get_node(self, uuid_):
        rows = self.client.runtime.call(
            self.client.runtime.query(
                "node",
                groups=self.client.groups(),
                uuid=uuid_,
                offset=0,
                limit=1,
            )
        )
        return graph_node(self.client.store.require(rows[0] if rows else None, "node"))

    def get_node_edges(self, node_uuid):
        # Both directions, with no silent 100-edge cap.
        rows = self.client.runtime.call(
            self.client.runtime.query(
                "node_edges", groups=self.client.groups(), uuid=node_uuid
            )
        )
        return [graph_edge(row) for row in rows]

    def search(
        self, *, graph_id, query, limit=10, scope="edges", reranker="cross_encoder"
    ):
        if scope not in {"nodes", "edges"} or not 1 <= limit <= 50 or not query.strip():
            raise ValueError("Invalid local graph search scope, query or limit")
        group = self.client.store.graph(graph_id)["group_id"]
        return self.client.runtime.call(
            self.client.runtime.search(group, query, scope, limit)
        )


class BatchApi:
    def __init__(self, client):
        self.client = client

    def create(self, *, metadata=None):
        return object_view(self.client.store.create_batch(metadata or {}))

    def get(self, batch_id):
        return object_view(self.client.store.batch(batch_id))

    def list(self, *, limit=100, cursor=None):
        if not 1 <= limit <= 100:
            raise ValueError("Invalid batch page limit")
        return object_view(self.client.store.list_batches(limit, cursor))

    def add(self, *, batch_id, items):
        if not 1 <= len(items) <= 350:
            raise ValueError("Batch submission requires 1–350 items")
        results = []
        for item in items:
            payload = (
                item.model_dump(exclude_none=True)
                if hasattr(item, "model_dump")
                else dict(item)
            )
            if (
                payload.get("type") != "graph_episode"
                or payload.get("data_type") != "text"
            ):
                raise ValueError("Local graph batches require text graph episodes")
            results.append(
                object_view(
                    self.client.store.add_episode(
                        payload["graph_id"],
                        payload["data"],
                        batch_id=batch_id,
                        source_description=payload.get(
                            "source_description", "MiroFish document"
                        ),
                        metadata=payload.get("metadata"),
                        created_at=payload.get("created_at"),
                    )
                )
            )
        return results

    def list_items(self, *, batch_id, limit=100, cursor=None):
        if not 1 <= limit <= 100:
            raise ValueError("Invalid batch item page limit")
        return object_view(self.client.store.list_items(batch_id, limit, cursor))

    def process(self, *, batch_id):
        return object_view(self.client.store.process_batch(batch_id))
