"""Tests for ticket persistence service (app/services/tickets.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_supabase(*, existing_data: list | None = None, insert_data: list | None = None) -> MagicMock:
    """Build a mock Supabase client with chained table().select/insert/update."""
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    # select chain: table.select("*").eq(...).execute()
    select_result = MagicMock()
    select_result.data = existing_data or []
    table.select.return_value = table
    table.eq.return_value = table
    table.execute.return_value = select_result

    # insert chain: table.insert(row).execute() — separate mock so insert
    # results don't collide with the select result.
    insert_result = MagicMock()
    insert_result.data = insert_data or []
    insert_chain = MagicMock()
    insert_chain.execute.return_value = insert_result
    table.insert.return_value = insert_chain

    return client


# ---------------------------------------------------------------------------
# create_ticket — new ticket
# ---------------------------------------------------------------------------


class TestCreateTicketNew:
    """Tests for creating a brand-new ticket."""

    @patch("app.services.tickets._get_supabase")
    def test_creates_ticket_with_correct_fields(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase(insert_data=[{"id": "uuid-1", "call_id": "CA_new"}])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_new",
            caller_phone="+15551234567",
            location="123 Main St",
            vehicle="Toyota Camry",
            issue="Flat tire",
        )

        assert ticket["call_id"] == "CA_new"
        table = sb.table.return_value
        table.insert.assert_called_once()

    @patch("app.services.tickets._get_supabase")
    def test_default_status_is_in_progress(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_status",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )

        table = sb.table.return_value
        insert_call = table.insert.call_args
        row = insert_call[0][0]
        assert row["status"] == "in_progress"

    @patch("app.services.tickets._get_supabase")
    def test_default_notification_status_is_pending(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_notif",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )

        table = sb.table.return_value
        row = table.insert.call_args[0][0]
        assert row["notification_status"] == "pending"

    @patch("app.services.tickets._get_supabase")
    def test_caller_phone_is_stored(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_phone",
            caller_phone="+15559876543",
            location="A",
            vehicle="B",
            issue="C",
        )

        row = sb.table.return_value.insert.call_args[0][0]
        assert row["caller_phone"] == "+15559876543"

    @patch("app.services.tickets._get_supabase")
    def test_location_vehicle_issue_are_stored(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_data",
            caller_phone="+15550000000",
            location="Highway 101 NB",
            vehicle="Honda Civic 2022",
            issue="Engine overheating",
        )

        row = sb.table.return_value.insert.call_args[0][0]
        assert row["location"] == "Highway 101 NB"
        assert row["vehicle"] == "Honda Civic 2022"
        assert row["issue"] == "Engine overheating"

    @patch("app.services.tickets._get_supabase")
    def test_session_id_is_included_when_provided(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_session",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
            session_id="sess_abc123",
        )

        row = sb.table.return_value.insert.call_args[0][0]
        assert row["session_id"] == "sess_abc123"

    @patch("app.services.tickets._get_supabase")
    def test_session_id_is_omitted_when_none(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_nosess",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )

        row = sb.table.return_value.insert.call_args[0][0]
        assert "session_id" not in row

    @patch("app.services.tickets._get_supabase")
    def test_partial_insert_omits_missing_fields(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_partial",
            caller_phone="+15550000000",
            location="123 Main St",
        )

        row = sb.table.return_value.insert.call_args[0][0]
        assert row["location"] == "123 Main St"
        assert "vehicle" not in row
        assert "issue" not in row
        assert row["status"] == "in_progress"
        assert row["notification_status"] == "pending"


# ---------------------------------------------------------------------------
# create_ticket — merge-upsert (duplicate call_id)
# ---------------------------------------------------------------------------


class TestCreateTicketMergeUpsert:
    """Tests for merge-upsert semantics on duplicate call_id."""

    @patch("app.services.tickets._get_supabase")
    def test_duplicate_call_id_merges_provided_fields(self, mock_get_sb: MagicMock) -> None:
        existing = {
            "id": "uuid-existing",
            "call_id": "CA_dup",
            "location": "Already St",
            "vehicle": None,
            "issue": None,
        }
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_dup",
            caller_phone="+15550000000",
            location="New St",
            vehicle="Toyota Camry",
        )

        assert ticket["id"] == "uuid-existing"
        assert ticket["location"] == "New St"
        assert ticket["vehicle"] == "Toyota Camry"
        assert ticket["issue"] is None  # omitted field is never nulled-invented

    @patch("app.services.tickets._get_supabase")
    def test_omitted_fields_are_preserved_not_nulled(self, mock_get_sb: MagicMock) -> None:
        existing = {
            "id": "uuid-1",
            "call_id": "CA_keep",
            "location": "123 Main St",
            "vehicle": "Honda Civic",
            "issue": None,
        }
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_keep",
            caller_phone="+15550000000",
            issue="Flat tire",
        )

        assert ticket["location"] == "123 Main St"
        assert ticket["vehicle"] == "Honda Civic"
        assert ticket["issue"] == "Flat tire"

    @patch("app.services.tickets._get_supabase")
    def test_merge_update_payload_contains_only_provided_fields(
        self, mock_get_sb: MagicMock
    ) -> None:
        existing = {"id": "uuid-2", "call_id": "CA_payload", "location": "Old"}
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_payload",
            caller_phone="+15550000000",
            issue="Blown tire",
        )

        table = sb.table.return_value
        table.insert.assert_not_called()
        update_payload = table.update.call_args[0][0]
        assert update_payload == {"issue": "Blown tire"}

    @patch("app.services.tickets._get_supabase")
    def test_duplicate_call_id_does_not_insert(self, mock_get_sb: MagicMock) -> None:
        existing = {"id": "uuid-existing", "call_id": "CA_dup2"}
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        create_ticket(
            call_id="CA_dup2",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )

        sb.table.return_value.insert.assert_not_called()

    @patch("app.services.tickets._get_supabase")
    def test_second_request_with_same_call_id_updates_same_row(
        self, mock_get_sb: MagicMock
    ) -> None:
        existing = {"id": "uuid-first", "call_id": "CA_same", "status": "pending"}
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        first = create_ticket(
            call_id="CA_same",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )
        second = create_ticket(
            call_id="CA_same",
            caller_phone="+15550000000",
            location="Different",
        )

        assert first["id"] == second["id"]
        assert second["location"] == "Different"
        assert second["status"] == "pending"  # status column untouched

    @patch("app.services.tickets._get_supabase")
    def test_nothing_to_save_raises_value_error(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        with pytest.raises(ValueError, match="nothing to save"):
            create_ticket(
                call_id="CA_empty",
                caller_phone="+15550000000",
                location=None,
                vehicle=None,
                issue=None,
            )

        sb.table.return_value.insert.assert_not_called()

    @patch("app.services.tickets._get_supabase")
    def test_concurrent_insert_race_merges_into_existing_row(
        self, mock_get_sb: MagicMock
    ) -> None:
        from postgrest.exceptions import APIError

        existing = {"id": "uuid-race", "call_id": "CA_race", "location": "Winner St"}
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table

        empty_result = MagicMock()
        empty_result.data = []
        existing_result = MagicMock()
        existing_result.data = [existing]
        # First select (before insert) finds nothing; select after the
        # unique-violation finds the winning row.
        table.select.return_value = table
        table.eq.return_value = table
        table.execute.side_effect = [empty_result, existing_result]

        insert_chain = MagicMock()
        insert_chain.execute.side_effect = APIError(
            {"code": "23505", "message": "duplicate key", "details": None, "hint": None}
        )
        table.insert.return_value = insert_chain

        update_chain = MagicMock()
        update_chain.eq.return_value = update_chain
        table.update.return_value = update_chain

        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_race",
            caller_phone="+15550000000",
            issue="Flat tire",
        )

        assert ticket["id"] == "uuid-race"
        assert ticket["location"] == "Winner St"
        assert ticket["issue"] == "Flat tire"
        update_payload = table.update.call_args[0][0]
        assert update_payload == {"issue": "Flat tire"}

    @patch("app.services.tickets._get_supabase")
    def test_non_unique_api_error_is_raised(self, mock_get_sb: MagicMock) -> None:
        from postgrest.exceptions import APIError

        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table

        empty_result = MagicMock()
        empty_result.data = []
        table.select.return_value = table
        table.eq.return_value = table
        table.execute.return_value = empty_result

        insert_chain = MagicMock()
        insert_chain.execute.side_effect = APIError(
            {"code": "42501", "message": "permission denied", "details": None, "hint": None}
        )
        table.insert.return_value = insert_chain
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        with pytest.raises(APIError):
            create_ticket(
                call_id="CA_perm",
                caller_phone="+15550000000",
                location="A",
            )


# ---------------------------------------------------------------------------
# start_assistance_request — idempotent early creation at call start
# ---------------------------------------------------------------------------


def _stateful_supabase() -> tuple[MagicMock, dict[str, dict]]:
    """Build a mock Supabase client backed by an in-memory row store.

    select().eq().execute() reads from the store keyed by call_id;
    insert().execute() writes into it. Enables idempotency/retry tests.
    """
    rows: dict[str, dict] = {}
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    def _select_execute(*args: object, **kwargs: object) -> MagicMock:
        call_id = table.eq.call_args[0][1] if table.eq.call_args else None
        row = rows.get(str(call_id))
        result = MagicMock()
        result.data = [row] if row else []
        return result

    table.select.return_value = table
    table.eq.return_value = table
    table.execute.side_effect = _select_execute

    def _insert(row: dict) -> MagicMock:
        chain = MagicMock()

        def _do_insert(*args: object, **kwargs: object) -> MagicMock:
            stored = dict(row)
            stored.setdefault("id", f"uuid-{row['call_id']}")
            rows[row["call_id"]] = stored
            result = MagicMock()
            result.data = [stored]
            return result

        chain.execute.side_effect = _do_insert
        return chain

    table.insert.side_effect = _insert
    return client, rows


class TestStartAssistanceRequest:
    """Tests for idempotent early creation of the assistance-request row."""

    @patch("app.services.tickets._get_supabase")
    def test_inserts_open_row_with_null_intake_fields(
        self, mock_get_sb: MagicMock
    ) -> None:
        sb = _mock_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        start_assistance_request(call_id="CA_early", caller_phone="+15551234567")

        table = sb.table.return_value
        row = table.insert.call_args[0][0]
        assert row["call_id"] == "CA_early"
        assert row["caller_phone"] == "+15551234567"
        assert row["status"] == "in_progress"
        assert row["notification_status"] == "pending"
        assert "location" not in row
        assert "vehicle" not in row
        assert "issue" not in row

    @patch("app.services.tickets._get_supabase")
    def test_existing_row_returned_without_insert(
        self, mock_get_sb: MagicMock
    ) -> None:
        existing = {
            "id": "uuid-existing",
            "call_id": "CA_dup",
            "status": "in_progress",
            "location": None,
        }
        sb = _mock_supabase(existing_data=[existing])
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        row = start_assistance_request(call_id="CA_dup", caller_phone="+15550000000")

        assert row["id"] == "uuid-existing"
        sb.table.return_value.insert.assert_not_called()

    @patch("app.services.tickets._get_supabase")
    def test_webhook_retry_yields_exactly_one_row(
        self, mock_get_sb: MagicMock
    ) -> None:
        sb, rows = _stateful_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        first = start_assistance_request(call_id="CA_retry", caller_phone="+15551111111")
        second = start_assistance_request(call_id="CA_retry", caller_phone="+15551111111")

        assert first["id"] == second["id"]
        assert len(rows) == 1
        assert sb.table.return_value.insert.call_count == 1

    @patch("app.services.tickets._get_supabase")
    def test_concurrent_calls_create_isolated_rows(
        self, mock_get_sb: MagicMock
    ) -> None:
        sb, rows = _stateful_supabase()
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        row_a = start_assistance_request(call_id="CA_call_a", caller_phone="+15551111111")
        row_b = start_assistance_request(call_id="CA_call_b", caller_phone="+15552222222")

        assert len(rows) == 2
        assert row_a["call_id"] == "CA_call_a"
        assert row_a["caller_phone"] == "+15551111111"
        assert row_b["call_id"] == "CA_call_b"
        assert row_b["caller_phone"] == "+15552222222"
        assert row_a["id"] != row_b["id"]

    @patch("app.services.tickets._get_supabase")
    def test_concurrent_insert_race_returns_existing_row(
        self, mock_get_sb: MagicMock
    ) -> None:
        from postgrest.exceptions import APIError

        existing = {"id": "uuid-race", "call_id": "CA_race", "status": "in_progress"}
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table

        empty_result = MagicMock()
        empty_result.data = []
        existing_result = MagicMock()
        existing_result.data = [existing]
        table.select.return_value = table
        table.eq.return_value = table
        table.execute.side_effect = [empty_result, existing_result]

        insert_chain = MagicMock()
        insert_chain.execute.side_effect = APIError(
            {"code": "23505", "message": "duplicate key", "details": None, "hint": None}
        )
        table.insert.return_value = insert_chain
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        row = start_assistance_request(call_id="CA_race", caller_phone="+15550000000")

        assert row["id"] == "uuid-race"
        table.update.assert_not_called()

    @patch("app.services.tickets._get_supabase")
    def test_non_unique_api_error_is_raised(self, mock_get_sb: MagicMock) -> None:
        from postgrest.exceptions import APIError

        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table

        empty_result = MagicMock()
        empty_result.data = []
        table.select.return_value = table
        table.eq.return_value = table
        table.execute.return_value = empty_result

        insert_chain = MagicMock()
        insert_chain.execute.side_effect = APIError(
            {"code": "42501", "message": "permission denied", "details": None, "hint": None}
        )
        table.insert.return_value = insert_chain
        mock_get_sb.return_value = sb

        from app.services.tickets import start_assistance_request

        with pytest.raises(APIError):
            start_assistance_request(call_id="CA_perm", caller_phone="+15550000000")


# ---------------------------------------------------------------------------
# is_intake_complete
# ---------------------------------------------------------------------------


class TestIsIntakeComplete:
    """Tests for the completion check used to gate the dispatcher SMS."""

    def test_all_fields_non_empty_is_complete(self) -> None:
        from app.services.tickets import is_intake_complete

        assert is_intake_complete(
            {"location": "A", "vehicle": "B", "issue": "C"}
        )

    def test_missing_field_is_incomplete(self) -> None:
        from app.services.tickets import is_intake_complete

        assert not is_intake_complete({"location": "A", "vehicle": "B"})

    def test_null_field_is_incomplete(self) -> None:
        from app.services.tickets import is_intake_complete

        assert not is_intake_complete(
            {"location": "A", "vehicle": "B", "issue": None}
        )

    def test_empty_string_field_is_incomplete(self) -> None:
        from app.services.tickets import is_intake_complete

        assert not is_intake_complete(
            {"location": "A", "vehicle": "", "issue": "C"}
        )


# ---------------------------------------------------------------------------
# create_ticket — returns inserted row on success
# ---------------------------------------------------------------------------


class TestCreateTicketReturnsInserted:
    """Verify the returned dict matches what Supabase would return."""

    @patch("app.services.tickets._get_supabase")
    def test_returns_inserted_row_from_supabase(self, mock_get_sb: MagicMock) -> None:
        inserted = {
            "id": "uuid-new",
            "call_id": "CA_new",
            "caller_phone": "+15550000000",
            "location": "X",
            "vehicle": "Y",
            "issue": "Z",
            "status": "in_progress",
            "notification_status": "pending",
        }
        # Build mock manually so insert().execute() returns the inserted row.
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        select_result = MagicMock()
        select_result.data = []
        table.select.return_value = table
        table.eq.return_value = table
        table.execute.return_value = select_result
        insert_result = MagicMock()
        insert_result.data = [inserted]
        table.insert.return_value = MagicMock(execute=MagicMock(return_value=insert_result))
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_new",
            caller_phone="+15550000000",
            location="X",
            vehicle="Y",
            issue="Z",
        )

        assert ticket["id"] == "uuid-new"
        assert ticket["status"] == "in_progress"

    @patch("app.services.tickets._get_supabase")
    def test_falls_back_to_row_when_no_data_returned(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_supabase(insert_data=[])
        mock_get_sb.return_value = sb

        from app.services.tickets import create_ticket

        ticket = create_ticket(
            call_id="CA_fallback",
            caller_phone="+15550000000",
            location="A",
            vehicle="B",
            issue="C",
        )

        assert ticket["call_id"] == "CA_fallback"
        assert ticket["location"] == "A"


# ---------------------------------------------------------------------------
# update_ticket_hazard
# ---------------------------------------------------------------------------


class TestUpdateTicketHazard:
    """Tests for the hazard escalation updater."""

    @patch("app.services.tickets._get_supabase")
    def test_sets_hazard_detected_true(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        mock_get_sb.return_value = sb

        from app.services.tickets import update_ticket_hazard

        update_ticket_hazard(call_id="CA_haz", hazard_reason="Vehicle fire")

        sb.table.return_value.update.assert_called_once()
        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload["hazard_detected"] is True

    @patch("app.services.tickets._get_supabase")
    def test_sets_hazard_reason(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        mock_get_sb.return_value = sb

        from app.services.tickets import update_ticket_hazard

        update_ticket_hazard(call_id="CA_haz", hazard_reason="Trapped occupant")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload["hazard_reason"] == "Trapped occupant"

    @patch("app.services.tickets._get_supabase")
    def test_sets_status_to_escalated(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        mock_get_sb.return_value = sb

        from app.services.tickets import update_ticket_hazard

        update_ticket_hazard(call_id="CA_haz", hazard_reason="Fire")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload["status"] == "escalated"

    @patch("app.services.tickets._get_supabase")
    def test_filters_by_call_id(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        # Chain: table.update().eq().execute() — each returns a distinct mock
        update_mock = MagicMock()
        table.update.return_value = update_mock
        eq_mock = MagicMock()
        update_mock.eq.return_value = eq_mock
        mock_get_sb.return_value = sb

        from app.services.tickets import update_ticket_hazard

        update_ticket_hazard(call_id="CA_specific", hazard_reason="Accident")

        update_mock.eq.assert_called_once_with("call_id", "CA_specific")

    @patch("app.services.tickets._get_supabase")
    def test_exception_is_swallowed(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        sb.table.return_value.update.side_effect = RuntimeError("DB down")
        mock_get_sb.return_value = sb

        from app.services.tickets import update_ticket_hazard

        # Should not raise
        update_ticket_hazard(call_id="CA_err", hazard_reason="Fire")


# ---------------------------------------------------------------------------
# update_notification_status
# ---------------------------------------------------------------------------


class TestUpdateNotificationStatus:
    """Tests for the notification status updater."""

    @patch("app.services.tickets._get_supabase")
    def test_updates_notification_status_to_sent(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        mock_get_sb.return_value = sb

        from app.services.tickets import update_notification_status

        update_notification_status(call_id="CA_notif", status="sent")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload["notification_status"] == "sent"

    @patch("app.services.tickets._get_supabase")
    def test_updates_notification_status_to_failed(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        mock_get_sb.return_value = sb

        from app.services.tickets import update_notification_status

        update_notification_status(call_id="CA_notif", status="failed")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload["notification_status"] == "failed"

    @patch("app.services.tickets._get_supabase")
    def test_filters_by_call_id(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        # Chain: table.update().eq().execute() — each returns a distinct mock
        update_mock = MagicMock()
        table.update.return_value = update_mock
        eq_mock = MagicMock()
        update_mock.eq.return_value = eq_mock
        mock_get_sb.return_value = sb

        from app.services.tickets import update_notification_status

        update_notification_status(call_id="CA_filter", status="sent")

        update_mock.eq.assert_called_once_with("call_id", "CA_filter")

    @patch("app.services.tickets._get_supabase")
    def test_exception_is_swallowed(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        sb.table.return_value.update.side_effect = RuntimeError("Timeout")
        mock_get_sb.return_value = sb

        from app.services.tickets import update_notification_status

        # Should not raise
        update_notification_status(call_id="CA_err", status="sent")


# ---------------------------------------------------------------------------
# complete_intake
# ---------------------------------------------------------------------------


def _mock_guarded_update(*, rows_updated: int = 1) -> MagicMock:
    """Build a mock Supabase client for update().eq().in_().execute() chains."""
    sb = MagicMock()
    table = MagicMock()
    sb.table.return_value = table
    update_mock = MagicMock()
    table.update.return_value = update_mock
    eq_mock = MagicMock()
    update_mock.eq.return_value = eq_mock
    in_mock = MagicMock()
    eq_mock.in_.return_value = in_mock
    execute_result = MagicMock()
    execute_result.data = [{"call_id": "CA_x"}] * rows_updated
    in_mock.execute.return_value = execute_result
    return sb


class TestCompleteIntake:
    """Tests for the completion status finalizer."""

    @patch("app.services.tickets._get_supabase")
    def test_sets_status_to_completed(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import complete_intake

        complete_intake(call_id="CA_done")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload == {"status": "completed"}

    @patch("app.services.tickets._get_supabase")
    def test_filters_by_call_id(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import complete_intake

        complete_intake(call_id="CA_specific")

        sb.table.return_value.update.return_value.eq.assert_called_once_with(
            "call_id", "CA_specific"
        )

    @patch("app.services.tickets._get_supabase")
    def test_guard_allows_open_and_abandoned_only(self, mock_get_sb: MagicMock) -> None:
        """The guard must flip open rows and self-heal abandoned rows, never escalated."""
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import complete_intake

        complete_intake(call_id="CA_guard")

        in_mock = sb.table.return_value.update.return_value.eq.return_value.in_
        in_mock.assert_called_once_with(
            "status", ["pending", "in_progress", "abandoned"]
        )

    @patch("app.services.tickets._get_supabase")
    def test_logs_rows_updated(self, mock_get_sb: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        sb = _mock_guarded_update(rows_updated=0)
        mock_get_sb.return_value = sb

        from app.services.tickets import complete_intake

        with caplog.at_level("INFO"):
            complete_intake(call_id="CA_norow")

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{")
        ]
        finalized = [e for e in events if e.get("event") == "Intake status finalized"]
        assert finalized, "expected an 'Intake status finalized' log event"
        assert finalized[0]["rows_updated"] == 0
        assert finalized[0]["status"] == "completed"

    @patch("app.services.tickets._get_supabase")
    def test_exception_is_swallowed(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        sb.table.return_value.update.side_effect = RuntimeError("DB down")
        mock_get_sb.return_value = sb

        from app.services.tickets import complete_intake

        # Should not raise
        complete_intake(call_id="CA_err")


# ---------------------------------------------------------------------------
# abandon_if_open
# ---------------------------------------------------------------------------


class TestAbandonIfOpen:
    """Tests for the open-row abandonment finalizer."""

    @patch("app.services.tickets._get_supabase")
    def test_sets_status_to_abandoned(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import abandon_if_open

        abandon_if_open(call_id="CA_gone")

        update_payload = sb.table.return_value.update.call_args[0][0]
        assert update_payload == {"status": "abandoned"}

    @patch("app.services.tickets._get_supabase")
    def test_filters_by_call_id(self, mock_get_sb: MagicMock) -> None:
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import abandon_if_open

        abandon_if_open(call_id="CA_specific")

        sb.table.return_value.update.return_value.eq.assert_called_once_with(
            "call_id", "CA_specific"
        )

    @patch("app.services.tickets._get_supabase")
    def test_guard_excludes_completed_and_escalated(self, mock_get_sb: MagicMock) -> None:
        """Only still-open rows may flip to abandoned."""
        sb = _mock_guarded_update()
        mock_get_sb.return_value = sb

        from app.services.tickets import abandon_if_open

        abandon_if_open(call_id="CA_guard")

        in_mock = sb.table.return_value.update.return_value.eq.return_value.in_
        in_mock.assert_called_once_with("status", ["pending", "in_progress"])
        allowed = in_mock.call_args[0][1]
        assert "completed" not in allowed
        assert "escalated" not in allowed

    @patch("app.services.tickets._get_supabase")
    def test_zero_matched_rows_is_a_no_op(self, mock_get_sb: MagicMock) -> None:
        """A retried callback against an already-finalized row matches nothing and does not raise."""
        sb = _mock_guarded_update(rows_updated=0)
        mock_get_sb.return_value = sb

        from app.services.tickets import abandon_if_open

        abandon_if_open(call_id="CA_retry")

        execute_mock = (
            sb.table.return_value.update.return_value.eq.return_value.in_.return_value.execute
        )
        execute_mock.assert_called_once()

    @patch("app.services.tickets._get_supabase")
    def test_exception_is_swallowed(self, mock_get_sb: MagicMock) -> None:
        sb = MagicMock()
        sb.table.return_value.update.side_effect = RuntimeError("Timeout")
        mock_get_sb.return_value = sb

        from app.services.tickets import abandon_if_open

        # Should not raise
        abandon_if_open(call_id="CA_err")
