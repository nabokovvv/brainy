"""Tests for storage module (UserSettingsRepo)."""

from __future__ import annotations

import tempfile
import unittest

from storage import (
    AsyncUserSettingsRepo,
    SCHEMA_VERSION,
    SCHEMA_SQL,
    SQLiteUserSettingsRepo,
    UserSettingsRepo,
    open_repo,
)


class UserSettingsRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = UserSettingsRepo()

    def test_default_settings_are_created(self) -> None:
        settings = self.repo.upsert(chat_id=123)

        self.assertEqual(settings.chat_id, 123)
        self.assertFalse(settings.web_enabled)
        self.assertEqual(settings.language, "en")
        self.assertIsInstance(settings.updated_at, float)

    def test_get_returns_none_for_missing_chat(self) -> None:
        self.assertIsNone(self.repo.get(999))

    def test_upsert_updates_web_enabled(self) -> None:
        self.repo.upsert(123, web_enabled=False)

        updated = self.repo.upsert(123, web_enabled=True)

        self.assertTrue(updated.web_enabled)

    def test_upsert_updates_language(self) -> None:
        self.repo.upsert(123, language="en")

        updated = self.repo.upsert(123, language="ru")

        self.assertEqual(updated.language, "ru")

    def test_upsert_preserves_unset_fields(self) -> None:
        self.repo.upsert(123, web_enabled=True, language="de")

        updated = self.repo.upsert(123, language="fr")

        self.assertTrue(updated.web_enabled)
        self.assertEqual(updated.language, "fr")

    def test_delete_removes_existing_chat(self) -> None:
        self.repo.upsert(123)

        self.assertTrue(self.repo.delete(123))
        self.assertIsNone(self.repo.get(123))

    def test_delete_returns_false_for_missing_chat(self) -> None:
        self.assertFalse(self.repo.delete(999))

    def test_all_returns_all_chats(self) -> None:
        self.repo.upsert(1)
        self.repo.upsert(2)
        self.repo.upsert(3)

        all_settings = self.repo.all()

        self.assertEqual(len(all_settings), 3)
        chat_ids = {s.chat_id for s in all_settings}
        self.assertEqual(chat_ids, {1, 2, 3})

    def test_isolation_per_chat(self) -> None:
        self.repo.upsert(1, web_enabled=True, language="en")
        self.repo.upsert(2, web_enabled=False, language="ru")

        self.assertTrue(self.repo.get(1).web_enabled)
        self.assertFalse(self.repo.get(2).web_enabled)
        self.assertEqual(self.repo.get(1).language, "en")
        self.assertEqual(self.repo.get(2).language, "ru")


class SQLiteUserSettingsRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.repo = SQLiteUserSettingsRepo(self.tmp.name)

    def tearDown(self) -> None:
        self.repo.close()
        import os

        os.unlink(self.tmp.name)

    def test_schema_version_created(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self.tmp.name)
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], SCHEMA_VERSION)

    def test_schema_sql_creates_user_settings_table(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self.tmp.name)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'"
        )
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_crud_operations_persist(self) -> None:
        self.repo.upsert(100, web_enabled=True, language="es")
        settings = self.repo.get(100)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.chat_id, 100)
        self.assertTrue(settings.web_enabled)
        self.assertEqual(settings.language, "es")

    def test_upsert_overwrites_existing(self) -> None:
        self.repo.upsert(100, web_enabled=False)
        updated = self.repo.upsert(100, web_enabled=True)

        self.assertTrue(updated.web_enabled)

    def test_delete_removes_from_db(self) -> None:
        self.repo.upsert(100)
        self.repo.delete(100)

        self.assertIsNone(self.repo.get(100))

    def test_all_returns_all_rows(self) -> None:
        self.repo.upsert(1)
        self.repo.upsert(2)

        all_settings = self.repo.all()

        self.assertEqual(len(all_settings), 2)

    def test_context_manager_closes_connection(self) -> None:
        with SQLiteUserSettingsRepo(self.tmp.name) as repo:
            repo.upsert(1)
        # Should not raise
        self.assertTrue(True)

    def test_persists_across_new_connection(self) -> None:
        self.repo.upsert(200, web_enabled=True, language="fr")
        self.repo.close()

        new_repo = SQLiteUserSettingsRepo(self.tmp.name)
        settings = new_repo.get(200)
        new_repo.close()

        self.assertIsNotNone(settings)
        self.assertTrue(settings.web_enabled)
        self.assertEqual(settings.language, "fr")

    def test_wal_mode_enabled(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self.tmp.name)
        cur = conn.execute("PRAGMA journal_mode")
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0].lower(), "wal")

    def test_foreign_keys_enabled(self) -> None:
        # foreign_keys is per-connection; verify on repo's connection
        cur = self.repo._conn.execute("PRAGMA foreign_keys")
        row = cur.fetchone()
        self.assertEqual(row[0], 1)


class AsyncUserSettingsRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.repo = AsyncUserSettingsRepo(SQLiteUserSettingsRepo(self.tmp.name))

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        import os

        os.unlink(self.tmp.name)

    async def test_serialized_repo_persists_preferences(self) -> None:
        await self.repo.upsert(42, language="ru", web_enabled=True)

        settings = await self.repo.get(42)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.language, "ru")
        self.assertTrue(settings.web_enabled)


class OpenRepoFactoryTests(unittest.TestCase):
    def test_returns_in_memory_by_default(self) -> None:
        repo = open_repo()

        self.assertIsInstance(repo, UserSettingsRepo)
        self.assertNotIsInstance(repo, SQLiteUserSettingsRepo)

    def test_returns_sqlite_when_path_given(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        try:
            repo = open_repo(tmp_path)
            self.assertIsInstance(repo, SQLiteUserSettingsRepo)
            repo.close()
        finally:
            import os

            os.unlink(tmp_path)


class MigrationStubTests(unittest.TestCase):
    def test_migration_dict_empty_for_current_version(self) -> None:
        from storage import MIGRATION_SQL

        self.assertEqual(MIGRATION_SQL, {})

    def test_schema_version_constant_matches_sql(self) -> None:
        import re

        match = re.search(r"VALUES\s*\((\d+)\)", SCHEMA_SQL)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
