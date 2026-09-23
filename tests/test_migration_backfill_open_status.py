"""Schema tests for the one-time open-status backfill migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed open/terminal rows → target migration → later migrations

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
TARGET_MIGRATION = "20260922150000_backfill_open_intake_status.sql"

SEED_FULL_PENDING = "CA_SEED_FULL_PENDING"
SEED_FULL_IN_PROGRESS = "CA_SEED_FULL_IN_PROGRESS"
SEED_PARTIAL_IN_PROGRESS = "CA_SEED_PARTIAL_IN_PROGRESS"
SEED_NULL_PENDING = "CA_SEED_NULL_PENDING"
SEED_ESCALATED = "CA_SEED_ESCALATED"
SEED_COMPLETED = "CA_SEED_COMPLETED"
SEED_ABANDONED = "CA_SEED_ABANDONED"

EXPECTED_STATUS = {
    SEED_FULL_PENDING: "completed",
    SEED_FULL_IN_PROGRESS: "completed",
    SEED_PARTIAL_IN_PROGRESS: "abandoned",
    SEED_NULL_PENDING: "abandoned",
    SEED_ESCALATED: "escalated",
    SEED_COMPLETED: "completed",
    SEED_ABANDONED: "abandoned",
}


@dataclass
class MigrationDb:
    admin: psycopg.Connection[tuple[Any, ...]]
    conn: psycopg.Connection[dict[str, Any]]
    dbname: str
    row_count_before_target: int
    row_count_after_target: int
    intake_status_after_target: dict[str, str]


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
    """Insert rows in every status shape the backfill must classify or preserve."""
    conn.execute(
        """
        insert into breakdown_tickets
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_full_pending', '+15550000001', '123 Main St', 'Toyota Camry',
           'Flat tire', 'pending', false, null, 'sent'),
          (%s, 'sess_full_in_progress', '+15550000002', 'Highway 101 NB', 'Honda Civic',
           'Blown tire', 'in_progress', false, null, 'pending'),
          (%s, 'sess_partial_in_progress', '+15550000003', 'I-90 West', null,
           null, 'in_progress', false, null, 'pending'),
          (%s, 'sess_null_pending', '+15550000004', null, null,
           null, 'pending', false, null, 'pending'),
          (%s, 'sess_escalated', '+15550000005', 'River Rd', 'Ford F-150',
           'Engine fire', 'escalated', true, 'Vehicle fire', 'sent'),
          (%s, 'sess_completed', '+15550000006', 'Oak St', 'Subaru',
           'Flat battery', 'completed', false, null, 'sent'),
          (%s, 'sess_abandoned', '+15550000007', null, null,
           null, 'abandoned', false, null, 'pending')
        """,
        (
            SEED_FULL_PENDING,
            SEED_FULL_IN_PROGRESS,
            SEED_PARTIAL_IN_PROGRESS,
            SEED_NULL_PENDING,
            SEED_ESCALATED,
            SEED_COMPLETED,
            SEED_ABANDONED,
        ),
    )


def _row_count(conn: psycopg.Connection[dict[str, Any]]) -> int:
    # Runs before and after the target migration but before the later
    # breakdown_tickets → assistance_requests rename joins the chain.
    row = conn.execute("select count(*) as n from breakdown_tickets").fetchone()
    assert row is not None
    return int(row["n"])


def _intake_status_by_call(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, str]:
    # Runs immediately after the target migration, before the later rename
    # and intake_status reconciliation migrations join the chain.
    rows = conn.execute(
        "select call_id, intake_status from breakdown_tickets"
    ).fetchall()
    return {row["call_id"]: row["intake_status"] for row in rows}


def _status_by_call(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, str]:
    # Called only from post-chain assertions, after the table rename has run.
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

    dbname = f"roadside_backfill_test_{uuid.uuid4().hex[:10]}"
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
        row_count_before = _row_count(conn)
        conn.execute(target.read_text())
        row_count_after = _row_count(conn)
        intake_after_target = _intake_status_by_call(conn)
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            admin=admin,
            conn=conn,
            dbname=dbname,
            row_count_before_target=row_count_before,
            row_count_after_target=row_count_after,
            intake_status_after_target=intake_after_target,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


# ---------------------------------------------------------------------------
# Backfill classification rules
# ---------------------------------------------------------------------------


class TestBackfillClassification:
    """Each seeded status shape maps to the specified final status."""

    def test_every_row_receives_expected_status(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert statuses == EXPECTED_STATUS

    def test_full_intake_open_rows_become_completed(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert statuses[SEED_FULL_PENDING] == "completed"
        assert statuses[SEED_FULL_IN_PROGRESS] == "completed"

    def test_partial_intake_open_rows_become_abandoned(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert statuses[SEED_PARTIAL_IN_PROGRESS] == "abandoned"
        assert statuses[SEED_NULL_PENDING] == "abandoned"

    def test_terminal_statuses_are_unchanged(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert statuses[SEED_ESCALATED] == "escalated"
        assert statuses[SEED_COMPLETED] == "completed"
        assert statuses[SEED_ABANDONED] == "abandoned"

    def test_no_row_remains_open(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select count(*) as n from assistance_requests "
            "where status in ('pending', 'in_progress')"
        ).fetchone()
        assert row is not None
        assert row["n"] == 0


# ---------------------------------------------------------------------------
# Row data and counts unchanged
# ---------------------------------------------------------------------------


class TestBackfillPreservesData:
    """The backfill rewrites only the status column."""

    def test_row_count_unchanged(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target

    def test_intake_status_untouched_by_target_backfill(
        self, migration_db: MigrationDb
    ) -> None:
        """Immediately after the target, intake_status is still the insert
        default: this backfill drives only the free-text `status` column.
        (The later intake_status reconciliation migration aligns it.)"""
        for call_id in EXPECTED_STATUS:
            assert migration_db.intake_status_after_target[call_id] == "in_progress"

    def test_intake_and_hazard_data_unchanged(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, session_id, caller_phone,
                   hazard_detected, hazard_reason, notification_status, intake_status
            from assistance_requests where call_id = %s
            """,
            (SEED_ESCALATED,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "River Rd"
        assert row["vehicle"] == "Ford F-150"
        assert row["issue"] == "Engine fire"
        assert row["session_id"] == "sess_escalated"
        assert row["caller_phone"] == "+15550000005"
        assert row["hazard_detected"] is True
        assert row["hazard_reason"] == "Vehicle fire"
        assert row["notification_status"] == "sent"
        # Final chain state: the later intake_status reconciliation migration
        # aligns intake_status with the terminal status.
        assert row["intake_status"] == "escalated"

    def test_partial_row_keeps_collected_fields(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select location, vehicle, issue, status from assistance_requests where call_id = %s",
            (SEED_PARTIAL_IN_PROGRESS,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "I-90 West"
        assert row["vehicle"] is None
        assert row["issue"] is None
        assert row["status"] == "abandoned"


# ---------------------------------------------------------------------------
# Application-order regression: the old nullable-intake target still passes
# ---------------------------------------------------------------------------


class TestChainStillAppliesCleanly:
    """The full migration chain (including this one) applies without error."""

    def test_all_seven_seed_rows_present(self, migration_db: MigrationDb) -> None:
        statuses = _status_by_call(migration_db.conn)
        assert set(statuses) == set(EXPECTED_STATUS)

    def test_status_column_still_free_text(self, migration_db: MigrationDb) -> None:
        call_id = f"CA_FREE_{uuid.uuid4().hex[:8]}"
        migration_db.conn.execute(
            "insert into assistance_requests (call_id, status) values (%s, 'pending')",
            (call_id,),
        )
        updated = migration_db.conn.execute(
            """
            update assistance_requests set status = 'custom_value'
            where call_id = %s
            returning status
            """,
            (call_id,),
        ).fetchone()
        assert updated is not None
        assert updated["status"] == "custom_value"
