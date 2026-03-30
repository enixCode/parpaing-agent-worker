"""Unit tests for container pool - startup and network."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tower.store import ContainerPool


class TestPoolStartup:
    """Pool starts correctly with a Runtime instance."""

    @pytest.mark.asyncio
    async def test_pool_starts(self):
        runtime = AsyncMock()
        runtime.ensure_network = AsyncMock(return_value="net-123")
        runtime.worker_alive = AsyncMock(return_value=False)
        runtime.list_orphan_workers = AsyncMock(return_value=[])

        pool = ContainerPool(runtime)
        db_pool = AsyncMock()
        db_pool.fetch = AsyncMock(return_value=[])
        db_pool.fetchval = AsyncMock(return_value=0)

        with patch("tower.store.pool.POOL_SIZE", 0):
            await pool.start(db_pool)
            await pool.shutdown()

        runtime.ensure_network.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_exposes_runtime(self):
        runtime = MagicMock()
        pool = ContainerPool(runtime)
        assert pool.runtime is runtime
