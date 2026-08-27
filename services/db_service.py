"""The audit trail: what was read, what it resolved to, what SAP said.

Three things about this layer are deliberate.

**Every statement runs inside ``pool.connection()``.** That context manager commits
on a clean exit and rolls back on an exception. The previous version committed by
hand and never rolled back, so the first failed statement left the connection in a
failed transaction and *every* later query on it raised ``InFailedSqlTransaction``
until the process was restarted - one malformed order took logging down for the day.

**The pool owns the connections.** One connection opened in ``__init__`` and never
closed leaks on every rerun and dies for good when the server drops it. A pool
reconnects, caps the count, and bounds how long a caller waits for one.

**Logging never breaks the order.** An order that reached SAP has happened whether
or not a row about it was written, so a database that is down or absent degrades to
a no-op with a warning rather than raising into the caller.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from config.settings import Settings

logger = logging.getLogger(__name__)

#: States a transaction may hold; mirrors the CHECK constraint in db/schema.sql.
STATES = ("STARTED", "EXTRACTED", "RESOLVED", "SUBMITTED", "COMPLETED", "FAILED")


def _json(value: Any) -> Json | None:
    """Wrap a value for a ``jsonb`` column.

    psycopg3 will not adapt a bare dict to jsonb; it raises rather than guessing.
    Anything not JSON-serialisable is stored as its repr instead of taking the whole
    write down, because a log line is not worth losing an order over.
    """
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        value = {"unserialisable": repr(value)}
    return Json(value)


class AuditLog:
    """Writes the audit trail. Obtain one from :func:`open_audit_log`."""

    def __init__(self, pool: ConnectionPool | None):
        self._pool = pool

    # -- plumbing ----------------------------------------------------------

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        with (self._pool.connection() as conn,
              conn.cursor(row_factory=dict_row) as cur):
            yield cur

    def _execute(self, query: str, params: tuple) -> None:
        with self._cursor() as cur:
            cur.execute(query, params)

    def _returning_id(self, query: str, params: tuple) -> int:
        with self._cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()["id"]

    # -- transactions ------------------------------------------------------

    def start_transaction(self, transaction_type: str, source: str,
                          external_ref: str | None = None) -> int:
        """Open a transaction and return its id.

        ``external_ref`` identifies whatever triggered this - a Gmail message id,
        say. It is unique per source, so a message polled twice cannot become two
        sales orders.
        """
        return self._returning_id(
            """
            INSERT INTO transactions (transaction_type, current_state, source,
                                      external_ref)
            VALUES (%s, 'STARTED', %s, %s)
            RETURNING id
            """,
            (transaction_type, source, external_ref),
        )

    def set_state(self, transaction_id: int, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown transaction state: {state!r}")
        self._execute(
            """
            UPDATE transactions
               SET current_state = %s, updated_at = now()
             WHERE id = %s
            """,
            (state, transaction_id),
        )

    def already_handled(self, source: str, external_ref: str) -> bool:
        """True when this source/reference already produced a completed order."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM transactions
                 WHERE source = %s AND external_ref = %s
                   AND current_state = 'COMPLETED'
                 LIMIT 1
                """,
                (source, external_ref),
            )
            return cur.fetchone() is not None

    # -- the things that happened -----------------------------------------

    def log_agent_run(self, transaction_id: int, agent_name: str, status: str,
                      input_data: Any = None, output_data: Any = None,
                      duration_ms: int | None = None) -> None:
        self._execute(
            """
            INSERT INTO agent_runs (transaction_id, agent_name, status,
                                    input_data, output_data, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (transaction_id, agent_name, status, _json(input_data),
             _json(output_data), duration_ms),
        )

    def log_erp_action(self, transaction_id: int, action_type: str, status: str,
                       document_id: str | None = None, request_data: Any = None,
                       response_data: Any = None,
                       duration_ms: int | None = None) -> None:
        self._execute(
            """
            INSERT INTO erp_actions (transaction_id, erp_system, action_type, status,
                                     document_id, request_data, response_data,
                                     duration_ms)
            VALUES (%s, 'SAP', %s, %s, %s, %s, %s, %s)
            """,
            (transaction_id, action_type, status, document_id,
             _json(request_data), _json(response_data), duration_ms),
        )

    def log_error(self, service_name: str, error: BaseException,
                  transaction_id: int | None = None) -> None:
        """Record a failure with its traceback, which is the part you actually need."""
        self._execute(
            """
            INSERT INTO system_errors (transaction_id, service_name, error_type,
                                       error_message, traceback)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (transaction_id, service_name, type(error).__name__, str(error),
             "".join(traceback.format_exception(type(error), error,
                                                error.__traceback__))),
        )


class NullAuditLog(AuditLog):
    """Stand-in used when no database is configured, or when it is unreachable.

    Same interface, no writes. Callers never have to ask whether logging is
    available, so the unlogged path is the same code as the logged one - and
    therefore the tested one.
    """

    def __init__(self) -> None:
        super().__init__(pool=None)

    def _execute(self, query: str, params: tuple) -> None:
        logger.debug("audit log disabled, dropping write")

    def _returning_id(self, query: str, params: tuple) -> int:
        return 0

    def already_handled(self, source: str, external_ref: str) -> bool:
        return False


@contextmanager
def open_audit_log() -> Iterator[AuditLog]:
    """Yield an :class:`AuditLog`, or a :class:`NullAuditLog` if there is no database.

    The pool is opened once for the life of the ``with`` block and closed on the way
    out, so a long-running poller keeps its connections and a short script does not
    leak them.
    """
    if not Settings.database_configured():
        logger.info("no database configured, audit trail disabled")
        yield NullAuditLog()
        return

    pool = ConnectionPool(
        conninfo=Settings.database_url(),
        min_size=1,
        max_size=Settings.DB_POOL_MAX_SIZE,
        timeout=Settings.DB_CONNECT_TIMEOUT_SECONDS,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=Settings.DB_CONNECT_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - any failure here is non-fatal
        # An unreachable database must not stop orders being created.
        logger.warning("database unreachable (%s), audit trail disabled", error)
        pool.close()
        yield NullAuditLog()
        return

    try:
        yield AuditLog(pool)
    finally:
        pool.close()
