"""Unit tests for container pool - startup and network."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tower.pool import ContainerPool


async def _fake_to_thread(func, *args, **kwargs):
    """Simulate asyncio.to_thread by calling func synchronously."""
    return func(*args, **kwargs)


class TestPoolStartup:
    """Pool starts correctly."""

    @pytest.mark.asyncio
    async def test_pool_starts(self):
        pool = ContainerPool()
        db_pool = AsyncMock()
        db_pool.fetch = AsyncMock(return_value=[])
        db_pool.fetchval = AsyncMock(return_value=0)
        mock_net = MagicMock()
        mock_net.id = "net-123"
        mock_docker = MagicMock()
        mock_docker.networks.get.return_value = mock_net
        mock_docker.containers.list.return_value = []
        with patch("tower.pool.docker_client", return_value=mock_docker), \
             patch("tower.pool.asyncio.to_thread", side_effect=_fake_to_thread), \
             patch("tower.pool.POOL_SIZE", 0):
            await pool.start(db_pool)
            await pool.shutdown()
