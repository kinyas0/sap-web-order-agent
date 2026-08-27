"""Streamlit front end: enter an order, review what it resolved to, then commit.

The one Streamlit-specific thing worth knowing is at the top. A Streamlit script
re-runs from the first line on every interaction, so anything expensive built at
module scope is rebuilt on every click. The previous version constructed an OpenAI
client that way, and the database layer it shipped with opened a PostgreSQL
connection in a constructor — one leaked connection per rerun, which on a form with
a dozen widgets is a dozen connections a minute. Both now live behind
``st.cache_resource``, which builds them once per process.

The other change is that nothing is sent to SAP until it has been shown. Free text
goes through a language model and a fuzzy match, and both can be wrong; a screen
that books the order the moment you press a button gives you no chance to notice.
"""

from __future__ import annotations

import logging

import streamlit as st

from config.settings import Settings
from services.db_service import AuditLog, NullAuditLog, open_audit_log
from services.errors import (
    ExtractionError,
    OrderAgentError,
    ResolutionError,
    SalesOrderError,
)
from services.llm_service import extract_order
from services.sap_service import (
    create_sales_order,
    find_customer_code,
    find_material_code,
)

logger = logging.getLogger("sap_web_order_agent")
logging.basicConfig(level=getattr(logging, Settings.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

SOURCE = "web"


@st.cache_resource(show_spinner=False)
def audit_log() -> AuditLog:
    """One audit log per process, not one per rerun.

    ``open_audit_log`` is a context manager because the poller wants a scoped pool;
    here the pool should outlive any single interaction, so it is entered once and
    deliberately not closed. Streamlit tears the process down when it reloads.
    """
    try:
        manager = open_audit_log()
        return manager.__enter__()
    except Exception:  # the UI must work without a database
        logger.exception("could not open the audit log")
        return NullAuditLog()


# --------------------------------------------------------------------- resolving


def resolve_free_text(order_text: str, audit: AuditLog, transaction_id: int) -> dict:
    """Read an order out of free text and resolve its names against SAP."""
    order = extract_order(order_text)
    audit.log_agent_run(transaction_id, "order_extraction", "SUCCESS",
                        input_data={"text": order_text[:4000]},
                        output_data=order.raw, duration_ms=order.duration_ms)
    audit.set_state(transaction_id, "EXTRACTED")

    return {
        "customer": find_customer_code(order.customer_name),
        "customer_name": order.customer_name,
        "items": [{"material": find_material_code(item["material_description"]),
                   "description": item["material_description"],
                   "quantity": item["quantity"]}
                  for item in order.items],
    }


def resolve_structured(customer: str, customer_is_code: bool,
                       rows: list[dict], materials_are_codes: bool) -> dict:
    """Resolve the form's rows, taking codes as given and matching the rest."""
    resolved_items = []
    for row in rows:
        text = (row["material"] or "").strip()
        if not text:
            continue
        resolved_items.append({
            "material": text.zfill(18) if materials_are_codes
                        else find_material_code(text),
            "description": text,
            "quantity": int(row["quantity"]),
        })

    if not resolved_items:
        raise ResolutionError("no materials were entered", term="")

    return {
        "customer": customer.strip().zfill(10) if customer_is_code
                    else find_customer_code(customer),
        "customer_name": customer,
        "items": resolved_items,
    }


def submit(resolved: dict, audit: AuditLog, transaction_id: int) -> str:
    """Send the resolved order to SAP and record the outcome."""
    payload = {"customer": resolved["customer"],
               "items": [{"material": item["material"], "quantity": item["quantity"]}
                         for item in resolved["items"]]}
    audit.set_state(transaction_id, "SUBMITTED")
    try:
        result = create_sales_order(payload)
    except SalesOrderError as error:
        audit.log_erp_action(transaction_id, "BAPI_SALESORDER_CREATEFROMDAT2",
                             "FAILED", request_data=payload,
                             response_data={"messages": error.messages})
        raise

    audit.log_erp_action(transaction_id, "BAPI_SALESORDER_CREATEFROMDAT2", "SUCCESS",
                         document_id=result.document, request_data=payload,
                         response_data={"messages": result.messages},
                         duration_ms=result.duration_ms)
    audit.set_state(transaction_id, "COMPLETED")
    return result.document


# --------------------------------------------------------------------------- ui

st.set_page_config(page_title="SAP Sales Order Agent", page_icon="📦")
st.title("SAP Sales Order Agent")

st.session_state.setdefault("resolved", None)
st.session_state.setdefault("transaction_id", None)

audit = audit_log()
if isinstance(audit, NullAuditLog):
    st.caption("Audit trail disabled — no database configured.")

free_text_tab, form_tab = st.tabs(["Free text", "Line by line"])

with free_text_tab:
    order_text = st.text_area(
        "Order text", height=180,
        placeholder="Paste the customer's message. Turkish or English.")

    if st.button("Read order", key="read"):
        if not order_text.strip():
            st.error("Enter the order text first.")
        else:
            transaction_id = audit.start_transaction("SALES_ORDER", SOURCE)
            try:
                with st.spinner("Reading and resolving…"):
                    st.session_state.resolved = resolve_free_text(
                        order_text, audit, transaction_id)
                st.session_state.transaction_id = transaction_id
                audit.set_state(transaction_id, "RESOLVED")
            except (ExtractionError, ResolutionError) as error:
                audit.log_error("web_ui", error, transaction_id)
                audit.set_state(transaction_id, "FAILED")
                st.error(str(error))
            except OrderAgentError as error:
                audit.log_error("web_ui", error, transaction_id)
                audit.set_state(transaction_id, "FAILED")
                st.error(f"SAP is not reachable: {error}" if error.retryable
                         else str(error))

with form_tab:
    customer_is_code = st.checkbox("Customer entered as an SAP code", key="cust_code")
    customer = st.text_input("Customer", key="cust",
                             placeholder="Company name, or 0000000003")

    materials_are_codes = st.checkbox("Materials entered as SAP codes", key="mat_code")
    rows = st.data_editor(
        [{"material": "", "quantity": 1}],
        column_config={
            "material": st.column_config.TextColumn("Material", width="large"),
            "quantity": st.column_config.NumberColumn("Quantity", min_value=1, step=1),
        },
        num_rows="dynamic", key="rows", use_container_width=True)

    if st.button("Resolve order", key="resolve"):
        if not customer.strip():
            st.error("Enter a customer first.")
        else:
            transaction_id = audit.start_transaction("SALES_ORDER", SOURCE)
            try:
                with st.spinner("Resolving against SAP…"):
                    st.session_state.resolved = resolve_structured(
                        customer, customer_is_code, list(rows), materials_are_codes)
                st.session_state.transaction_id = transaction_id
                audit.set_state(transaction_id, "RESOLVED")
            except OrderAgentError as error:
                audit.log_error("web_ui", error, transaction_id)
                audit.set_state(transaction_id, "FAILED")
                st.error(str(error))

# ---------------------------------------------------------------- review/commit

resolved = st.session_state.resolved
if resolved:
    st.divider()
    st.subheader("Review")
    st.write(f"**Customer** `{resolved['customer']}` — {resolved['customer_name']}")
    st.table([{"Material": item["material"],
               "Entered as": item["description"],
               "Quantity": item["quantity"]} for item in resolved["items"]])

    st.caption("Names were matched approximately. Check the codes before committing.")

    confirm, cancel = st.columns(2)

    with confirm:
        if st.button("Create sales order", type="primary", use_container_width=True):
            try:
                with st.spinner("Creating…"):
                    document = submit(resolved, audit,
                                      st.session_state.transaction_id)
                st.success(f"Sales order created: **{document}**")
                st.session_state.resolved = None
                st.session_state.transaction_id = None
            except OrderAgentError as error:
                audit.log_error("web_ui", error, st.session_state.transaction_id)
                audit.set_state(st.session_state.transaction_id, "FAILED")
                st.error(str(error))

    with cancel:
        if st.button("Discard", use_container_width=True):
            if st.session_state.transaction_id:
                audit.set_state(st.session_state.transaction_id, "FAILED")
            st.session_state.resolved = None
            st.session_state.transaction_id = None
            st.rerun()
