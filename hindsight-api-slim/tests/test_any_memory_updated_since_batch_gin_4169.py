"""#4169: tagged batch staleness must use the GIN-friendly aggregate path."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from hindsight_api.engine.memories.base import MemoryScopeWatermark
from hindsight_api.engine.memories.pg import reads


class _FakeConn:
    backend_type = "postgresql"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any):
        self.queries.append(query)
        # One row per scope_key in the JSON payload — parse keys loosely.
        import json

        payload = json.loads(args[1])
        return [{"scope_key": item["scope_key"], "stale": False} for item in payload]


@pytest.mark.asyncio
async def test_tagged_batch_uses_bool_or_not_btree_walk():
    conn = _FakeConn()
    scopes = [
        MemoryScopeWatermark(
            key="mm-a",
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            tags=["project:example"],
            tags_match="all_strict",
        )
    ]
    out = await reads.any_memory_updated_since_batch(
        conn=conn,
        fq_table=lambda name: name,
        bank_id="bank",
        scopes=scopes,
    )
    assert out == {"mm-a": False}
    assert len(conn.queries) == 1
    sql = conn.queries[0]
    assert "bool_or(mu.updated_at > s.since)" in sql
    assert "ORDER BY mu.updated_at DESC" not in sql
    assert "LIMIT 1" not in sql


@pytest.mark.asyncio
async def test_untagged_batch_keeps_btree_early_exit():
    conn = _FakeConn()
    scopes = [
        MemoryScopeWatermark(
            key="mm-b",
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            tags=None,
            tags_match="any",
        )
    ]
    out = await reads.any_memory_updated_since_batch(
        conn=conn,
        fq_table=lambda name: name,
        bank_id="bank",
        scopes=scopes,
    )
    assert out == {"mm-b": False}
    sql = conn.queries[0]
    assert "ORDER BY mu.updated_at DESC" in sql
    assert "LIMIT 1" in sql
    assert "bool_or" not in sql


@pytest.mark.asyncio
async def test_four_tag_modes_all_use_gin_path_when_tagged():
    conn = _FakeConn()
    scopes = [
        MemoryScopeWatermark(
            key=mode,
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            tags=["a", "b"],
            tags_match=mode,
        )
        for mode in ("any", "all", "any_strict", "all_strict")
    ]
    out = await reads.any_memory_updated_since_batch(
        conn=conn,
        fq_table=lambda name: name,
        bank_id="bank",
        scopes=scopes,
    )
    assert set(out) == {"any", "all", "any_strict", "all_strict"}
    # Modes with different clauses may split into multiple group queries.
    assert conn.queries
    for sql in conn.queries:
        assert "bool_or(mu.updated_at > s.since)" in sql
        assert "ORDER BY mu.updated_at DESC" not in sql
