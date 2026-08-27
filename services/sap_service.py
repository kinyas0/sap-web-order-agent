"""SAP RFC access: master-data lookup and sales order creation.

Two problems in the original are worth naming, because they are the difference
between something that works against a demo client and something that works.

**Master data was re-read per lookup.** ``find_customer_code`` pulled the whole of
KNA1 and ``find_material_code`` the whole of MAKT, on every call. A five-line order
meant six full table reads, and MAKT came back in every language the system had
installed, so the fuzzy match compared a Turkish description against its own English
translation. Both reads are now capped, MAKT is filtered by language, and the result
is cached for the process.

**Connections were opened and dropped.** ``get_connection()`` returned a new
``pyrfc.Connection`` on every call and nothing ever closed one. The same five-line
order opened seven and leaked all seven.

RFC_READ_TABLE is used because it is what a read-only integration user is normally
allowed to call. It is not a good API - it truncates rows past 512 bytes and returns
everything as fixed-width text - so the fields are unpacked by the offsets it
reports rather than by splitting on a delimiter, which is what breaks the moment a
customer name contains the delimiter character.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

try:
    import pyrfc
except ImportError:  # pragma: no cover - depends on the SAP NetWeaver RFC SDK
    # pyrfc needs the SAP NetWeaver RFC SDK, which cannot be installed on a build
    # agent. Importing this module must still work so the payload building, the
    # RETURN-table reading and the fuzzy match can be tested; only opening an
    # actual connection needs the real library.
    pyrfc = None

from rapidfuzz import fuzz, process

from config.settings import Settings
from services.errors import (
    ResolutionError,
    SalesOrderError,
    SapConnectionError,
)

logger = logging.getLogger(__name__)


class Record(NamedTuple):
    """A master-data row: the SAP key and the text a human would search for."""

    key: str
    text: str


# --------------------------------------------------------------------------- rfc


@contextmanager
def sap_connection() -> Iterator[pyrfc.Connection]:
    """Open an RFC connection and close it, whatever happens inside."""
    if pyrfc is None:
        raise SapConnectionError(
            "pyrfc is not installed: it requires the SAP NetWeaver RFC SDK")
    Settings.require_sap()

    try:
        conn = pyrfc.Connection(**Settings.SAP_CONFIG)
    except pyrfc.RFCError as error:
        raise SapConnectionError(f"could not connect to SAP: {error}") from error

    try:
        yield conn
    except pyrfc.RFCError as error:
        raise SapConnectionError(f"RFC call failed: {error}") from error
    finally:
        try:
            conn.close()
        except pyrfc.RFCError:
            logger.debug("RFC connection already closed")


def read_table(conn: pyrfc.Connection, table: str, fields: list[str],
               where: list[str] | None = None,
               row_limit: int | None = None) -> list[dict[str, str]]:
    """Read a table through RFC_READ_TABLE, unpacked by the offsets SAP reports.

    ``where`` is passed as OPTIONS, which SAP applies server-side - the difference
    between transferring a table and transferring the rows you asked for.
    """
    limit = row_limit if row_limit is not None else Settings.SAP_READ_ROW_LIMIT
    result = conn.call(
        "RFC_READ_TABLE",
        QUERY_TABLE=table,
        DELIMITER="",                     # unused: rows are cut by offset instead
        FIELDS=[{"FIELDNAME": name} for name in fields],
        OPTIONS=[{"TEXT": clause} for clause in (where or [])],
        ROWCOUNT=limit,
    )

    # FIELDS comes back annotated with where each column starts and how wide it is.
    layout = [(entry["FIELDNAME"], int(entry["OFFSET"]), int(entry["LENGTH"]))
              for entry in result["FIELDS"]]

    rows = []
    for row in result["DATA"]:
        raw = row["WA"]
        rows.append({name: raw[offset:offset + length].strip()
                     for name, offset, length in layout})

    if len(rows) >= limit:
        logger.warning("%s hit the %d row limit; raise SAP_READ_ROW_LIMIT or "
                       "narrow the query", table, limit)
    return rows


# ------------------------------------------------------------------- master data


class _Cache:
    """Master data held for a while, because it changes far slower than orders."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, list[Record]]] = {}

    def get(self, key: str) -> list[Record] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        loaded_at, records = entry
        if time.monotonic() - loaded_at > Settings.SAP_CACHE_TTL_SECONDS:
            del self._entries[key]
            return None
        return records

    def put(self, key: str, records: list[Record]) -> None:
        self._entries[key] = (time.monotonic(), records)

    def clear(self) -> None:
        self._entries.clear()


_cache = _Cache()


def clear_cache() -> None:
    """Drop cached master data. Called by tests, and useful after a data load."""
    _cache.clear()


def load_customers(conn: pyrfc.Connection | None = None) -> list[Record]:
    """Customer number and name from KNA1."""
    cached = _cache.get("customers")
    if cached is not None:
        return cached

    def fetch(connection: pyrfc.Connection) -> list[Record]:
        rows = read_table(connection, "KNA1", ["KUNNR", "NAME1"])
        return [Record(row["KUNNR"], row["NAME1"]) for row in rows if row["NAME1"]]

    records = fetch(conn) if conn is not None else _with_connection(fetch)
    logger.info("loaded %d customers from KNA1", len(records))
    _cache.put("customers", records)
    return records


def load_materials(conn: pyrfc.Connection | None = None) -> list[Record]:
    """Material number and description from MAKT, in one language."""
    cached = _cache.get("materials")
    if cached is not None:
        return cached

    def fetch(connection: pyrfc.Connection) -> list[Record]:
        rows = read_table(
            connection, "MAKT", ["MATNR", "MAKTX"],
            where=[f"SPRAS = '{Settings.SAP_MATERIAL_LANGUAGE}'"],
        )
        return [Record(row["MATNR"], row["MAKTX"]) for row in rows if row["MAKTX"]]

    records = fetch(conn) if conn is not None else _with_connection(fetch)
    logger.info("loaded %d materials from MAKT (language %s)",
                len(records), Settings.SAP_MATERIAL_LANGUAGE)
    _cache.put("materials", records)
    return records


def _with_connection(work):
    with sap_connection() as conn:
        return work(conn)


# --------------------------------------------------------------------- matching


def _resolve(term: str, records: list[Record], *, what: str, pad_to: int,
             threshold: int | None = None) -> str:
    """Fuzzy-match a free-text name onto a master-data key.

    Raises rather than returning a poor match: booking an order against the wrong
    customer is worse in every way than not booking it.
    """
    if not records:
        raise ResolutionError(f"no {what} master data available", term=term)

    by_text = {record.text: record.key for record in records}
    match = process.extractOne(term, list(by_text), scorer=fuzz.token_sort_ratio)
    if match is None:
        raise ResolutionError(f"no {what} matched {term!r}", term=term)

    matched_text, score = match[0], match[1]
    limit = Settings.MATCH_SCORE_THRESHOLD if threshold is None else threshold
    if score < limit:
        raise ResolutionError(
            f"best {what} match for {term!r} was {matched_text!r} at {score:.0f}, "
            f"below the threshold of {limit}",
            term=term, best_match=matched_text, score=score,
        )

    logger.debug("%s %r resolved to %r (%.0f)", what, term, matched_text, score)
    return by_text[matched_text].zfill(pad_to)


def find_customer_code(customer_name: str, threshold: int | None = None) -> str:
    """SAP customer number for a company name, zero-padded to KUNNR's width."""
    return _resolve(customer_name, load_customers(),
                    what="customer", pad_to=10, threshold=threshold)


def find_material_code(material_description: str,
                       threshold: int | None = None) -> str:
    """SAP material number for a description, zero-padded to MATNR's width."""
    return _resolve(material_description, load_materials(),
                    what="material", pad_to=18, threshold=threshold)


# ----------------------------------------------------------------- sales orders


def build_order_payload(customer: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the BAPI tables. Split out so it can be tested without a SAP system."""
    header = {
        "DOC_TYPE": Settings.SAP_DOC_TYPE,
        "SALES_ORG": Settings.SAP_SALES_ORG,
        "DISTR_CHAN": Settings.SAP_DISTR_CHAN,
        "DIVISION": Settings.SAP_DIVISION,
    }
    items_in, items_inx, schedules_in, schedules_inx = [], [], [], []

    for index, item in enumerate(items, start=1):
        number = str(index * 10).zfill(6)
        quantity = str(item["quantity"])

        items_in.append({"ITM_NUMBER": number, "MATERIAL": item["material"],
                         "TARGET_QTY": quantity})
        items_inx.append({"ITM_NUMBER": number, "UPDATEFLAG": "I",
                          "MATERIAL": "X", "TARGET_QTY": "X"})
        schedules_in.append({"ITM_NUMBER": number, "SCHED_LINE": "0001",
                             "REQ_QTY": quantity})
        schedules_inx.append({"ITM_NUMBER": number, "SCHED_LINE": "0001",
                              "UPDATEFLAG": "I", "REQ_QTY": "X"})

    return {
        "ORDER_HEADER_IN": header,
        # Every header field that is set must also be flagged, or SAP ignores it.
        "ORDER_HEADER_INX": {"UPDATEFLAG": "I", **dict.fromkeys(header, "X")},
        "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": customer},
                           {"PARTN_ROLE": "WE", "PARTN_NUMB": customer}],
        "ORDER_ITEMS_IN": items_in,
        "ORDER_ITEMS_INX": items_inx,
        "ORDER_SCHEDULES_IN": schedules_in,
        "ORDER_SCHEDULES_INX": schedules_inx,
    }


def check_return(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the entries that mean the call failed. ``E`` is an error, ``A`` abort."""
    return [message for message in messages if message.get("TYPE") in ("E", "A")]


class SalesOrderResult(NamedTuple):
    document: str
    messages: list[dict[str, str]]
    duration_ms: int


def create_sales_order(payload: dict[str, Any]) -> SalesOrderResult:
    """Create a sales order and commit it, or roll back and raise.

    The BAPI does not raise on a rejected order - it returns a RETURN table and
    leaves the update task pending. Committing without reading that table books
    whatever SAP was willing to accept, which may be nothing.
    """
    if not payload.get("items"):
        raise SalesOrderError("refusing to create an order with no items")

    bapi = build_order_payload(payload["customer"], payload["items"])
    started = time.monotonic()

    with sap_connection() as conn:
        result = conn.call("BAPI_SALESORDER_CREATEFROMDAT2", **bapi)
        messages = list(result.get("RETURN", []))
        document = (result.get("SALESDOCUMENT") or "").strip()
        errors = check_return(messages)

        for message in messages:
            logger.log(logging.ERROR if message.get("TYPE") in ("E", "A")
                       else logging.INFO,
                       "SAP %s %s", message.get("TYPE"), message.get("MESSAGE"))

        if errors or not document:
            conn.call("BAPI_TRANSACTION_ROLLBACK")
            summary = "; ".join(m.get("MESSAGE", "") for m in errors) or \
                      "SAP returned no document number"
            raise SalesOrderError(f"sales order rejected: {summary}",
                                  messages=messages)

        conn.call("BAPI_TRANSACTION_COMMIT", WAIT="X")

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info("created sales order %s in %d ms", document, duration_ms)
    return SalesOrderResult(document=document, messages=messages,
                            duration_ms=duration_ms)
