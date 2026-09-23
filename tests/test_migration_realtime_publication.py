"""Schema tests for the realtime-publication migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → create supabase_realtime publication →
    seed rows → target migration → later migrations

The no-publication (plain PostgreSQL) path is covered indirectly: every
earlier migration test applies this file in its "after" chain, where the
guarded block must be a no-op.

Skips (does not fail) when no local PostgreSQL server is reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import psycopg.rows
import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
TARGET_MIGRATION = "20260923170000_enable_realtime_assistance_requests.sql"

PUBLICATION_NAME = "supabase_realtime"
SEED_CALL_ID = "CA_SEED_REALTIME"


@dataclass
class MigrationDb:
    conn: psycopg.Connection[dict[str, Any]]
    target_sql: str
    row_count_before_target: int
    row_count_after_target: int


def _migration_split() -> tuple[list[Path], Path, list[Path]]:
    """Return (before, target, after) migration files in application order."""
    target = MIGRATIONS_DIR / TARGET_MIGRATION
    if not target.exists():
        raise RuntimeError(f"target migration missing: {target}")
    before: list[Path] = []
    after: list[Path] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < target.name:
            before.append(path)
        elif path.name > target.name:
            after.append(path)
    return before, target, after


def _seed_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           intake_status, hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_realtime', '+15550000010', '42 River Rd', 'Ford F-150',
           'Flat tire', 'completed', 'completed', false, null, 'sent')
        """,
        (SEED_CALL_ID,),
    )


def _row_count(conn: psycopg.Connection[dict[str, Any]]) -> int:
    row = conn.execute("select count(*) as n from assistance_requests").fetchone()
    assert row is not None
    return int(row["n"])


@pytest.fixture(scope="module")
def migration_db() -> Iterator[MigrationDb]:
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True, connect_timeout=3)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    dbname = f"roadside_realtime_pub_test_{uuid.uuid4().hex[:10]}"
    conn: psycopg.Connection[dict[str, Any]] | None = None
    try:
        admin.execute(f'create database "{dbname}"')
        conn = psycopg.connect(
            f"dbname={dbname}",
            autocommit=True,
            row_factory=psycopg.rows.dict_row,
        )
        before, target, after = _migration_split()
        for path in before:
            conn.execute(path.read_text())
        # Simulate the Supabase-managed publication that always exists on a
        # hosted project; plain SQL migrations never create it themselves.
        conn.execute(f'create publication "{PUBLICATION_NAME}"')
        _seed_rows(conn)
        row_count_before = _row_count(conn)
        target_sql = target.read_text()
        conn.execute(target_sql)
        row_count_after = _row_count(conn)
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            conn=conn,
            target_sql=target_sql,
            row_count_before_target=row_count_before,
            row_count_after_target=row_count_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


class TestPublicationMembership:
    """assistance_requests is streamed by Supabase Realtime after the migration."""

    def test_table_added_to_realtime_publication(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_publication_tables
            where pubname = %s
              and schemaname = 'public'
              and tablename = 'assistance_requests'
            """,
            (PUBLICATION_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_migration_is_idempotent(self, migration_db: MigrationDb) -> None:
        migration_db.conn.execute(migration_db.target_sql)
        row = migration_db.conn.execute(
            "select count(*) as n from pg_publication_tables where pubname = %s",
            (PUBLICATION_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1


class TestDataUntouched:
    """Publication membership is catalog-only: no row changes."""

    def test_row_count_unchanged(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target

    def test_seed_row_intact(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, status, intake_status, notification_status
            from assistance_requests where call_id = %s
            """,
            (SEED_CALL_ID,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "42 River Rd"
        assert row["vehicle"] == "Ford F-150"
        assert row["issue"] == "Flat tire"
        assert row["status"] == "completed"
        assert row["intake_status"] == "completed"
        assert row["notification_status"] == "sent"


class TestSecurityUnchanged:
    """Realtime membership adds no read/write surface beyond existing RLS."""

    def test_row_level_security_still_enabled(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select relrowsecurity from pg_class where relname = 'assistance_requests'"
        ).fetchone()
        assert row is not None
        assert row["relrowsecurity"] is True

    def test_no_write_policy_added_for_authenticated(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_policies
            where tablename = 'assistance_requests'
              and cmd in ('INSERT', 'UPDATE', 'DELETE')
              and 'authenticated' = any(roles)
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0
