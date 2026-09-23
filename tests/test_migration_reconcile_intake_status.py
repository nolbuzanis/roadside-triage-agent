"""Schema tests for the intake_status lifecycle reconciliation migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed stale/aligned rows → target migration → later migrations

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
TARGET_MIGRATION = "20260923120000_reconcile_intake_status_lifecycle.sql"

SEED_COMPLETED_STALE = "CA_SEED_COMPLETED_STALE"
SEED_ABANDONED_STALE = "CA_SEED_ABANDONED_STALE"
SEED_ESCALATED_STALE = "CA_SEED_ESCALATED_STALE"
SEED_OPEN_IN_PROGRESS = "CA_SEED_OPEN_IN_PROGRESS"
SEED_PENDING_OPEN = "CA_SEED_PENDING_OPEN"
SEED_ALIGNED_COMPLETED = "CA_SEED_ALIGNED_COMPLETED"

EXPECTED_INTAKE_STATUS = {
    SEED_COMPLETED_STALE: "completed",
    SEED_ABANDONED_STALE: "abandoned",
    SEED_ESCALATED_STALE: "escalated",
    SEED_OPEN_IN_PROGRESS: "in_progress",
    SEED_PENDING_OPEN: "in_progress",
    SEED_ALIGNED_COMPLETED: "completed",
}

TERMINAL_STATUSES = ("completed", "abandoned", "escalated")


@dataclass
class MigrationDb:
    admin: psycopg.Connection[tuple[Any, ...]]
    conn: psycopg.Connection[dict[str, Any]]
    dbname: str
    rows_before: list[dict[str, Any]]
    rows_after: list[dict[str, Any]]
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
    """Insert rows in every status/intake_status shape the backfill must reconcile.

    Stale rows mimic production data finalized before the application wrote
    intake_status: terminal `status` with the default 'in_progress'. Open rows
    and already-aligned rows must be left untouched.
    """
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_completed_stale', '+15550000001', '123 Main St', 'Toyota Camry',
           'Flat tire', 'completed', false, null, 'sent'),
          (%s, 'sess_abandoned_stale', '+15550000002', 'I-90 West', null,
           null, 'abandoned', false, null, 'pending'),
          (%s, 'sess_escalated_stale', '+15550000003', 'River Rd', 'Ford F-150',
           'Engine fire', 'escalated', true, 'Vehicle fire', 'sent'),
          (%s, 'sess_open_in_progress', '+15550000004', 'Oak St', null,
           null, 'in_progress', false, null, 'pending'),
          (%s, 'sess_pending_open', '+15550000005', null, null,
           null, 'pending', false, null, 'pending')
        """,
        (
            SEED_COMPLETED_STALE,
            SEED_ABANDONED_STALE,
            SEED_ESCALATED_STALE,
            SEED_OPEN_IN_PROGRESS,
            SEED_PENDING_OPEN,
        ),
    )
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           intake_status, hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_aligned_completed', '+15550000006', 'Pine St', 'Honda Civic',
           'Dead battery', 'completed', 'completed', false, null, 'sent')
        """,
        (SEED_ALIGNED_COMPLETED,),
    )


def _all_rows(conn: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from assistance_requests order by call_id"
    ).fetchall()
    return [dict(row) for row in rows]


def _row_count(rows: list[dict[str, Any]]) -> int:
    return len(rows)


def _intake_status_by_call(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, str]:
    rows = conn.execute(
        "select call_id, intake_status from assistance_requests"
    ).fetchall()
    return {row["call_id"]: row["intake_status"] for row in rows}


def _status_by_call(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, str]:
    rows = conn.execute(
        "select call_id, status from assistance_requests"
    ).fetchall()
    return {row["call_id"]: row["status"] for row in rows}


@pytest.fixture(scope="module")
def migration_db() -> Iterator[MigrationDb]:
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True, connect_timeout=3)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    dbname = f"roadside_reconcile_intake_test_{uuid.uuid4().hex[:10]}"
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
        _seed_rows(conn)
        rows_before = _all_rows(conn)
        row_count_before = _row_count(rows_before)
        conn.execute(target.read_text())
        rows_after = _all_rows(conn)
        row_count_after = _row_count(rows_after)
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            admin=admin,
            conn=conn,
            dbname=dbname,
            rows_before=rows_before,
            rows_after=rows_after,
            row_count_before_target=row_count_before,
            row_count_after_target=row_count_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


# ---------------------------------------------------------------------------
# Reconciliation classification rules
# ---------------------------------------------------------------------------


class TestReconcileClassification:
    """Each seeded status shape ends with the truthful intake_status."""

    def test_every_row_receives_expected_intake_status(
        self, migration_db: MigrationDb
    ) -> None:
        intake_statuses = _intake_status_by_call(migration_db.conn)
        assert intake_statuses == EXPECTED_INTAKE_STATUS

    def test_stale_terminal_rows_leave_in_progress(
        self, migration_db: MigrationDb
    ) -> None:
        intake_statuses = _intake_status_by_call(migration_db.conn)
        assert intake_statuses[SEED_COMPLETED_STALE] == "completed"
        assert intake_statuses[SEED_ABANDONED_STALE] == "abandoned"
        assert intake_statuses[SEED_ESCALATED_STALE] == "escalated"

    def test_open_rows_stay_in_progress(self, migration_db: MigrationDb) -> None:
        intake_statuses = _intake_status_by_call(migration_db.conn)
        assert intake_statuses[SEED_OPEN_IN_PROGRESS] == "in_progress"
        assert intake_statuses[SEED_PENDING_OPEN] == "in_progress"

    def test_aligned_rows_are_unchanged(self, migration_db: MigrationDb) -> None:
        intake_statuses = _intake_status_by_call(migration_db.conn)
        assert intake_statuses[SEED_ALIGNED_COMPLETED] == "completed"

    def test_no_terminal_status_row_left_with_in_progress_intake(
        self, migration_db: MigrationDb
    ) -> None:
        """Acceptance: no terminal request remains intake_status 'in_progress'."""
        row = migration_db.conn.execute(
            """
            select count(*) as n from assistance_requests
            where status in ('completed', 'abandoned', 'escalated')
              and intake_status = 'in_progress'
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0


# ---------------------------------------------------------------------------
# Only intake_status changes; everything else preserved
# ---------------------------------------------------------------------------


class TestReconcilePreservesData:
    """The backfill rewrites only the intake_status (and updated_at) columns."""

    def test_row_count_unchanged(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target

    def test_status_values_unchanged(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert statuses == {
            SEED_COMPLETED_STALE: "completed",
            SEED_ABANDONED_STALE: "abandoned",
            SEED_ESCALATED_STALE: "escalated",
            SEED_OPEN_IN_PROGRESS: "in_progress",
            SEED_PENDING_OPEN: "pending",
            SEED_ALIGNED_COMPLETED: "completed",
        }

    def test_only_intake_status_differs_from_before(
        self, migration_db: MigrationDb
    ) -> None:
        before_by_call = {row["call_id"]: row for row in migration_db.rows_before}
        after_by_call = {row["call_id"]: row for row in migration_db.rows_after}
        assert set(before_by_call) == set(after_by_call)
        for call_id, after_row in after_by_call.items():
            before_row = before_by_call[call_id]
            for column, before_value in before_row.items():
                if column in ("intake_status", "updated_at"):
                    continue
                assert after_row[column] == before_value, (
                    f"{call_id}.{column} changed: {before_value!r} -> {after_row[column]!r}"
                )

    def test_intake_and_hazard_data_unchanged(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, session_id, caller_phone,
                   hazard_detected, hazard_reason, notification_status
            from assistance_requests where call_id = %s
            """,
            (SEED_ESCALATED_STALE,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "River Rd"
        assert row["vehicle"] == "Ford F-150"
        assert row["issue"] == "Engine fire"
        assert row["session_id"] == "sess_escalated_stale"
        assert row["caller_phone"] == "+15550000003"
        assert row["hazard_detected"] is True
        assert row["hazard_reason"] == "Vehicle fire"
        assert row["notification_status"] == "sent"


# ---------------------------------------------------------------------------
# Application-order regression: full chain still applies
# ---------------------------------------------------------------------------


class TestChainStillAppliesCleanly:
    """The full migration chain (including this one) applies without error."""

    def test_all_six_seed_rows_present(self, migration_db: MigrationDb) -> None:
        intake_statuses = _intake_status_by_call(migration_db.conn)
        assert set(intake_statuses) == set(EXPECTED_INTAKE_STATUS)

    def test_intake_status_check_constraint_still_enforced(
        self, migration_db: MigrationDb
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, status, intake_status)
                values (%s, 'pending', 'bogus')
                """,
                (f"CA_RECONCILE_CHECK_{uuid.uuid4().hex[:8]}",),
            )

    def test_new_rows_still_default_to_open_intake_status(
        self, migration_db: MigrationDb
    ) -> None:
        call_id = f"CA_RECONCILE_DEFAULT_{uuid.uuid4().hex[:8]}"
        row = migration_db.conn.execute(
            """
            insert into assistance_requests (call_id, status)
            values (%s, 'in_progress')
            returning intake_status
            """,
            (call_id,),
        ).fetchone()
        assert row is not None
        assert row["intake_status"] == "in_progress"
