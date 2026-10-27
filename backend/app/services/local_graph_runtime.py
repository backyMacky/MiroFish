"""Authenticated RPC to the isolated local Graphiti service (no Cloud fallback)."""

from __future__ import annotations
import asyncio
import ipaddress
import math
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import httpx


def graph_node(data):
    properties = dict(data.get("properties") or {})
    reserved = {"uuid", "name", "group_id", "summary", "created_at", "name_embedding"}
    return SimpleNamespace(
        uuid_=data["uuid"],
        name=data.get("name", ""),
        labels=list(data.get("labels") or []),
        summary=data.get("summary") or "",
        created_at=str(data.get("created_at") or ""),
        attributes={
            key: value for key, value in properties.items() if key not in reserved
        },
    )


def graph_edge(data):
    properties = dict(data.get("properties") or {})
    reserved = {
        "uuid",
        "group_id",
        "name",
        "fact",
        "fact_embedding",
        "episodes",
        "created_at",
        "valid_at",
        "invalid_at",
        "expired_at",
        "reference_time",
    }
    return SimpleNamespace(
        uuid_=data["uuid"],
        name=data.get("name") or "",
        fact=data.get("fact") or "",
        fact_type=data.get("name") or "",
        source_node_uuid=data["source_node_uuid"],
        target_node_uuid=data["target_node_uuid"],
        episodes=list(data.get("episodes") or []),
        created_at=str(data.get("created_at") or ""),
        valid_at=data.get("valid_at"),
        invalid_at=data.get("invalid_at"),
        expired_at=data.get("expired_at"),
        attributes={
            key: value for key, value in properties.items() if key not in reserved
        },
    )


class LocalGraphRuntime:
    def __init__(self, cache_dir=None):
        self.base_url = os.getenv(
            "LOCAL_GRAPH_URL", "http://mirofish-local-graph:8064"
        ).rstrip("/")
        address = urlparse(self.base_url)
        local = address.hostname in {
            "mirofish-local-graph",
            "localhost",
            "127.0.0.1",
            "::1",
        }
        try:
            local = local or ipaddress.ip_address(address.hostname).is_private
        except ValueError:
            pass
        if (
            address.scheme != "http"
            or not local
            or address.username
            or address.password
        ):
            raise ValueError(
                "LOCAL_GRAPH_URL must target the internal local graph service"
            )
        key_file = os.getenv("LOCAL_GRAPH_API_KEY_FILE")
        key = (
            Path(key_file).read_text().strip()
            if key_file
            else os.getenv("LOCAL_GRAPH_API_KEY", "")
        )
        if not key:
            raise ValueError("Local graph service authentication is not configured")
        self.ingestion_timeout = float(os.getenv('LOCAL_GRAPH_INGESTION_WAIT_TIMEOUT_SECONDS', '600'))
        if not math.isfinite(self.ingestion_timeout) or not 1 <= self.ingestion_timeout <= 7200:
            raise ValueError('Local graph ingestion timeout must be from 1 to 7200 seconds')
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="mirofish-local-graph-rpc", daemon=True
        )
        self.thread.start()
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=600,
            trust_env=False,
        )

    def call(self, coroutine, timeout=120):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError("Local graph operation exceeded its deadline") from None

    async def _post(self, path, payload, timeout=120):
        response = await self.client.post(path, json=payload, timeout=timeout)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Local graph service rejected the operation (HTTP {response.status_code})"
            )
        return response.json()

    async def initialize(self):
        response = await self.client.get("/health", timeout=5)
        response.raise_for_status()
        return response.json()

    async def ingest(self, group_id, episode, ontology):
        return await self._post(
            "/ingest",
            {"group_id": group_id, "episode": episode, "ontology": ontology},
            timeout=self.ingestion_timeout,
        )

    async def query(self, operation, **parameters):
        return await self._post(
            "/query", {"operation": operation, **parameters}, timeout=30
        )

    async def search(self, group_id, query, scope, limit):
        data = await self._post(
            "/search",
            {"group_id": group_id, "query": query, "scope": scope, "limit": limit},
        )
        return SimpleNamespace(
            nodes=[SimpleNamespace(**item) for item in data["nodes"]],
            edges=[SimpleNamespace(**item) for item in data["edges"]],
        )

    def close(self):
        self.call(self.client.aclose())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
