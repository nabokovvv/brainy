"""User settings persistence stub for Stage 4 (SQLite).

In-memory implementation for Stage 0/1; SQLite schema ready for Stage 4.
No user content is stored — only per-chat preferences.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS user_settings (
    chat_id INTEGER PRIMARY KEY,
    web_enabled INTEGER NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT 'en',
    persona TEXT NOT NULL DEFAULT 'assistant',
    updated_at REAL NOT NULL
);
"""

MIGRATION_SQL: dict[int, str] = {
    2: """
    ALTER TABLE user_settings ADD COLUMN persona TEXT NOT NULL DEFAULT 'assistant';
    """,
}


@dataclass(frozen=True, slots=True)
class UserSettings:
    chat_id: int
    web_enabled: bool
    language: str
    persona: str
    updated_at: float


class UserSettingsRepo:
    """In-memory repository; swap for SQLite in Stage 4."""

    def __init__(self) -> None:
        self._store: dict[int, UserSettings] = {}

    def get(self, chat_id: int) -> Optional[UserSettings]:
        return self._store.get(chat_id)

    def upsert(
        self,
        chat_id: int,
        *,
        web_enabled: Optional[bool] = None,
        language: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> UserSettings:
        now = time.time()
        existing = self._store.get(chat_id)
        if existing is None:
            settings = UserSettings(
                chat_id=chat_id,
                web_enabled=web_enabled if web_enabled is not None else False,
                language=language if language is not None else "en",
                persona=persona if persona is not None else "assistant",
                updated_at=now,
            )
        else:
            settings = UserSettings(
                chat_id=chat_id,
                web_enabled=web_enabled if web_enabled is not None else existing.web_enabled,
                language=language if language is not None else existing.language,
                persona=persona if persona is not None else existing.persona,
                updated_at=now,
            )
        self._store[chat_id] = settings
        return settings

    def delete(self, chat_id: int) -> bool:
        return self._store.pop(chat_id, None) is not None

    def all(self) -> list[UserSettings]:
        return list(self._store.values())


class SQLiteUserSettingsRepo:
    """SQLite-backed repository (Stage 4). Not used until Stage 4 migration."""

    def __init__(self, path: str) -> None:
        import sqlite3

        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        cur = self._conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            # Fresh database: schema is already at the latest version.
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()
            return
        current_version = row[0]
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version in MIGRATION_SQL:
                self._conn.executescript(MIGRATION_SQL[version])
        if current_version < SCHEMA_VERSION:
            self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        self._conn.commit()

    def _run_migrations(self) -> None:
        cur = self._conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version in MIGRATION_SQL:
                self._conn.executescript(MIGRATION_SQL[version])
        if current_version < SCHEMA_VERSION:
            self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        self._conn.commit()

    def get(self, chat_id: int) -> Optional[UserSettings]:
        cur = self._conn.execute(
            "SELECT chat_id, web_enabled, language, persona, updated_at FROM user_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return UserSettings(
            chat_id=row[0],
            web_enabled=bool(row[1]),
            language=row[2],
            persona=row[3],
            updated_at=row[4],
        )

    def upsert(
        self,
        chat_id: int,
        *,
        web_enabled: Optional[bool] = None,
        language: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> UserSettings:
        now = time.time()
        existing = self.get(chat_id)
        if existing is None:
            settings = UserSettings(
                chat_id=chat_id,
                web_enabled=web_enabled if web_enabled is not None else False,
                language=language if language is not None else "en",
                persona=persona if persona is not None else "assistant",
                updated_at=now,
            )
        else:
            settings = UserSettings(
                chat_id=chat_id,
                web_enabled=web_enabled if web_enabled is not None else existing.web_enabled,
                language=language if language is not None else existing.language,
                persona=persona if persona is not None else existing.persona,
                updated_at=now,
            )
        self._conn.execute(
            """
            INSERT INTO user_settings (chat_id, web_enabled, language, persona, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                web_enabled = excluded.web_enabled,
                language = excluded.language,
                persona = excluded.persona,
                updated_at = excluded.updated_at
            """,
            (
                settings.chat_id,
                int(settings.web_enabled),
                settings.language,
                settings.persona,
                settings.updated_at,
            ),
        )
        self._conn.commit()
        return settings

    def delete(self, chat_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM user_settings WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def all(self) -> list[UserSettings]:
        cur = self._conn.execute(
            "SELECT chat_id, web_enabled, language, persona, updated_at FROM user_settings"
        )
        return [
            UserSettings(
                chat_id=r[0], web_enabled=bool(r[1]), language=r[2], persona=r[3], updated_at=r[4]
            )
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteUserSettingsRepo":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class AsyncUserSettingsRepo:
    """Serialize SQLite access off the event loop over one shared connection."""

    def __init__(self, repo: SQLiteUserSettingsRepo) -> None:
        self._repo = repo
        self._lock = asyncio.Lock()

    async def get(self, chat_id: int) -> Optional[UserSettings]:
        async with self._lock:
            return await asyncio.to_thread(self._repo.get, chat_id)

    async def upsert(
        self,
        chat_id: int,
        *,
        web_enabled: Optional[bool] = None,
        language: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> UserSettings:
        async with self._lock:
            return await asyncio.to_thread(
                self._repo.upsert,
                chat_id,
                web_enabled=web_enabled,
                language=language,
                persona=persona,
            )

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._repo.close)


def open_repo(path: Optional[str] = None) -> UserSettingsRepo:
    """Factory for the active repository. In-memory by default."""
    if path is None:
        return UserSettingsRepo()
    return SQLiteUserSettingsRepo(path)


__all__ = [
    "UserSettings",
    "UserSettingsRepo",
    "SQLiteUserSettingsRepo",
    "AsyncUserSettingsRepo",
    "open_repo",
    "SCHEMA_VERSION",
    "SCHEMA_SQL",
]
