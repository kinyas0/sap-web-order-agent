# SAP Web Order Agent

Enter a sales order as free text or line by line, see what it resolved to in SAP,
and commit it — with a record of every step.

```
free text ──▶ extract ──▶ resolve ──▶ review ──▶ BAPI_SALESORDER_CREATEFROMDAT2
   or         (LLM)      (KNA1/MAKT)    │                    │
line items ───────────────┘             │                    │
                                        └────────────────────┴──▶ PostgreSQL audit trail
```

Built during an internship against a live SAP system, as the interactive
counterpart to [sap-mail-order-agent](https://github.com/kinyas0/sap-mail-order-agent),
which does the same job unattended from a mailbox. The two share their service
layer; the difference is where the order comes from and who confirms it.

## What it does

**Free text** — paste the customer's message. A language model extracts the customer
and the line items, and the result is validated before anything else happens: a
quantity of "about a dozen" fails here, naming the item, rather than three calls
later inside a BAPI.

**Line by line** — type the rows. Customer and materials can each be entered as free
text, to be matched, or as SAP codes, to be taken as given.

Either way the resolved codes are shown before anything is sent. Fuzzy matching can
be wrong, and a screen that books the order on the first click gives nobody a chance
to notice. Only after confirmation does `BAPI_SALESORDER_CREATEFROMDAT2` run, and
the commit happens only if the RETURN table carries no error.

## The audit trail

Four tables, applied from `db/schema.sql`:

| table | one row per |
|---|---|
| `transactions` | order being entered, with the state it reached |
| `agent_runs` | model call — input, parsed output, duration |
| `erp_actions` | BAPI call — request payload, RETURN table, document number |
| `system_errors` | failure, with its type and traceback |

A transaction moves `STARTED → EXTRACTED → RESOLVED → SUBMITTED → COMPLETED`, or
stops at `FAILED` with a row saying why — including orders a user discarded at the
review step, which is worth knowing when the matching threshold is being tuned.

It is optional. Leave `DB_HOST` empty and the app runs exactly as it does with a
database, minus the logging, and says so in the header.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill it in
```

For the audit trail:

```bash
psql "postgresql://user@host/dbname" -f db/schema.sql
```

`pyrfc` needs the SAP NetWeaver RFC SDK, which SAP licenses separately — it is the
one dependency `pip` cannot fetch for you.

## Running

```bash
streamlit run app.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # 42 tests
ruff check .
```

Covered without a live system: response parsing and validation, the RFC_READ_TABLE
unpacking, the BAPI payload, the RETURN-table reading, the fuzzy match, and the
audit writes. `sap_service` imports `pyrfc` defensively so the suite runs on a CI
runner without the SDK. CI also applies `db/schema.sql` to a real PostgreSQL twice,
because DDL documented as safe to re-run should be tested that way.

## Layout

```
config/settings.py       environment, coerced and validated on load
services/
  llm_service.py         extraction, response parsing, validation
  sap_service.py         RFC access, master data, sales orders
  db_service.py          the audit trail
  errors.py              typed failures
db/schema.sql            the four audit tables
app.py                   the Streamlit interface
tests/
```

## Notes

**Streamlit re-runs the script on every interaction.** Anything built at module
scope is rebuilt on every click, so the OpenAI client and the PostgreSQL pool live
behind `st.cache_resource` and are created once per process. A connection opened in
a constructor leaks one per rerun, which on a form with a dozen widgets is a dozen a
minute.

**`RFC_READ_TABLE` is a poor API** — fixed-width text, truncated past 512 bytes —
but it is what a read-only integration user is normally permitted to call. Fields
are cut using the offsets SAP reports rather than by splitting on a delimiter, which
breaks as soon as a customer name contains that character. Master data is read once
per process and cached, not re-read for every line of every order.

Sales area (`DOC_TYPE`, `SALES_ORG`, `DISTR_CHAN`, `DIVISION`) and the material
description language are configuration, not constants.

## Credentials

Nothing sensitive is in this repository. SAP hosts, database credentials and API
keys come from `.env`, which is gitignored.
