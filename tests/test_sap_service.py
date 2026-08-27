"""SAP logic that can be checked without a SAP system.

The BAPI payload, the RETURN-table reading and the fuzzy match are where the
mistakes are; none of them need a connection to test.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from services.errors import ResolutionError, SalesOrderError
from services.sap_service import (
    Record,
    _resolve,
    build_order_payload,
    check_return,
    create_sales_order,
    read_table,
)

CUSTOMERS = [
    Record("0000000003", "Eksen Mekatronik Ltd. Sti."),
    Record("0000000007", "Nova Otomasyon Teknolojileri A.S."),
    Record("0000000011", "Delta Endustriyel Malzeme"),
]


class FakeConnection:
    """Just enough of pyrfc.Connection to exercise read_table's unpacking."""

    def __init__(self, fields, rows):
        self._fields = fields
        self._rows = rows
        self.calls = []

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"FIELDS": self._fields, "DATA": [{"WA": row} for row in self._rows]}


class TestReadTable:

    def test_fields_are_cut_by_offset_not_by_a_delimiter(self):
        # A name containing the delimiter used to shift every later column. Offsets
        # come from SAP itself and cannot collide with the data.
        fields = [{"FIELDNAME": "KUNNR", "OFFSET": "0", "LENGTH": "10"},
                  {"FIELDNAME": "NAME1", "OFFSET": "10", "LENGTH": "35"}]
        rows = ["0000000003" + "A|B Elektronik Ltd.".ljust(35)]
        conn = FakeConnection(fields, rows)

        result = read_table(conn, "KNA1", ["KUNNR", "NAME1"])

        assert result == [{"KUNNR": "0000000003", "NAME1": "A|B Elektronik Ltd."}]

    def test_where_clauses_are_sent_as_options(self):
        conn = FakeConnection([], [])
        read_table(conn, "MAKT", ["MATNR"], where=["SPRAS = 'E'"], row_limit=5)

        name, kwargs = conn.calls[0]
        assert name == "RFC_READ_TABLE"
        assert kwargs["OPTIONS"] == [{"TEXT": "SPRAS = 'E'"}]
        assert kwargs["ROWCOUNT"] == 5


class TestResolve:

    def test_matches_an_approximate_name(self):
        assert _resolve("Eksen Mekatronik", CUSTOMERS,
                        what="customer", pad_to=10) == "0000000003"

    def test_pads_to_the_sap_field_width(self):
        records = [Record("3", "Eksen Mekatronik Ltd. Sti.")]
        assert _resolve("Eksen Mekatronik", records,
                        what="customer", pad_to=10) == "0000000003"

    def test_a_poor_match_raises_rather_than_guessing(self):
        # Booking against the wrong customer is worse than not booking at all.
        with pytest.raises(ResolutionError) as caught:
            _resolve("Completely Unrelated Company", CUSTOMERS,
                     what="customer", pad_to=10, threshold=90)
        assert caught.value.term == "Completely Unrelated Company"
        assert caught.value.score is not None

    def test_empty_master_data_is_reported(self):
        with pytest.raises(ResolutionError, match="no customer master data"):
            _resolve("Eksen", [], what="customer", pad_to=10)


class TestBuildOrderPayload:

    def test_item_numbering_and_flags(self):
        payload = build_order_payload("0000000003", [
            {"material": "000000000000000042", "quantity": 12},
            {"material": "000000000000000043", "quantity": 3},
        ])

        assert [i["ITM_NUMBER"] for i in payload["ORDER_ITEMS_IN"]] == \
               ["000010", "000020"]
        assert payload["ORDER_ITEMS_IN"][0]["TARGET_QTY"] == "12"
        assert all(i["UPDATEFLAG"] == "I" for i in payload["ORDER_ITEMS_INX"])

    def test_schedule_lines_mirror_the_items(self):
        payload = build_order_payload("0000000003",
                                      [{"material": "42", "quantity": 7}])
        schedule = payload["ORDER_SCHEDULES_IN"][0]
        assert schedule["ITM_NUMBER"] == "000010"
        assert schedule["REQ_QTY"] == "7"
        assert schedule["SCHED_LINE"] == "0001"

    def test_sold_to_and_ship_to_both_get_the_customer(self):
        payload = build_order_payload("0000000003",
                                      [{"material": "42", "quantity": 1}])
        roles = {p["PARTN_ROLE"]: p["PARTN_NUMB"] for p in payload["ORDER_PARTNERS"]}
        assert roles == {"AG": "0000000003", "WE": "0000000003"}

    def test_sales_area_comes_from_configuration(self):
        payload = build_order_payload("0000000003",
                                      [{"material": "42", "quantity": 1}])
        assert payload["ORDER_HEADER_IN"]["SALES_ORG"] == Settings.SAP_SALES_ORG
        assert payload["ORDER_HEADER_IN"]["DOC_TYPE"] == Settings.SAP_DOC_TYPE
        # Every header field set must be flagged, or SAP silently ignores it.
        assert set(payload["ORDER_HEADER_INX"]) == \
               set(payload["ORDER_HEADER_IN"]) | {"UPDATEFLAG"}


class TestCheckReturn:

    def test_errors_and_aborts_count(self):
        messages = [{"TYPE": "S", "MESSAGE": "ok"},
                    {"TYPE": "W", "MESSAGE": "watch out"},
                    {"TYPE": "E", "MESSAGE": "material not found"},
                    {"TYPE": "A", "MESSAGE": "aborted"}]
        assert [m["TYPE"] for m in check_return(messages)] == ["E", "A"]

    def test_success_and_warnings_do_not(self):
        assert check_return([{"TYPE": "S"}, {"TYPE": "W"}, {"TYPE": "I"}]) == []


class TestCreateSalesOrder:

    def test_an_empty_order_is_refused_before_any_rfc_call(self):
        with pytest.raises(SalesOrderError, match="no items"):
            create_sales_order({"customer": "0000000003", "items": []})
