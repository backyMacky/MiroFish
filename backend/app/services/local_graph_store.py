"""Durable local graph catalog, ontology, batches and ingestion acknowledgements."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from zep_cloud import NotFoundError


def now():
    return datetime.now(timezone.utc).isoformat()


class LocalGraphStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "catalog.db"
        with self.db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS graphs (
                    graph_id TEXT PRIMARY KEY, group_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL,
                    ontology TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    uuid TEXT PRIMARY KEY, graph_id TEXT NOT NULL,
                    batch_id TEXT, sequence_index INTEGER,
                    data TEXT NOT NULL, source_description TEXT NOT NULL,
                    metadata TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft', error TEXT,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE,
                    FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
                    UNIQUE(batch_id, sequence_index)
                );
                CREATE INDEX IF NOT EXISTS episodes_queue ON episodes(status, created_at);
                CREATE INDEX IF NOT EXISTS episodes_graph ON episodes(graph_id);
                CREATE INDEX IF NOT EXISTS episodes_batch ON episodes(batch_id);
            """
            )

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def require(row, label):
        if row is None:
            raise NotFoundError(body={"message": f"Local graph {label} not found"})
        return dict(row)

    def create_graph(self, graph_id, name, description):
        # Separate Graphiti namespace from every other application graph.
        group_id = "mirofish_local_" + hashlib.sha256(graph_id.encode()).hexdigest()
        with self.db() as connection:
            connection.execute(
                "INSERT INTO graphs(graph_id,group_id,name,description,created_at) "
                "VALUES(?,?,?,?,?)",
                (graph_id, group_id, name, description, now()),
            )
        return self.graph(graph_id)

    def graph(self, graph_id):
        with self.db() as connection:
            return self.require(
                connection.execute(
                    "SELECT * FROM graphs WHERE graph_id=?", (graph_id,)
                ).fetchone(),
                "catalog entry",
            )

    def ontology(self, graph_id):
        return json.loads(self.graph(graph_id)["ontology"])

    def set_ontology(self, graph_id, ontology):
        self.graph(graph_id)
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM episodes WHERE graph_id=? AND status IN ('queued','running')",
                (graph_id,),
            ).fetchone():
                raise ValueError(
                    "Cannot change ontology while graph ingestion is active"
                )
            connection.execute(
                "UPDATE graphs SET ontology=? WHERE graph_id=?",
                (json.dumps(ontology), graph_id),
            )

    def create_batch(self, metadata):
        batch_id = str(uuid.uuid4())
        with self.db() as connection:
            connection.execute(
                "INSERT INTO batches(batch_id,metadata,created_at) VALUES(?,?,?)",
                (batch_id, json.dumps(metadata), now()),
            )
        return self.batch(batch_id)

    def batch(self, batch_id):
        with self.db() as connection:
            batch = self.require(
                connection.execute(
                    "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
                ).fetchone(),
                "batch",
            )
            counts = dict(
                connection.execute(
                    "SELECT status,count(*) FROM episodes WHERE batch_id=? GROUP BY status",
                    (batch_id,),
                ).fetchall()
            )
        total = sum(counts.values())
        done = counts.get("succeeded", 0)
        failed = counts.get("failed", 0)
        if batch["status"] != "draft":
            batch["status"] = (
                "succeeded"
                if total and done == total
                else "failed" if failed and done + failed == total else "processing"
            )
        batch["metadata"] = json.loads(batch["metadata"])
        batch["progress"] = {
            "succeeded_items": done,
            "failed_items": failed,
            "percent_complete": 100 * (done + failed) / total if total else 0,
        }
        return batch

    def list_batches(self, limit, cursor):
        offset = int(cursor or 0)
        if offset < 0:
            raise ValueError("Invalid batch cursor")
        with self.db() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM batches ORDER BY rowid LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
        return {
            "batches": [self.batch(row[0]) for row in rows[:limit]],
            "next_cursor": offset + limit if len(rows) > limit else None,
        }

    def add_episode(
        self,
        graph_id,
        data,
        *,
        batch_id=None,
        source_description="MiroFish episode",
        metadata=None,
        created_at=None,
    ):
        if not isinstance(data, str) or not data.strip() or len(data) > 10_000:
            raise ValueError("Episodes require 1–10,000 characters of text")
        self.graph(graph_id)
        episode_id = str(uuid.uuid4())
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence = None
            if batch_id:
                batch = self.require(
                    connection.execute(
                        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
                    ).fetchone(),
                    "batch",
                )
                if batch["status"] != "draft":
                    raise ValueError("Cannot add to a batch after processing starts")
                sequence = connection.execute(
                    "SELECT count(*) FROM episodes WHERE batch_id=?", (batch_id,)
                ).fetchone()[0]
            connection.execute(
                "INSERT INTO episodes(uuid,graph_id,batch_id,sequence_index,data,source_description,"
                "metadata,created_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    episode_id,
                    graph_id,
                    batch_id,
                    sequence,
                    data,
                    source_description,
                    json.dumps(metadata or {}),
                    created_at or now(),
                    "draft" if batch_id else "queued",
                ),
            )
        return self.episode(episode_id)

    def episode(self, episode_id):
        with self.db() as connection:
            episode = self.require(
                connection.execute(
                    "SELECT * FROM episodes WHERE uuid=?", (episode_id,)
                ).fetchone(),
                "episode",
            )
        episode["processed"] = episode["status"] == "succeeded"
        episode["episode_uuid"] = episode_id
        episode["source_uuid"] = episode_id
        episode["uuid_"] = episode_id
        episode["metadata"] = json.loads(episode["metadata"])
        return episode

    def list_items(self, batch_id, limit, cursor):
        self.batch(batch_id)
        offset = int(cursor or 0)
        if offset < 0:
            raise ValueError("Invalid item cursor")
        with self.db() as connection:
            ids = connection.execute(
                "SELECT uuid FROM episodes WHERE batch_id=? ORDER BY sequence_index LIMIT ? OFFSET ?",
                (batch_id, limit + 1, offset),
            ).fetchall()
        return {
            "items": [self.episode(row[0]) for row in ids[:limit]],
            "next_cursor": offset + limit if len(ids) > limit else None,
        }

    def process_batch(self, batch_id):
        self.batch(batch_id)
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM episodes WHERE batch_id=?", (batch_id,)
            ).fetchone():
                raise ValueError("Cannot process an empty batch")
            connection.execute(
                "UPDATE episodes SET status='queued' WHERE batch_id=? AND status='draft'",
                (batch_id,),
            )
            connection.execute(
                "UPDATE batches SET status='processing' WHERE batch_id=? AND status='draft'",
                (batch_id,),
            )
        return self.batch(batch_id)

    def recover_interrupted(self):
        # A lost response after partial semantic extraction is ambiguous. Never
        # replay it automatically and never acknowledge it as processed.
        with self.db() as connection:
            connection.execute(
                "UPDATE episodes SET status='failed',error=? WHERE status='running'",
                (
                    "Ingestion interrupted; review or rebuild this graph before retrying",
                ),
            )

    def claim(self):
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT uuid FROM episodes WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE episodes SET status='running' WHERE uuid=?", (row[0],)
            )
        return self.episode(row[0])

    def finish(self, episode_id, error=None):
        with self.db() as connection:
            connection.execute(
                "UPDATE episodes SET status=?,error=? WHERE uuid=?",
                ("failed" if error else "succeeded", error, episode_id),
            )
