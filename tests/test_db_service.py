"""The audit layer's contract, checked without a PostgreSQL server.

Two things matter here and neither needs a database: that jsonb parameters are
wrapped rather than handed over raw, and that a missing database degrades to a
no-op instead of taking the order path down with it.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Json

from services.db_service import STATES, AuditLog, NullAuditLog, _json


class RecordingLog(AuditLog):
    """Captures the SQL and parameters instead of executing them."""

    def __init__(self):
        super().__init__(pool=None)
        self.statements: list[tuple[str, tuple]] = []

    def _execute(self, query, params):
        self.statements.append((query, params))

    def _returning_id(self, query, params):
        self.statements.append((query, params))
        return 1


class TestJsonAdaptation:

    def test_dicts_are_wrapped_for_jsonb(self):
        # psycopg3 refuses a bare dict for a jsonb column, which is what the
        # previous version passed. Every audit write carried one.
        assert isinstance(_json({"customer": "Example Ltd."}), Json)

    def test_none_stays_none(self):
        assert _json(None) is None

    def test_unserialisable_values_do_not_break_the_write(self):
        wrapped = _json({"connection": object()})
        assert isinstance(wrapped, Json)
        assert "unserialisable" in wrapped.obj


class TestAuditWrites:

    def test_agent_run_wraps_both_payloads(self):
        log = RecordingLog()
        log.log_agent_run(1, "order_extraction", "SUCCESS",
                          input_data={"body": "..."}, output_data={"items": []})
        _, params = log.statements[0]
        assert isinstance(params[3], Json) and isinstance(params[4], Json)

    def test_erp_action_records_the_document(self):
        log = RecordingLog()
        log.log_erp_action(1, "BAPI_SALESORDER_CREATEFROMDAT2", "SUCCESS",
                           document_id="0000004711", request_data={"a": 1})
        _, params = log.statements[0]
        assert "0000004711" in params

    def test_error_is_stored_with_its_type_and_traceback(self):
        log = RecordingLog()
        try:
            raise ValueError("material not found")
        except ValueError as error:
            log.log_error("order_pipeline", error, transaction_id=7)

        _, params = log.statements[0]
        assert params[2] == "ValueError"
        assert params[3] == "material not found"
        assert "ValueError: material not found" in params[4]

    def test_unknown_state_is_rejected(self):
        # The CHECK constraint would catch this, but only after a round trip, and
        # only if a database is attached at all.
        with pytest.raises(ValueError, match="unknown transaction state"):
            RecordingLog().set_state(1, "ALMOST_DONE")

    @pytest.mark.parametrize("state", STATES)
    def test_every_declared_state_is_accepted(self, state):
        RecordingLog().set_state(1, state)


class TestNullAuditLog:

    def test_writes_are_dropped_silently(self):
        log = NullAuditLog()
        log.log_agent_run(0, "order_extraction", "SUCCESS")
        log.log_erp_action(0, "BAPI", "SUCCESS")
        log.set_state(0, "COMPLETED")
        log.log_error("order_pipeline", RuntimeError("boom"))

    def test_start_transaction_returns_a_usable_id(self):
        # Callers pass this straight into the next call; it must not be None.
        assert NullAuditLog().start_transaction("SALES_ORDER", "gmail") == 0

    def test_nothing_looks_already_handled(self):
        assert NullAuditLog().already_handled("gmail", "abc123") is False
