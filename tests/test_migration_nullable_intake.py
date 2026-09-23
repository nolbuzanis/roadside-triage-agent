"""Schema tests for the P0 nullable-intake + intake_status migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed historical rows → target migration → later migrations

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
TARGET_MIGRATION = "20260922000000_allow_nullable_intake_add_intake_status.sql"

SEED_CALL_PENDING = "CA_SEED_PENDING"
SEED_CALL_ESCALATED = "CA_SEED_ESCALATED"

ALLOWED_INTAKE_STATUSES = ("in_progress", "completed", "abandoned", "escalated")


@dataclass
class MigrationDb:
    admin: psycopg.Connection[tuple[Any, ...]]
    conn: psycopg.Connection[dict[str, Any]]
    dbname: str
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


def _seed_historical_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Insert rows shaped like production data created under the old model."""
    conn.execute(
        """
        insert into breakdown_tickets
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_seed_pending', '+15550000001', '123 Main St', 'Toyota Camry',
           'Flat tire', 'pending', false, null, 'sent'),
          (%s, 'sess_seed_escalated', '+15550000002', 'Highway 101 NB', 'Honda Civic',
           'Engine fire', 'escalated', true, 'Vehicle fire', 'sent')
        """,
        (SEED_CALL_PENDING, SEED_CALL_ESCALATED),
    )


def _row_count(conn: psycopg.Connection[dict[str, Any]]) -> int:
    # Runs before and after the target migration but before the later
    # breakdown_tickets → assistance_requests rename joins the chain.
    row = conn.execute("select count(*) as n from breakdown_tickets").fetchone()
    assert row is not None
    return int(row["n"])


@pytest.fixture(scope="module")
def migration_db() -> Iterator[MigrationDb]:
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True, connect_timeout=3)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    dbname = f"roadside_migration_test_{uuid.uuid4().hex[:10]}"
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
        row_count_before = _row_count(conn)
        conn.execute(target.read_text())
        row_count_after = _row_count(conn)
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            admin=admin,
            conn=conn,
            dbname=dbname,
            row_count_before_target=row_count_before,
            row_count_after_target=row_count_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


# ---------------------------------------------------------------------------
# Existing production rows migrate successfully
# ---------------------------------------------------------------------------


class TestHistoricalBackfill:
    """Rows created under the old full-intake model end with a truthful
    intake_status after the full chain: the legacy pending row completes, the
    escalated row stays escalated."""

    def test_existing_rows_receive_truthful_intake_status(
        self, migration_db: MigrationDb
    ) -> None:
        rows = migration_db.conn.execute(
            "select call_id, intake_status from assistance_requests where call_id in (%s, %s)",
            (SEED_CALL_PENDING, SEED_CALL_ESCALATED),
        ).fetchall()
        assert len(rows) == 2
        intake_by_call = {row["call_id"]: row["intake_status"] for row in rows}
        assert intake_by_call[SEED_CALL_PENDING] == "completed"
        assert intake_by_call[SEED_CALL_ESCALATED] == "escalated"

    def test_no_historical_row_marked_in_progress(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n from assistance_requests
            where call_id in (%s, %s) and intake_status = 'in_progress'
            """,
            (SEED_CALL_PENDING, SEED_CALL_ESCALATED),
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_historical_intake_values_unchanged(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, session_id, caller_phone,
                   hazard_detected, hazard_reason, notification_status
            from assistance_requests where call_id = %s
            """,
            (SEED_CALL_PENDING,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "123 Main St"
        assert row["vehicle"] == "Toyota Camry"
        assert row["issue"] == "Flat tire"
        assert row["session_id"] == "sess_seed_pending"
        assert row["caller_phone"] == "+15550000001"
        assert row["hazard_detected"] is False
        assert row["hazard_reason"] is None
        assert row["notification_status"] == "sent"

    def test_row_count_unchanged_by_migration(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target


# ---------------------------------------------------------------------------
# Nullable intake columns
# ---------------------------------------------------------------------------


class TestNullableIntake:
    """location / vehicle / issue accept nulls for early-created rows."""

    def test_intake_columns_are_nullable(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select column_name, is_nullable from information_schema.columns
            where table_name = 'assistance_requests'
              and column_name in ('location', 'vehicle', 'issue')
            """
        ).fetchall()
        assert len(rows) == 3
        assert all(row["is_nullable"] == "YES" for row in rows)

    def test_insert_with_null_intake_succeeds(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_NULL_{uuid.uuid4().hex[:8]}"
        row = migration_db.conn.execute(
            """
            insert into assistance_requests
              (call_id, caller_phone, location, vehicle, issue, status)
            values (%s, '+15551112222', null, null, null, 'pending')
            returning call_id, location, vehicle, issue
            """,
            (call_id,),
        ).fetchone()
        assert row is not None
        assert row["call_id"] == call_id
        assert row["location"] is None
        assert row["vehicle"] is None
        assert row["issue"] is None


# ---------------------------------------------------------------------------
# intake_status column: default, NOT NULL, check constraint
# ---------------------------------------------------------------------------


class TestIntakeStatusColumn:
    """The dedicated intake lifecycle column behaves as specified."""

    def test_column_is_not_null(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select is_nullable from information_schema.columns
            where table_name = 'assistance_requests' and column_name = 'intake_status'
            """
        ).fetchone()
        assert row is not None
        assert row["is_nullable"] == "NO"

    def test_column_default_is_in_progress(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select column_default from information_schema.columns
            where table_name = 'assistance_requests' and column_name = 'intake_status'
            """
        ).fetchone()
        assert row is not None
        assert row["column_default"] == "'in_progress'::text"

    def test_new_row_defaults_to_in_progress(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_DEFAULT_{uuid.uuid4().hex[:8]}"
        row = migration_db.conn.execute(
            """
            insert into assistance_requests (call_id, status)
            values (%s, 'pending')
            returning intake_status
            """,
            (call_id,),
        ).fetchone()
        assert row is not None
        assert row["intake_status"] == "in_progress"

    @pytest.mark.parametrize("value", ALLOWED_INTAKE_STATUSES)
    def test_allowed_values_accepted(self, migration_db: MigrationDb, value: str) -> None:
        call_id = f"CA_ALLOW_{value}_{uuid.uuid4().hex[:6]}"
        row = migration_db.conn.execute(
            """
            insert into assistance_requests (call_id, status, intake_status)
            values (%s, 'pending', %s)
            returning intake_status
            """,
            (call_id, value),
        ).fetchone()
        assert row is not None
        assert row["intake_status"] == value

    def test_invalid_value_rejected(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_INVALID_{uuid.uuid4().hex[:8]}"
        with pytest.raises(psycopg.errors.CheckViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, status, intake_status)
                values (%s, 'pending', 'bogus')
                """,
                (call_id,),
            )

    def test_null_intake_status_rejected(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_NULL_STATUS_{uuid.uuid4().hex[:8]}"
        with pytest.raises(psycopg.errors.NotNullViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, status, intake_status)
                values (%s, 'pending', null)
                """,
                (call_id,),
            )


# ---------------------------------------------------------------------------
# Existing status behavior unchanged
# ---------------------------------------------------------------------------


class TestStatusUnchanged:
    """`status` stays the dispatcher/business lifecycle column."""

    def test_status_default_still_pending(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select column_default from information_schema.columns
            where table_name = 'assistance_requests' and column_name = 'status'
            """
        ).fetchone()
        assert row is not None
        assert row["column_default"] == "'pending'::text"

    def test_new_row_status_defaults_to_pending(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_STATUS_{uuid.uuid4().hex[:8]}"
        row = migration_db.conn.execute(
            """
            insert into assistance_requests (call_id)
            values (%s)
            returning status
            """,
            (call_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"

    def test_status_remains_free_text(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_STATUS_FREE_{uuid.uuid4().hex[:8]}"
        migration_db.conn.execute(
            "insert into assistance_requests (call_id, status) values (%s, 'pending')",
            (call_id,),
        )
        updated = migration_db.conn.execute(
            """
            update assistance_requests set status = 'escalated'
            where call_id = %s
            returning status
            """,
            (call_id,),
        ).fetchone()
        assert updated is not None
        assert updated["status"] == "escalated"

    def test_historical_status_finalized_by_backfill(self, migration_db: MigrationDb) -> None:
        """After the full chain, the legacy pending row is finalized by the
        one-time open-status backfill (full intake → completed); escalated
        and other terminal statuses are never overwritten."""
        rows = migration_db.conn.execute(
            "select call_id, status from assistance_requests where call_id in (%s, %s)",
            (SEED_CALL_PENDING, SEED_CALL_ESCALATED),
        ).fetchall()
        status_by_call = {row["call_id"]: row["status"] for row in rows}
        assert status_by_call[SEED_CALL_PENDING] == "completed"
        assert status_by_call[SEED_CALL_ESCALATED] == "escalated"


# ---------------------------------------------------------------------------
# Existing table invariants unchanged
# ---------------------------------------------------------------------------


class TestExistingInvariants:
    """Unique call_id and RLS remain enforced after the migration."""

    def test_duplicate_call_id_still_rejected(self, migration_db: MigrationDb) -> None:
        with pytest.raises(psycopg.errors.UniqueViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, location, vehicle, issue, status)
                values (%s, 'A', 'B', 'C', 'pending')
                """,
                (SEED_CALL_PENDING,),
            )

    def test_row_level_security_still_enabled(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select relrowsecurity from pg_class where relname = 'assistance_requests'"
        ).fetchone()
        assert row is not None
        assert row["relrowsecurity"] is True

    def test_deny_policy_still_exists(self, migration_db: MigrationDb) -> None:
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
        role = f"roadside_migration_test_{uuid.uuid4().hex[:8]}"
        call_id = f"CA_RLS_{uuid.uuid4().hex[:8]}"
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
