"""Schema tests for the demo request access RLS migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → seed rows → target migration → later migrations

Supabase Auth sessions are simulated by setting the `request.jwt.claims` GUC
(read by `auth.jwt()`/`auth.uid()`) while running under the `authenticated`
role. Skips (does not fail) when no local PostgreSQL server is reachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import psycopg.rows
import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
TARGET_MIGRATION = "20260924120000_restrict_demo_request_access_rls.sql"

USER_A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_DISPATCHER = "44444444-dddd-4ddd-8ddd-dddddddddddd"

SESSION_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SESSION_B = "bbbbbbbb-0000-4000-8000-00000000000b"
SESSION_A_EXPIRED = "eeeeeeee-0000-4000-8000-00000000eeee"

CALL_A = "CA_DEMO_A"
CALL_B = "CA_DEMO_B"
CALL_A_EXPIRED = "CA_DEMO_A_EXPIRED"
CALL_HISTORICAL = "CA_HISTORICAL_PHONE"

PHONE_A = "+15550000111"
PHONE_B = "+15550000222"

ALL_CALLS = [CALL_A, CALL_A_EXPIRED, CALL_B, CALL_HISTORICAL]

ASSISTANCE_DENY_POLICY = "Anonymous cannot access tickets"
ASSISTANCE_DISPATCHER_POLICY = "Dispatcher can read assistance requests"
ASSISTANCE_DEMO_POLICY = "Demo users read only their linked assistance requests"
DEMO_SESSION_DENY_POLICY = "Anonymous cannot access demo sessions"
DEMO_SESSION_OWN_POLICY = "Demo user can read own demo session"


@dataclass
class MigrationDb:
    conn: psycopg.Connection[dict[str, Any]]
    requests_before: int
    requests_after: int
    sessions_before: int
    sessions_after: int


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
    """Insert two demo owners' sessions and their linked requests, plus an
    expired-session request and an unlinked historical request that shares
    demo user A's caller_phone."""
    conn.execute(
        """
        insert into demo_sessions (id, auth_user_id, phone_hmac, phone_last4,
                                   expires_at, claimed_at, call_id)
        values
          (%s, %s, %s, '1111', now() + interval '15 minutes', now(), %s),
          (%s, %s, %s, '2222', now() + interval '15 minutes', now(), %s),
          (%s, %s, %s, '3333', now() - interval '1 minute', now(), %s)
        """,
        (
            SESSION_A,
            USER_A,
            "a" * 64,
            CALL_A,
            SESSION_B,
            USER_B,
            "b" * 64,
            CALL_B,
            SESSION_A_EXPIRED,
            USER_A,
            "c" * 64,
            CALL_A_EXPIRED,
        ),
    )
    conn.execute(
        """
        insert into assistance_requests
          (call_id, session_id, caller_phone, location, vehicle, issue, status,
           intake_status, hazard_detected, hazard_reason, notification_status,
           demo_session_id)
        values
          (%s, 'sess_a', %s, '1 A St', 'Toyota', 'Flat tire', 'completed',
           'completed', false, null, 'sent', %s),
          (%s, 'sess_b', %s, '2 B St', 'Honda', 'Battery dead', 'completed',
           'completed', false, null, 'sent', %s),
          (%s, 'sess_a_exp', %s, '3 C St', 'Ford', 'Lockout', 'completed',
           'completed', false, null, 'sent', %s),
          (%s, 'sess_hist', %s, '4 D St', 'Subaru', 'Old request', 'completed',
           'completed', false, null, 'sent', null)
        """,
        (
            CALL_A,
            PHONE_A,
            SESSION_A,
            CALL_B,
            PHONE_B,
            SESSION_B,
            CALL_A_EXPIRED,
            PHONE_A,
            SESSION_A_EXPIRED,
            CALL_HISTORICAL,
            PHONE_A,
        ),
    )


def _row_count(conn: psycopg.Connection[dict[str, Any]], table: str) -> int:
    row = conn.execute(f"select count(*) as n from {table}").fetchone()
    assert row is not None
    return int(row["n"])


@pytest.fixture(scope="module")
def migration_db() -> Iterator[MigrationDb]:
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True, connect_timeout=3)
    except (psycopg.OperationalError, OSError) as exc:
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    dbname = f"roadside_demo_rls_test_{uuid.uuid4().hex[:10]}"
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
        requests_before = _row_count(conn, "assistance_requests")
        sessions_before = _row_count(conn, "demo_sessions")
        conn.execute(target.read_text())
        requests_after = _row_count(conn, "assistance_requests")
        sessions_after = _row_count(conn, "demo_sessions")
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            conn=conn,
            requests_before=requests_before,
            requests_after=requests_after,
            sessions_before=sessions_before,
            sessions_after=sessions_after,
        )
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        admin.close()


def _set_claims(conn: psycopg.Connection[dict[str, Any]], claims: dict[str, Any] | None) -> None:
    if claims is None:
        conn.execute("reset request.jwt.claims")
    else:
        conn.execute(
            "select set_config('request.jwt.claims', %s, false)",
            (json.dumps(claims),),
        )


def _as_authenticated(
    conn: psycopg.Connection[dict[str, Any]],
    claims: dict[str, Any] | None,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Run a query as `authenticated` under the given JWT claims."""
    _set_claims(conn, claims)
    conn.execute("set role authenticated")
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.execute("reset role")
        _set_claims(conn, None)


def _anonymous_claims(user_id: str) -> dict[str, Any]:
    return {"sub": user_id, "is_anonymous": True}


def _permanent_claims(user_id: str) -> dict[str, Any]:
    return {"sub": user_id, "is_anonymous": False}


# ---------------------------------------------------------------------------
# Policy catalog state after the migration
# ---------------------------------------------------------------------------


class TestPolicyCatalog:
    """The new policies are scoped exactly as designed and the existing
    dispatcher/deny posture is untouched."""

    def test_rls_enabled_on_both_tables(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select relname, relrowsecurity
            from pg_class
            where relname in ('assistance_requests', 'demo_sessions')
            order by relname
            """
        ).fetchall()
        assert {row["relname"]: row["relrowsecurity"] for row in rows} == {
            "assistance_requests": True,
            "demo_sessions": True,
        }

    def test_existing_deny_policies_survive(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select tablename, policyname, cmd, qual
            from pg_policies
            where policyname in (%s, %s)
            order by tablename
            """,
            (ASSISTANCE_DENY_POLICY, DEMO_SESSION_DENY_POLICY),
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["cmd"] in ("*", "ALL")
            assert row["qual"] == "false"

    def test_dispatcher_policy_unchanged(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, roles, qual, with_check
            from pg_policies
            where tablename = 'assistance_requests' and policyname = %s
            """,
            (ASSISTANCE_DISPATCHER_POLICY,),
        ).fetchone()
        assert row is not None
        assert row["permissive"] == "PERMISSIVE"
        assert row["cmd"] == "SELECT"
        assert row["roles"] == ["authenticated"]
        assert row["qual"] == "true"

    def test_demo_restriction_policy_is_restrictive_select(
        self, migration_db: MigrationDb
    ) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, roles, qual
            from pg_policies
            where tablename = 'assistance_requests' and policyname = %s
            """,
            (ASSISTANCE_DEMO_POLICY,),
        ).fetchone()
        assert row is not None
        assert row["permissive"] == "RESTRICTIVE"
        assert row["cmd"] == "SELECT"
        assert row["roles"] == ["authenticated"]
        assert "demo_sessions" in row["qual"]
        assert "expires_at" in row["qual"]
        assert "auth.uid()" in row["qual"]

    def test_demo_session_own_row_policy_exists(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, roles, qual
            from pg_policies
            where tablename = 'demo_sessions' and policyname = %s
            """,
            (DEMO_SESSION_OWN_POLICY,),
        ).fetchone()
        assert row is not None
        assert row["permissive"] == "PERMISSIVE"
        assert row["cmd"] == "SELECT"
        assert row["roles"] == ["authenticated"]
        assert "auth_user_id" in row["qual"]
        assert "expires_at" in row["qual"]

    def test_no_write_policy_for_authenticated(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_policies
            where tablename in ('assistance_requests', 'demo_sessions')
              and cmd in ('INSERT', 'UPDATE', 'DELETE')
              and 'authenticated' = any(roles)
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0

    def test_no_policy_references_caller_phone(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select tablename, policyname, coalesce(qual, '') as qual,
                   coalesce(with_check, '') as with_check
            from pg_policies
            where tablename in ('assistance_requests', 'demo_sessions')
            """
        ).fetchall()
        assert len(rows) > 0
        for row in rows:
            assert "caller_phone" not in row["qual"], row["policyname"]
            assert "caller_phone" not in row["with_check"], row["policyname"]


# ---------------------------------------------------------------------------
# Anonymous demo sessions read only their own valid rows
# ---------------------------------------------------------------------------


class TestDemoUserAccess:
    """An authenticated anonymous session reaches exactly its own linked,
    unexpired assistance request and its own unexpired session row."""

    def _visible_calls(self, migration_db: MigrationDb, user_id: str) -> list[str]:
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(user_id),
            "select call_id from assistance_requests order by call_id",
        )
        return [row["call_id"] for row in rows]

    def test_demo_a_reads_only_own_linked_request(self, migration_db: MigrationDb) -> None:
        assert self._visible_calls(migration_db, USER_A) == [CALL_A]

    def test_demo_b_reads_only_own_linked_request(self, migration_db: MigrationDb) -> None:
        assert self._visible_calls(migration_db, USER_B) == [CALL_B]

    def test_expired_session_request_not_visible(self, migration_db: MigrationDb) -> None:
        # USER_A also owns SESSION_A_EXPIRED; its linked request must stay hidden.
        assert CALL_A_EXPIRED not in self._visible_calls(migration_db, USER_A)

    def test_guessing_another_request_id_exposes_nothing(
        self, migration_db: MigrationDb
    ) -> None:
        target = migration_db.conn.execute(
            "select id from assistance_requests where call_id = %s",
            (CALL_B,),
        ).fetchone()
        assert target is not None
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_A),
            "select call_id from assistance_requests where id = %s",
            (target["id"],),
        )
        assert rows == []

    def test_caller_phone_alone_grants_nothing(self, migration_db: MigrationDb) -> None:
        # USER_A's phone also appears on an expired-session request and on an
        # unlinked historical request; only the valid linked row may appear.
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_A),
            "select call_id from assistance_requests where caller_phone = %s order by call_id",
            (PHONE_A,),
        )
        assert [row["call_id"] for row in rows] == [CALL_A]

    def test_demo_a_reads_only_own_demo_session(self, migration_db: MigrationDb) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_A),
            "select id::text as id from demo_sessions order by id",
        )
        assert [row["id"] for row in rows] == [SESSION_A]

    def test_demo_b_reads_only_own_demo_session(self, migration_db: MigrationDb) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_B),
            "select id::text as id from demo_sessions order by id",
        )
        assert [row["id"] for row in rows] == [SESSION_B]

    def test_expired_demo_session_row_not_readable(self, migration_db: MigrationDb) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_A),
            "select id from demo_sessions where id = %s",
            (SESSION_A_EXPIRED,),
        )
        assert rows == []


# ---------------------------------------------------------------------------
# Dispatcher access is unchanged
# ---------------------------------------------------------------------------


class TestDispatcherUnchanged:
    """A permanent (non-anonymous) authenticated user still reads every row,
    with or without JWT claims — the plain-PostgreSQL test convention."""

    def test_permanent_user_jwt_reads_all_requests(self, migration_db: MigrationDb) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            _permanent_claims(USER_DISPATCHER),
            "select call_id from assistance_requests order by call_id",
        )
        assert [row["call_id"] for row in rows] == ALL_CALLS

    def test_authenticated_without_jwt_claims_reads_all(
        self, migration_db: MigrationDb
    ) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            None,
            "select call_id from assistance_requests order by call_id",
        )
        assert [row["call_id"] for row in rows] == ALL_CALLS


# ---------------------------------------------------------------------------
# Unauthenticated roles stay fully denied
# ---------------------------------------------------------------------------


class TestUnauthenticatedDenied:
    """The anon role keeps table grants (Supabase default privileges) but
    sees zero rows on both tables because only the deny policies apply."""

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
        conn.execute("grant select on assistance_requests, demo_sessions to anon")
        try:
            conn.execute("set role anon")
            try:
                requests = conn.execute(
                    "select count(*) as n from assistance_requests"
                ).fetchone()
                sessions = conn.execute("select count(*) as n from demo_sessions").fetchone()
            finally:
                conn.execute("reset role")
            assert requests is not None
            assert sessions is not None
            assert requests["n"] == 0
            assert sessions["n"] == 0
        finally:
            conn.execute("revoke select on assistance_requests, demo_sessions from anon")
            if created_anon:
                conn.execute("drop role anon")


# ---------------------------------------------------------------------------
# Demo sessions remain read-only for client roles
# ---------------------------------------------------------------------------


class TestDemoUserCannotWrite:
    """Even with Supabase default table grants, an anonymous demo session
    cannot insert, update, or delete either table."""

    def test_assistance_requests_insert_denied(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        conn.execute("grant insert on assistance_requests to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        """
                        insert into assistance_requests (call_id, location, vehicle, issue, status)
                        values (%s, 'A', 'B', 'C', 'pending')
                        """,
                        (f"CA_DEMO_INSERT_{uuid.uuid4().hex[:8]}",),
                    )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
        finally:
            conn.execute("revoke insert on assistance_requests from authenticated")

    def test_assistance_requests_update_delete_match_no_rows(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        conn.execute("grant update, delete on assistance_requests to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                updated = conn.execute(
                    "update assistance_requests set location = 'HACKED' where call_id = %s",
                    (CALL_B,),
                )
                deleted = conn.execute(
                    "delete from assistance_requests where call_id = %s",
                    (CALL_B,),
                )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
            assert updated.rowcount == 0
            assert deleted.rowcount == 0
        finally:
            conn.execute("revoke update, delete on assistance_requests from authenticated")
        row = conn.execute(
            "select location from assistance_requests where call_id = %s",
            (CALL_B,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "2 B St"

    def test_demo_sessions_insert_denied(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        conn.execute("grant insert on demo_sessions to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        """
                        insert into demo_sessions (auth_user_id, phone_hmac, phone_last4, expires_at)
                        values (%s, %s, '9999', now() + interval '15 minutes')
                        """,
                        (USER_A, "f" * 64),
                    )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
        finally:
            conn.execute("revoke insert on demo_sessions from authenticated")

    def test_demo_sessions_update_delete_match_no_rows(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        conn.execute("grant update, delete on demo_sessions to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                updated = conn.execute(
                    "update demo_sessions set phone_last4 = '0000' where id = %s",
                    (SESSION_B,),
                )
                deleted = conn.execute(
                    "delete from demo_sessions where id = %s",
                    (SESSION_B,),
                )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
            assert updated.rowcount == 0
            assert deleted.rowcount == 0
        finally:
            conn.execute("revoke update, delete on demo_sessions from authenticated")
        row = conn.execute(
            "select phone_last4 from demo_sessions where id = %s",
            (SESSION_B,),
        ).fetchone()
        assert row is not None
        assert row["phone_last4"] == "2222"


# ---------------------------------------------------------------------------
# Application-order regression: full chain still applies, no data changes
# ---------------------------------------------------------------------------


class TestChainStillAppliesCleanly:
    """The policy-only migration applies without touching data."""

    def test_row_counts_unchanged_by_migration(self, migration_db: MigrationDb) -> None:
        assert migration_db.requests_after == migration_db.requests_before
        assert migration_db.sessions_after == migration_db.sessions_before

    def test_seed_data_untouched(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, status, intake_status, demo_session_id
            from assistance_requests where call_id = %s
            """,
            (CALL_A,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "1 A St"
        assert row["vehicle"] == "Toyota"
        assert row["issue"] == "Flat tire"
        assert row["status"] == "completed"
        assert row["intake_status"] == "completed"
        assert row["demo_session_id"] == uuid.UUID(SESSION_A)
