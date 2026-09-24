"""Schema tests for the demo-session model migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed rows → target migration → later migrations

Covers the demo_sessions table shape, the keyed-HMAC-only phone storage
contract, at-most-once/expiry claim guards, the assistance_requests link,
and the deny-all RLS posture. Skips (does not fail) when no local
PostgreSQL server is reachable.
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
TARGET_MIGRATION = "20260923180000_create_demo_sessions.sql"

SEED_CALL_ID = "CA_SEED_DEMO_MODEL"
AUTH_USER_ID = "11111111-2222-3333-4444-555555555555"
PHONE_HMAC = "a" * 64
DENY_POLICY_NAME = "Anonymous cannot access demo sessions"


@dataclass
class MigrationDb:
    conn: psycopg.Connection[dict[str, Any]]
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
    """Insert a pre-existing assistance request with no demo link."""
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           intake_status, hazard_detected, hazard_reason, notification_status)
        values
          (%s, 'sess_demo_model', '+15550000020', '9 Elm St', 'Honda Civic',
           'Battery dead', 'completed', 'completed', false, null, 'sent')
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

    dbname = f"roadside_demo_sessions_test_{uuid.uuid4().hex[:10]}"
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
            conn=conn,
            row_count_before_target=row_count_before,
            row_count_after_target=row_count_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


def _insert_session(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    expires_offset_seconds: int = 900,
) -> str:
    row = conn.execute(
        """
        insert into demo_sessions (auth_user_id, phone_hmac, phone_last4, expires_at)
        values (%s, %s, '1234', now() + make_interval(secs => %s))
        returning id
        """,
        (AUTH_USER_ID, PHONE_HMAC, expires_offset_seconds),
    ).fetchone()
    assert row is not None
    return str(row["id"])


class TestDemoSessionsSchema:
    """The table has the documented columns, defaults, and constraints."""

    def test_table_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select count(*) as n from pg_class where relname = 'demo_sessions'"
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_columns_and_nullability(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select column_name, is_nullable, column_default
            from information_schema.columns
            where table_schema = 'public' and table_name = 'demo_sessions'
            """
        ).fetchall()
        columns = {r["column_name"]: r for r in rows}
        expected = {
            "id",
            "auth_user_id",
            "phone_hmac",
            "phone_last4",
            "expires_at",
            "claimed_at",
            "call_id",
            "created_at",
        }
        assert set(columns) == expected
        for col in ("id", "auth_user_id", "phone_hmac", "phone_last4", "expires_at", "created_at"):
            assert columns[col]["is_nullable"] == "NO", col
        for col in ("claimed_at", "call_id"):
            assert columns[col]["is_nullable"] == "YES", col
        assert "gen_random_uuid()" in (columns["id"]["column_default"] or "")

    def test_phone_last4_check_constraint(self, migration_db: MigrationDb) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            migration_db.conn.execute(
                """
                insert into demo_sessions (auth_user_id, phone_hmac, phone_last4, expires_at)
                values (%s, %s, '12', now() + interval '15 minutes')
                """,
                (AUTH_USER_ID, PHONE_HMAC),
            )

    def test_claim_fields_must_be_written_together(self, migration_db: MigrationDb) -> None:
        session_id = _insert_session(migration_db.conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            migration_db.conn.execute(
                "update demo_sessions set claimed_at = now() where id = %s",
                (session_id,),
            )

    def test_phone_hmac_index_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_indexes
            where tablename = 'demo_sessions' and indexname = 'demo_sessions_phone_hmac_idx'
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_gen_random_uuid_primary_key(self, migration_db: MigrationDb) -> None:
        session_id = _insert_session(migration_db.conn)
        uuid.UUID(session_id)


class TestRawPhoneNeverPersisted:
    """Only the keyed HMAC and last four digits are stored."""

    def test_no_plaintext_phone_column_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'demo_sessions'
              and column_name in ('phone', 'phone_number', 'caller_phone', 'phone_plain')
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_insert_with_hmac_and_last4_succeeds(self, migration_db: MigrationDb) -> None:
        session_id = _insert_session(migration_db.conn)
        row = migration_db.conn.execute(
            "select phone_hmac, phone_last4 from demo_sessions where id = %s",
            (session_id,),
        ).fetchone()
        assert row is not None
        assert row["phone_hmac"] == PHONE_HMAC
        assert row["phone_last4"] == "1234"


class TestClaimGuards:
    """Guarded claim semantics live in the schema constraints and predicate."""

    def test_unexpired_unclaimed_session_can_be_claimed_once(
        self, migration_db: MigrationDb
    ) -> None:
        session_id = _insert_session(migration_db.conn)
        first = migration_db.conn.execute(
            """
            update demo_sessions
            set claimed_at = now(), call_id = 'CA_CALL_1'
            where id = %s and claimed_at is null and expires_at > now()
            returning id
            """,
            (session_id,),
        ).fetchall()
        assert len(first) == 1

        second = migration_db.conn.execute(
            """
            update demo_sessions
            set claimed_at = now(), call_id = 'CA_CALL_2'
            where id = %s and claimed_at is null and expires_at > now()
            returning id
            """,
            (session_id,),
        ).fetchall()
        assert second == []

        row = migration_db.conn.execute(
            "select call_id from demo_sessions where id = %s", (session_id,)
        ).fetchone()
        assert row is not None
        assert row["call_id"] == "CA_CALL_1"

    def test_expired_session_cannot_be_claimed(self, migration_db: MigrationDb) -> None:
        session_id = _insert_session(migration_db.conn, expires_offset_seconds=-60)
        rows = migration_db.conn.execute(
            """
            update demo_sessions
            set claimed_at = now(), call_id = 'CA_LATE_CALL'
            where id = %s and claimed_at is null and expires_at > now()
            returning id
            """,
            (session_id,),
        ).fetchall()
        assert rows == []
        row = migration_db.conn.execute(
            "select claimed_at, call_id from demo_sessions where id = %s", (session_id,)
        ).fetchone()
        assert row is not None
        assert row["claimed_at"] is None
        assert row["call_id"] is None

    def test_missing_session_matches_nothing(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            update demo_sessions
            set claimed_at = now(), call_id = 'CA_GHOST'
            where id = %s and claimed_at is null and expires_at > now()
            returning id
            """,
            (str(uuid.uuid4()),),
        ).fetchall()
        assert rows == []


class TestAssistanceRequestLinkage:
    """assistance_requests gains an optional, cleanup-safe demo link."""

    def test_column_exists_and_is_nullable(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select is_nullable
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'assistance_requests'
              and column_name = 'demo_session_id'
            """
        ).fetchone()
        assert row is not None
        assert row["is_nullable"] == "YES"

    def test_row_count_and_seed_unchanged(self, migration_db: MigrationDb) -> None:
        assert migration_db.row_count_after_target == migration_db.row_count_before_target
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, status, intake_status,
                   notification_status, demo_session_id
            from assistance_requests where call_id = %s
            """,
            (SEED_CALL_ID,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "9 Elm St"
        assert row["vehicle"] == "Honda Civic"
        assert row["issue"] == "Battery dead"
        assert row["status"] == "completed"
        assert row["intake_status"] == "completed"
        assert row["notification_status"] == "sent"
        assert row["demo_session_id"] is None

    def test_link_round_trips_and_unlinks_on_delete(
        self, migration_db: MigrationDb
    ) -> None:
        session_id = _insert_session(migration_db.conn)
        migration_db.conn.execute(
            """
            insert into assistance_requests (call_id, demo_session_id)
            values ('CA_DEMO_LINKED', %s)
            """,
            (session_id,),
        )
        migration_db.conn.execute(
            "delete from demo_sessions where id = %s", (session_id,)
        )
        row = migration_db.conn.execute(
            "select demo_session_id from assistance_requests where call_id = 'CA_DEMO_LINKED'"
        ).fetchone()
        assert row is not None
        assert row["demo_session_id"] is None

    def test_unknown_demo_session_id_rejected(self, migration_db: MigrationDb) -> None:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            migration_db.conn.execute(
                """
                insert into assistance_requests (call_id, demo_session_id)
                values ('CA_DEMO_BAD_FK', %s)
                """,
                (str(uuid.uuid4()),),
            )


class TestSecurityUnchanged:
    """RLS is on with a deny-all policy; the only client read grant (added by
    the demo request access RLS migration in the chain's "after" step) is the
    owner-scoped demo session SELECT — never a write policy."""

    def test_row_level_security_enabled(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select relrowsecurity from pg_class where relname = 'demo_sessions'"
        ).fetchone()
        assert row is not None
        assert row["relrowsecurity"] is True

    def test_deny_all_policy_present(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_policies
            where tablename = 'demo_sessions' and policyname = %s
            """,
            (DENY_POLICY_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1
        policy = migration_db.conn.execute(
            """
            select cmd, qual
            from pg_policies
            where tablename = 'demo_sessions' and policyname = %s
            """,
            (DENY_POLICY_NAME,),
        ).fetchone()
        assert policy is not None
        assert policy["cmd"] in ("*", "ALL")
        assert policy["qual"] == "false"

    def test_only_owner_scoped_read_policy_for_client_roles(
        self, migration_db: MigrationDb
    ) -> None:
        rows = migration_db.conn.execute(
            """
            select policyname, cmd, roles
            from pg_policies
            where tablename = 'demo_sessions'
            order by policyname
            """
        ).fetchall()
        # The deny-all policy plus exactly one SELECT grant for authenticated
        # (the owner-scoped demo session read from the later RLS migration);
        # anon gets nothing and no write policy exists for any client role.
        assert len(rows) == 2
        by_name = {row["policyname"]: row for row in rows}
        assert set(by_name) == {DENY_POLICY_NAME, "Demo user can read own demo session"}
        assert by_name[DENY_POLICY_NAME]["cmd"] in ("*", "ALL")
        read = by_name["Demo user can read own demo session"]
        assert read["cmd"] == "SELECT"
        assert read["roles"] == ["authenticated"]

        write = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_policies
            where tablename = 'demo_sessions'
              and cmd in ('INSERT', 'UPDATE', 'DELETE')
            """
        ).fetchone()
        assert write is not None
        assert write["n"] == 0
