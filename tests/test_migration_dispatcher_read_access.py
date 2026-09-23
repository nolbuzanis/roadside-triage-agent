"""Schema tests for the dispatcher authentication/read-access migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed rows → target migration → later migrations

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
TARGET_MIGRATION = "20260923150000_add_dispatcher_read_access.sql"

SEED_CALL_ACTIVE = "CA_SEED_ACTIVE"
SEED_CALL_COMPLETED = "CA_SEED_COMPLETED"

DENY_POLICY_NAME = "Anonymous cannot access tickets"
READ_POLICY_NAME = "Dispatcher can read assistance requests"


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


def _seed_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Insert one open request and one completed request to read back."""
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           intake_status, hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_active', '+15550000001', null, null, null, 'in_progress',
           'in_progress', false, null, 'pending'),
          (%s, 'sess_completed', '+15550000002', '123 Main St', 'Toyota Camry',
           'Flat tire', 'completed', 'completed', false, null, 'sent')
        """,
        (SEED_CALL_ACTIVE, SEED_CALL_COMPLETED),
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

    dbname = f"roadside_dispatcher_auth_test_{uuid.uuid4().hex[:10]}"
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
# Policy catalog state after the migration
# ---------------------------------------------------------------------------


class TestPolicyCatalogState:
    """RLS stays enabled, the deny policy survives, and exactly one new
    SELECT policy for `authenticated` is added — no write policies."""

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
              and policyname = %s
            """,
            (DENY_POLICY_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_dispatcher_read_policy_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n from pg_policies
            where tablename = 'assistance_requests'
              and policyname = %s
              and cmd = 'SELECT'
              and 'authenticated' = any(roles)
            """,
            (READ_POLICY_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_no_write_policy_for_authenticated(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n from pg_policies
            where tablename = 'assistance_requests'
              and cmd in ('INSERT', 'UPDATE', 'DELETE')
              and 'authenticated' = any(roles)
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_authenticated_role_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select count(*) as n from pg_roles where rolname = 'authenticated'"
        ).fetchone()
        assert row is not None
        assert row["n"] == 1


# ---------------------------------------------------------------------------
# Authenticated dispatcher sessions can read
# ---------------------------------------------------------------------------


class TestAuthenticatedDispatcherRead:
    """The `authenticated` role (the dispatcher session) reads every row."""

    def test_authenticated_reads_all_seeded_rows(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        conn.execute("set role authenticated")
        try:
            rows = conn.execute(
                "select call_id from assistance_requests order by call_id"
            ).fetchall()
        finally:
            conn.execute("reset role")
        assert [row["call_id"] for row in rows] == [
            SEED_CALL_ACTIVE,
            SEED_CALL_COMPLETED,
        ]

    def test_authenticated_sees_partial_intake_fields(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        conn.execute("set role authenticated")
        try:
            row = conn.execute(
                """
                select caller_phone, location, vehicle, issue, intake_status
                from assistance_requests where call_id = %s
                """,
                (SEED_CALL_ACTIVE,),
            ).fetchone()
        finally:
            conn.execute("reset role")
        assert row is not None
        assert row["caller_phone"] == "+15550000001"
        assert row["location"] is None
        assert row["intake_status"] == "in_progress"

    def test_authenticated_cannot_insert(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        # Simulate Supabase default privileges: table grants exist, RLS must
        # still block writes because no INSERT policy covers `authenticated`.
        conn.execute("grant insert on assistance_requests to authenticated")
        try:
            conn.execute("set role authenticated")
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        """
                        insert into assistance_requests (call_id, location, vehicle, issue, status)
                        values (%s, 'A', 'B', 'C', 'pending')
                        """,
                        (f"CA_AUTH_INSERT_{uuid.uuid4().hex[:8]}",),
                    )
            finally:
                conn.execute("reset role")
        finally:
            conn.execute("revoke insert on assistance_requests from authenticated")

    def test_authenticated_update_matches_no_rows(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        conn.execute("grant update on assistance_requests to authenticated")
        try:
            conn.execute("set role authenticated")
            try:
                result = conn.execute(
                    """
                    update assistance_requests set location = 'HACKED'
                    where call_id = %s
                    """,
                    (SEED_CALL_COMPLETED,),
                )
                assert result.rowcount == 0
            finally:
                conn.execute("reset role")
        finally:
            conn.execute("revoke update on assistance_requests from authenticated")
        row = conn.execute(
            "select location from assistance_requests where call_id = %s",
            (SEED_CALL_COMPLETED,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "123 Main St"


# ---------------------------------------------------------------------------
# Anonymous / non-authenticated roles stay denied
# ---------------------------------------------------------------------------


class TestAnonymousStillDenied:
    """The anon role keeps table grants (Supabase default privileges) but
    sees zero rows because only the deny policy applies to it."""

    def test_anon_role_sees_no_rows(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        created_anon = False
        row = conn.execute(
            "select count(*) as n from pg_roles where rolname = 'anon'"
        ).fetchone()
        assert row is not None
        if row["n"] == 0:
            conn.execute("create role anon nologin")
            created_anon = True
        conn.execute("grant select on assistance_requests to anon")
        try:
            conn.execute("set role anon")
            try:
                visible = conn.execute(
                    "select count(*) as n from assistance_requests"
                ).fetchone()
            finally:
                conn.execute("reset role")
            assert visible is not None
            assert visible["n"] == 0
        finally:
            conn.execute("revoke select on assistance_requests from anon")
            if created_anon:
                conn.execute("drop role anon")

    def test_custom_non_authenticated_role_sees_no_rows(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        role = f"roadside_dispatcher_test_{uuid.uuid4().hex[:8]}"
        conn.execute(f'create role "{role}" nologin')
        try:
            conn.execute(f'grant select on assistance_requests to "{role}"')
            conn.execute(f'set role "{role}"')
            try:
                visible = conn.execute(
                    "select count(*) as n from assistance_requests"
                ).fetchone()
            finally:
                conn.execute("reset role")
            assert visible is not None
            assert visible["n"] == 0
        finally:
            conn.execute(f'revoke all on assistance_requests from "{role}"')
            conn.execute(f'drop role "{role}"')


# ---------------------------------------------------------------------------
# Application-order regression: full chain still applies, no data changes
# ---------------------------------------------------------------------------


class TestChainStillAppliesCleanly:
    """The policy-only migration applies without touching data."""

    def test_row_count_unchanged_by_migration(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target

    def test_seed_data_untouched(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, status, intake_status
            from assistance_requests where call_id = %s
            """,
            (SEED_CALL_COMPLETED,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "123 Main St"
        assert row["vehicle"] == "Toyota Camry"
        assert row["issue"] == "Flat tire"
        assert row["status"] == "completed"
        assert row["intake_status"] == "completed"
