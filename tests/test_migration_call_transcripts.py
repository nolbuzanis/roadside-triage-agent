"""Schema tests for the call_transcripts table migration.

Applies the real Supabase migration chain to a throwaway database on a local
PostgreSQL server, mirroring production ordering:

    migrations before target → create supabase_realtime publication →
    seed rows → target migration → later migrations

The target creates the table itself, so transcript rows are seeded after the
target applies (as the test superuser, which bypasses RLS); the pre-existing
demo_sessions / assistance_requests seed proves the migration leaves existing
data and policies untouched.

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
TARGET_MIGRATION = "20260926000000_create_call_transcripts.sql"

PUBLICATION_NAME = "supabase_realtime"

USER_A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_DISPATCHER = "44444444-dddd-4ddd-8ddd-dddddddddddd"

SESSION_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SESSION_B = "bbbbbbbb-0000-4000-8000-00000000000b"
SESSION_A_EXPIRED = "eeeeeeee-0000-4000-8000-00000000eeee"

CALL_A = "CA_TRANSCRIPT_A"
CALL_B = "CA_TRANSCRIPT_B"
CALL_A_EXPIRED = "CA_TRANSCRIPT_A_EXPIRED"
CALL_NON_DEMO = "CA_TRANSCRIPT_NON_DEMO"
CALL_REQUEST_SEED = "CA_TRANSCRIPT_REQUEST_SEED"

DENY_POLICY = "Anonymous cannot access call transcripts"
DISPATCHER_POLICY = "Dispatcher can read call transcripts"
DEMO_POLICY = "Demo users read only their linked call transcripts"
ASSISTANCE_DISPATCHER_POLICY = "Dispatcher can read assistance requests"


@dataclass
class MigrationDb:
    conn: psycopg.Connection[dict[str, Any]]
    target_sql: str
    requests_before: int
    requests_after: int


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


def _seed_base_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Insert demo sessions and assistance requests that pre-date the target."""
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
          (%s, 'sess_t', '+15550000311', '1 T St', 'Toyota', 'Flat tire',
            'completed', 'completed', false, null, 'sent', %s),
          (%s, 'sess_hist', '+15550000399', '2 H St', 'Subaru', 'Old request',
            'completed', 'completed', false, null, 'sent', null)
        """,
        (CALL_REQUEST_SEED, SESSION_A, f"{CALL_REQUEST_SEED}_HIST"),
    )


def _seed_transcript_rows(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Insert transcript turns covering own/other/expired/unlinked sessions."""
    conn.execute(
        """
        insert into call_transcripts (call_id, demo_session_id, speaker, text, seq)
        values
          (%s, %s, 'caller', 'I am at 1 T St', 0),
          (%s, %s, 'assistant', 'Got it, what vehicle?', 1),
          (%s, %s, 'caller', 'My battery died', 0),
          (%s, %s, 'caller', 'Old expired turn', 0),
          (%s, null, 'caller', 'Non-demo production turn', 0)
        """,
        (
            CALL_A,
            SESSION_A,
            CALL_A,
            SESSION_A,
            CALL_B,
            SESSION_B,
            CALL_A_EXPIRED,
            SESSION_A_EXPIRED,
            CALL_NON_DEMO,
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

    dbname = f"roadside_call_transcripts_test_{uuid.uuid4().hex[:10]}"
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
        _seed_base_rows(conn)
        requests_before = _row_count(conn, "assistance_requests")
        target_sql = target.read_text()
        conn.execute(target_sql)
        _seed_transcript_rows(conn)
        requests_after = _row_count(conn, "assistance_requests")
        for path in after:
            conn.execute(path.read_text())
        yield MigrationDb(
            conn=conn,
            target_sql=target_sql,
            requests_before=requests_before,
            requests_after=requests_after,
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
# Table shape
# ---------------------------------------------------------------------------


class TestTableSchema:
    """The table stores one ordered turn per row with the designed guards."""

    def test_expected_columns(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select column_name, data_type, is_nullable, column_default
            from information_schema.columns
            where table_schema = 'public' and table_name = 'call_transcripts'
            order by ordinal_position
            """
        ).fetchall()
        by_name = {row["column_name"]: row for row in rows}
        assert set(by_name) == {
            "id",
            "demo_session_id",
            "call_id",
            "speaker",
            "text",
            "seq",
            "created_at",
        }
        assert by_name["id"]["data_type"] == "uuid"
        assert by_name["demo_session_id"]["data_type"] == "uuid"
        assert by_name["demo_session_id"]["is_nullable"] == "YES"
        assert by_name["call_id"]["is_nullable"] == "NO"
        assert by_name["speaker"]["is_nullable"] == "NO"
        assert by_name["text"]["is_nullable"] == "NO"
        assert by_name["seq"]["data_type"] == "integer"
        assert by_name["seq"]["is_nullable"] == "NO"
        assert by_name["created_at"]["column_default"] is not None

    def test_speaker_check_constraint(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                insert into call_transcripts (call_id, speaker, text, seq)
                values ('CA_BAD_SPEAKER', 'system', 'hello', 0)
                """
            )

    def test_text_must_be_non_empty(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                insert into call_transcripts (call_id, speaker, text, seq)
                values ('CA_EMPTY_TEXT', 'caller', '', 0)
                """
            )

    def test_seq_must_be_non_negative(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                insert into call_transcripts (call_id, speaker, text, seq)
                values ('CA_NEG_SEQ', 'caller', 'hello', -1)
                """
            )

    def test_seq_unique_per_call_but_reusable_across_calls(
        self, migration_db: MigrationDb
    ) -> None:
        conn = migration_db.conn
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                """
                insert into call_transcripts (call_id, demo_session_id, speaker, text, seq)
                values (%s, %s, 'caller', 'duplicate seq', 0)
                """,
                (CALL_A, SESSION_A),
            )
        row = conn.execute(
            """
            insert into call_transcripts (call_id, speaker, text, seq)
            values ('CA_OTHER_SEQ_SCOPE', 'caller', 'same seq other call', 0)
            returning id
            """
        ).fetchone()
        assert row is not None
        conn.execute("delete from call_transcripts where call_id = 'CA_OTHER_SEQ_SCOPE'")

    def test_demo_session_fk_set_null_on_delete(self, migration_db: MigrationDb) -> None:
        fk = migration_db.conn.execute(
            """
            select confdeltype
            from pg_constraint
            where conrelid = 'public.call_transcripts'::regclass
              and contype = 'f'
            """
        ).fetchone()
        assert fk is not None
        # 'n' = SET NULL: removing an expired demo session never orphans turns.
        assert fk["confdeltype"] == "n"

    def test_ordering_indexes_exist(self, migration_db: MigrationDb) -> None:
        rows = migration_db.conn.execute(
            """
            select indexname
            from pg_indexes
            where schemaname = 'public' and tablename = 'call_transcripts'
            """
        ).fetchall()
        names = {row["indexname"] for row in rows}
        assert "call_transcripts_demo_session_id_idx" in names
        assert "call_transcripts_call_id_seq_idx" in names


# ---------------------------------------------------------------------------
# Policy catalog state after the migration
# ---------------------------------------------------------------------------


class TestPolicyCatalog:
    """The new policies scope reads exactly as designed."""

    def test_rls_enabled(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            "select relrowsecurity from pg_class where relname = 'call_transcripts'"
        ).fetchone()
        assert row is not None
        assert row["relrowsecurity"] is True

    def test_deny_policy_blocks_everything(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, qual
            from pg_policies
            where tablename = 'call_transcripts' and policyname = %s
            """,
            (DENY_POLICY,),
        ).fetchone()
        assert row is not None
        assert row["cmd"] in ("*", "ALL")
        assert row["qual"] == "false"

    def test_dispatcher_policy_is_blanket_select(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, roles, qual
            from pg_policies
            where tablename = 'call_transcripts' and policyname = %s
            """,
            (DISPATCHER_POLICY,),
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
            where tablename = 'call_transcripts' and policyname = %s
            """,
            (DEMO_POLICY,),
        ).fetchone()
        assert row is not None
        assert row["permissive"] == "RESTRICTIVE"
        assert row["cmd"] == "SELECT"
        assert row["roles"] == ["authenticated"]
        assert "demo_sessions" in row["qual"]
        assert "demo_session_id" in row["qual"]
        assert "expires_at" in row["qual"]
        assert "auth.uid()" in row["qual"]

    def test_no_write_policy_for_authenticated(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_policies
            where tablename = 'call_transcripts'
              and cmd in ('INSERT', 'UPDATE', 'DELETE')
              and 'authenticated' = any(roles)
            """
        ).fetchone()
        assert row is not None
        assert row["n"] == 0


# ---------------------------------------------------------------------------
# Realtime publication membership
# ---------------------------------------------------------------------------


class TestPublicationMembership:
    """call_transcripts is streamed by Supabase Realtime after the migration."""

    def test_table_added_to_realtime_publication(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_publication_tables
            where pubname = %s
              and schemaname = 'public'
              and tablename = 'call_transcripts'
            """,
            (PUBLICATION_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1

    def test_migration_is_idempotent(self, migration_db: MigrationDb) -> None:
        migration_db.conn.execute(migration_db.target_sql)
        row = migration_db.conn.execute(
            """
            select count(*) as n
            from pg_publication_tables
            where pubname = %s and tablename = 'call_transcripts'
            """,
            (PUBLICATION_NAME,),
        ).fetchone()
        assert row is not None
        assert row["n"] == 1


# ---------------------------------------------------------------------------
# Anonymous demo sessions read only their own valid rows
# ---------------------------------------------------------------------------


class TestDemoUserAccess:
    """An authenticated anonymous session reaches exactly its own session's
    turns, ordered by seq."""

    def _visible_turns(
        self, migration_db: MigrationDb, user_id: str
    ) -> list[tuple[str, str, int]]:
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(user_id),
            "select call_id, speaker, seq from call_transcripts order by call_id, seq",
        )
        return [(row["call_id"], row["speaker"], row["seq"]) for row in rows]

    def test_demo_a_reads_only_own_turns(self, migration_db: MigrationDb) -> None:
        assert self._visible_turns(migration_db, USER_A) == [
            (CALL_A, "caller", 0),
            (CALL_A, "assistant", 1),
        ]

    def test_demo_b_reads_only_own_turns(self, migration_db: MigrationDb) -> None:
        assert self._visible_turns(migration_db, USER_B) == [
            (CALL_B, "caller", 0),
        ]

    def test_expired_session_turns_not_visible(self, migration_db: MigrationDb) -> None:
        # USER_A also owns SESSION_A_EXPIRED; its turns must stay hidden.
        assert CALL_A_EXPIRED not in [
            call_id for call_id, _, _ in self._visible_turns(migration_db, USER_A)
        ]

    def test_non_demo_turns_not_visible_to_demo_sessions(
        self, migration_db: MigrationDb
    ) -> None:
        # Rows with a null demo_session_id belong to no session.
        for user_id in (USER_A, USER_B):
            assert CALL_NON_DEMO not in [
                call_id for call_id, _, _ in self._visible_turns(migration_db, user_id)
            ]

    def test_guessing_another_turn_id_exposes_nothing(
        self, migration_db: MigrationDb
    ) -> None:
        target = migration_db.conn.execute(
            "select id from call_transcripts where call_id = %s",
            (CALL_B,),
        ).fetchone()
        assert target is not None
        rows = _as_authenticated(
            migration_db.conn,
            _anonymous_claims(USER_A),
            "select call_id from call_transcripts where id = %s",
            (target["id"],),
        )
        assert rows == []


# ---------------------------------------------------------------------------
# Dispatcher access reads everything
# ---------------------------------------------------------------------------


class TestDispatcherUnchanged:
    """A permanent (non-anonymous) authenticated user still reads every row,
    with or without JWT claims — the plain-PostgreSQL test convention."""

    def test_permanent_user_jwt_reads_all_turns(self, migration_db: MigrationDb) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            _permanent_claims(USER_DISPATCHER),
            "select call_id from call_transcripts order by call_id",
        )
        assert [row["call_id"] for row in rows] == [
            CALL_A,
            CALL_A,
            CALL_A_EXPIRED,
            CALL_B,
            CALL_NON_DEMO,
        ]

    def test_authenticated_without_jwt_claims_reads_all(
        self, migration_db: MigrationDb
    ) -> None:
        rows = _as_authenticated(
            migration_db.conn,
            None,
            "select count(*) as n from call_transcripts",
        )
        assert rows[0]["n"] == 5


# ---------------------------------------------------------------------------
# Unauthenticated roles stay fully denied
# ---------------------------------------------------------------------------


class TestUnauthenticatedDenied:
    """The anon role keeps table grants (Supabase default privileges) but
    sees zero rows because only the deny policy applies."""

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
        conn.execute("grant select on call_transcripts to anon")
        try:
            conn.execute("set role anon")
            try:
                count = conn.execute(
                    "select count(*) as n from call_transcripts"
                ).fetchone()
            finally:
                conn.execute("reset role")
            assert count is not None
            assert count["n"] == 0
        finally:
            conn.execute("revoke select on call_transcripts from anon")
            if created_anon:
                conn.execute("drop role anon")


# ---------------------------------------------------------------------------
# Demo sessions remain read-only for client roles
# ---------------------------------------------------------------------------


class TestDemoUserCannotWrite:
    """Even with Supabase default table grants, an anonymous demo session
    cannot insert, update, or delete transcript rows."""

    def test_insert_denied(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        conn.execute("grant insert on call_transcripts to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        """
                        insert into call_transcripts (call_id, demo_session_id, speaker, text, seq)
                        values ('CA_HACK', %s, 'caller', 'hi', 0)
                        """,
                        (SESSION_A,),
                    )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
        finally:
            conn.execute("revoke insert on call_transcripts from authenticated")

    def test_update_delete_match_no_rows(self, migration_db: MigrationDb) -> None:
        conn = migration_db.conn
        conn.execute("grant update, delete on call_transcripts to authenticated")
        try:
            _set_claims(conn, _anonymous_claims(USER_A))
            conn.execute("set role authenticated")
            try:
                updated = conn.execute(
                    "update call_transcripts set text = 'HACKED' where call_id = %s",
                    (CALL_B,),
                )
                deleted = conn.execute(
                    "delete from call_transcripts where call_id = %s",
                    (CALL_B,),
                )
            finally:
                conn.execute("reset role")
                _set_claims(conn, None)
            assert updated.rowcount == 0
            assert deleted.rowcount == 0
        finally:
            conn.execute("revoke update, delete on call_transcripts from authenticated")
        row = conn.execute(
            "select text from call_transcripts where call_id = %s",
            (CALL_B,),
        ).fetchone()
        assert row is not None
        assert row["text"] == "My battery died"


# ---------------------------------------------------------------------------
# Existing assistance_requests behavior is unchanged
# ---------------------------------------------------------------------------


class TestExistingBehaviorUntouched:
    """The transcript migration touches no existing table, row, or policy."""

    def test_request_row_counts_unchanged_by_migration(
        self, migration_db: MigrationDb
    ) -> None:
        assert migration_db.requests_after == migration_db.requests_before

    def test_seed_request_intact(self, migration_db: MigrationDb) -> None:
        row = migration_db.conn.execute(
            """
            select location, vehicle, issue, intake_status, demo_session_id
            from assistance_requests where call_id = %s
            """,
            (CALL_REQUEST_SEED,),
        ).fetchone()
        assert row is not None
        assert row["location"] == "1 T St"
        assert row["vehicle"] == "Toyota"
        assert row["issue"] == "Flat tire"
        assert row["intake_status"] == "completed"
        assert row["demo_session_id"] == uuid.UUID(SESSION_A)

    def test_dispatcher_request_policy_unchanged(
        self, migration_db: MigrationDb
    ) -> None:
        row = migration_db.conn.execute(
            """
            select permissive, cmd, roles, qual
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
