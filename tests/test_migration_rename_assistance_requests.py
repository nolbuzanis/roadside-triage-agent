"""Schema tests for the breakdown_tickets → assistance_requests rename migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed historical rows → target migration → later migrations

Asserts the rename is a pure metadata change: row count, ids, and stored data
are unchanged, while the PK, unique call_id, check constraint, and RLS
deny-all policy all move to the renamed table under new constraint names.

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
TARGET_MIGRATION = "20260923000000_rename_breakdown_tickets_to_assistance_requests.sql"

SEED_CALL_COMPLETED = "CA_SEED_RENAME_COMPLETED"
SEED_CALL_ESCALATED = "CA_SEED_RENAME_ESCALATED"

EXPECTED_CONSTRAINTS = (
    "assistance_requests_pkey",
    "assistance_requests_call_id_key",
    "assistance_requests_intake_status_check",
)


@dataclass
class MigrationDb:
    admin: psycopg.Connection[tuple[Any, ...]]
    conn: psycopg.Connection[dict[str, Any]]
    dbname: str
    rows_before: list[dict[str, Any]]
    rows_after: list[dict[str, Any]]


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


def _seed_historical_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Insert rows under the old table name, before the rename runs."""
    conn.execute(
        """
        insert into breakdown_tickets
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_rename_completed', '+15550000011', '123 Main St', 'Toyota Camry',
           'Flat tire', 'completed', false, null, 'sent'),
          (%s, 'sess_rename_escalated', '+15550000012', 'Highway 101 NB', 'Honda Civic',
           'Engine fire', 'escalated', true, 'Vehicle fire', 'sent')
        """,
        (SEED_CALL_COMPLETED, SEED_CALL_ESCALATED),
    )


def _all_rows(
    conn: psycopg.Connection[dict[str, Any]], table: str
) -> list[dict[str, Any]]:
    rows = conn.execute(f"select * from {table} order by call_id").fetchall()
    return [dict(row) for row in rows]


@pytest.fixture(scope="module")
def migration_db() -> Iterator[MigrationDb]:
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True, connect_timeout=3)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    dbname = f"roadside_rename_test_{uuid.uuid4().hex[:10]}"
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
        _seed_historical_rows(conn)
        rows_before = _all_rows(conn, "breakdown_tickets")
        conn.execute(target.read_text())
        rows_after = _all_rows(conn, "assistance_requests")
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            admin=admin,
            conn=conn,
            dbname=dbname,
            rows_before=rows_before,
            rows_after=rows_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


class TestTableRenamed:
    """The table itself moved; old name is gone."""

    def test_old_table_is_gone(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select count(*) as n from pg_class where relname = 'breakdown_tickets'"
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_new_table_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select count(*) as n from pg_class where relname = 'assistance_requests'"
        ).fetchone()
        assert row is not None
        assert row["n"] == 1


class TestRenamePreservesData:
    """Row count, ids, and stored values are untouched by the rename."""

    def test_row_count_unchanged(self, migration_db: MigrationDb) -> None:
        assert len(migration_db.rows_after) == len(migration_db.rows_before)
        assert len(migration_db.rows_after) == 2

    def test_ids_and_data_unchanged(self, migration_db: MigrationDb) -> None:
        assert migration_db.rows_after == migration_db.rows_before

    def test_seeded_calls_present(self, migration_db: MigrationDb) -> None:
        call_ids = {row["call_id"] for row in migration_db.rows_after}
        assert call_ids == {SEED_CALL_COMPLETED, SEED_CALL_ESCALATED}


class TestConstraintsMoved:
    """PK, unique call_id, and check constraints renamed with the table."""

    def test_constraints_renamed(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select conname from pg_constraint
            where conrelid = 'assistance_requests'::regclass
            """
        ).fetchall()
        names = {row["conname"] for row in rows}
        assert set(EXPECTED_CONSTRAINTS) <= names

    def test_no_constraint_keeps_old_table_prefix(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n from pg_constraint
            where conname like 'breakdown_tickets%'
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_duplicate_call_id_still_rejected(self, migration_db: MigrationDb) -> None:
        with pytest.raises(psycopg.errors.UniqueViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, location, vehicle, issue, status)
                values (%s, 'A', 'B', 'C', 'pending')
                """,
                (SEED_CALL_COMPLETED,),
            )

    def test_invalid_intake_status_still_rejected(self, migration_db: MigrationDb) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, status, intake_status)
                values (%s, 'pending', 'bogus')
                """,
                (f"CA_RENAME_CHECK_{uuid.uuid4().hex[:8]}",),
            )


class TestRlsStillEnforced:
    """RLS and the deny-all policy stay attached after the rename."""

    def test_row_level_security_enabled(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select relrowsecurity from pg_class where relname = 'assistance_requests'"
        ).fetchone()
        assert row is not None
        assert row["relrowsecurity"] is True

    def test_deny_policy_attached_to_renamed_table(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n from pg_policies
            where tablename = 'assistance_requests'
              and policyname = 'Anonymous cannot access tickets'
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_non_owner_role_denied_by_rls(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        role = f"roadside_rename_test_{uuid.uuid4().hex[:8]}"
        call_id = f"CA_RLS_RENAME_{uuid.uuid4().hex[:8]}"
        conn.execute(f'create role "{role}" nologin')
        try:
            conn.execute(f'grant select, insert on assistance_requests to "{role}"')
            conn.execute(f'set role "{role}"')
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        """
                        insert into assistance_requests (call_id, location, vehicle, issue, status)
                        values (%s, 'A', 'B', 'C', 'pending')
                        """,
                        (call_id,),
                    )
                visible = conn.execute(
                    "select count(*) as n from assistance_requests"
                ).fetchone()
                assert visible is not None
                assert visible["n"] == 0
            finally:
                conn.execute("reset role")
        finally:
            conn.execute(f'revoke all on assistance_requests from "{role}"')
            conn.execute(f'drop role "{role}"')
