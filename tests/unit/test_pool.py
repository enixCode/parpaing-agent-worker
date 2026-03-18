"""Unit tests for container pool - gateway validation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tower.pool import ContainerPool


async def _fake_to_thread(func, *args, **kwargs):
    """Simulate asyncio.to_thread by calling func synchronously."""
    return func(*args, **kwargs)


class TestGatewayValidation:
    """Pool refuses to start if GATEWAY_URL is set without WORKER_HARDENED."""

    @pytest.mark.asyncio
    async def test_gateway_without_hardened_raises(self):
        pool = ContainerPool()
        db_pool = AsyncMock()
        with patch("tower.pool.GATEWAY_URL", "http://gateway:4000"), \
             patch("tower.pool.WORKER_HARDENED", False):
            with pytest.raises(RuntimeError, match="GATEWAY_URL requires WORKER_HARDENED"):
                await pool.start(db_pool)

    @pytest.mark.asyncio
    async def test_gateway_with_hardened_passes_validation(self):
        pool = ContainerPool()
        db_pool = AsyncMock()
        db_pool.fetch = AsyncMock(return_value=[])
        db_pool.fetchval = AsyncMock(return_value=0)
        mock_net = MagicMock()
        mock_net.id = "net-123"
        mock_docker = MagicMock()
        mock_docker.networks.get.side_effect = Exception("not found")
        mock_docker.networks.create.return_value = mock_net
        mock_docker.containers.list.return_value = []
        mock_docker.containers.run.return_value = MagicMock(id="c-abc")
        mock_docker.containers.get.return_value = MagicMock()
        with patch("tower.pool.GATEWAY_URL", "http://gateway:4000"), \
             patch("tower.pool.WORKER_HARDENED", True), \
             patch("tower.pool.docker_client", mock_docker), \
             patch("tower.pool.asyncio.to_thread", side_effect=_fake_to_thread), \
             patch("tower.pool.POOL_SIZE", 0):
            await pool.start(db_pool)
            # No RuntimeError raised - validation passed
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_no_gateway_no_validation(self):
        pool = ContainerPool()
        db_pool = AsyncMock()
        db_pool.fetch = AsyncMock(return_value=[])
        db_pool.fetchval = AsyncMock(return_value=0)
        mock_net = MagicMock()
        mock_net.id = "net-123"
        mock_docker = MagicMock()
        mock_docker.networks.get.side_effect = Exception("not found")
        mock_docker.networks.create.return_value = mock_net
        mock_docker.containers.list.return_value = []
        with patch("tower.pool.GATEWAY_URL", ""), \
             patch("tower.pool.WORKER_HARDENED", False), \
             patch("tower.pool.docker_client", mock_docker), \
             patch("tower.pool.asyncio.to_thread", side_effect=_fake_to_thread), \
             patch("tower.pool.POOL_SIZE", 0):
            await pool.start(db_pool)
            # No RuntimeError - direct mode works without hardening
            await pool.shutdown()
