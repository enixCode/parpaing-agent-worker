"""PostgreSQL config store - profiles, engines, templates with in-memory cache."""

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

logger = logging.getLogger("tower.config_store")

VALID_TYPES = ("profile", "engine", "template")


class ConfigStore:
    """DB-backed config store with in-memory cache for fast sync reads."""

    _instance: "ConfigStore | None" = None

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        # Cache: (name, type) -> {name, type, content, description, created_at, updated_at}
        self._cache: dict[tuple[str, str], dict] = {}

    @classmethod
    async def init(cls, pool: asyncpg.Pool):
        """Initialize singleton and load all configs into memory."""
        cls._instance = cls(pool)
        await cls._instance._load_all()
        logger.info("ConfigStore initialized (%d configs cached)", len(cls._instance._cache))

    @classmethod
    def instance(cls) -> "ConfigStore":
        return cls._instance

    # --- Cache management ---

    async def _load_all(self):
        rows = await self._pool.fetch(
            "SELECT name, type, content, description, created_at, updated_at FROM configs"
        )
        self._cache = {
            (r["name"], r["type"]): {
                "name": r["name"], "type": r["type"],
                "content": r["content"], "description": r["description"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            }
            for r in rows
        }

    # --- Sync reads (from cache) ---

    def get(self, name: str, config_type: str) -> str | None:
        """Get raw content by name and type. Returns None if not found."""
        entry = self._cache.get((name, config_type))
        return entry["content"] if entry else None

    def get_full(self, name: str, config_type: str) -> dict | None:
        """Get full config entry (name, type, content, description, timestamps)."""
        return self._cache.get((name, config_type))

    def list_by_type(self, config_type: str) -> list[dict]:
        """List all configs of a given type (without content)."""
        return [
            {"name": v["name"], "type": v["type"], "description": v["description"],
             "created_at": v["created_at"], "updated_at": v["updated_at"]}
            for (_, t), v in sorted(self._cache.items(), key=lambda x: x[1]["name"])
            if t == config_type
        ]

    def list_all(self) -> list[dict]:
        """List all configs (without content)."""
        return [
            {"name": v["name"], "type": v["type"], "description": v["description"],
             "created_at": v["created_at"], "updated_at": v["updated_at"]}
            for v in sorted(self._cache.values(), key=lambda x: (x["type"], x["name"]))
        ]

    # --- Async writes (DB + cache) ---

    async def create(self, name: str, config_type: str, content: str, description: str = "") -> dict:
        """Insert a new config. Raises if already exists."""
        if (name, config_type) in self._cache:
            raise ValueError(f"Config already exists: {config_type}/{name}")

        now = datetime.now(timezone.utc)
        await self._pool.execute(
            """INSERT INTO configs (name, type, content, description, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $5)""",
            name, config_type, content, description, now,
        )
        entry = {
            "name": name, "type": config_type, "content": content,
            "description": description, "created_at": now, "updated_at": now,
        }
        self._cache[(name, config_type)] = entry
        logger.info("Config created: %s/%s", config_type, name)
        return entry

    async def update(self, name: str, config_type: str, content: str,
                     description: str | None = None) -> dict:
        """Update an existing config. Raises if not found."""
        if (name, config_type) not in self._cache:
            raise KeyError(f"Config not found: {config_type}/{name}")

        now = datetime.now(timezone.utc)
        if description is not None:
            await self._pool.execute(
                """UPDATE configs SET content = $1, description = $2, updated_at = $3
                   WHERE name = $4 AND type = $5""",
                content, description, now, name, config_type,
            )
        else:
            await self._pool.execute(
                """UPDATE configs SET content = $1, updated_at = $2
                   WHERE name = $3 AND type = $4""",
                content, now, name, config_type,
            )

        entry = self._cache[(name, config_type)]
        entry["content"] = content
        entry["updated_at"] = now
        if description is not None:
            entry["description"] = description
        logger.info("Config updated: %s/%s", config_type, name)
        return entry

    async def delete(self, name: str, config_type: str):
        """Delete a config. Raises if not found."""
        if (name, config_type) not in self._cache:
            raise KeyError(f"Config not found: {config_type}/{name}")

        await self._pool.execute(
            "DELETE FROM configs WHERE name = $1 AND type = $2", name, config_type,
        )
        del self._cache[(name, config_type)]
        logger.info("Config deleted: %s/%s", config_type, name)

    # --- Seed from files ---

    async def seed_from_files(self, profiles_dir: Path, engines_dir: Path,
                              templates_dir: Path):
        """Seed DB from disk files if configs table is empty (first startup)."""
        count = await self._pool.fetchval("SELECT count(*) FROM configs")
        if count > 0:
            logger.info("Configs table has %d rows, skipping seed", count)
            return

        seeded = 0

        # Profiles
        if profiles_dir.exists():
            for f in profiles_dir.glob("*.toml"):
                content = f.read_text(encoding="utf-8")
                desc = self._extract_toml_description(content)
                await self.create(f.stem, "profile", content, desc)
                seeded += 1

        # Engines
        if engines_dir.exists():
            for f in engines_dir.glob("*.toml"):
                content = f.read_text(encoding="utf-8")
                desc = self._extract_toml_description(content)
                await self.create(f.stem, "engine", content, desc)
                seeded += 1

        # Templates (preserve path-like names for profile references)
        if templates_dir.exists():
            for f in templates_dir.rglob("*.j2"):
                rel = f.relative_to(templates_dir).as_posix()
                content = f.read_text(encoding="utf-8")
                await self.create(rel, "template", content, f"Template: {rel}")
                seeded += 1

        logger.info("Seeded %d configs from disk files", seeded)

    @staticmethod
    def _extract_toml_description(content: str) -> str:
        """Extract description from TOML content (best-effort)."""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("description"):
                # description = "some text"
                parts = stripped.split("=", 1)
                if len(parts) == 2:
                    val = parts[1].strip().strip('"').strip("'")
                    return val
        return ""
