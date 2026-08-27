"""Turning free text into an order the rest of the service can act on.

The extraction prompt lived inside the polling loop, and its answer went straight
into ``json.loads``. Models wrap JSON in a `````json`` fence often enough that this
is not an edge case, and when it happened the order was lost with a ``JSONDecodeError``
that named a column number rather than a cause.

What comes back is also not trusted: a quantity of ``"about 12"`` or a missing
customer name has to fail here, with a message that says which field was wrong,
rather than three calls later inside a BAPI.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, NamedTuple

from openai import OpenAI

from config.settings import Settings
from services.errors import ExtractionError

logger = logging.getLogger(__name__)

#: ```json { ... } ``` - fenced output, with or without the language tag.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

PROMPT = """You are an SAP sales order extraction system.

Extract from the text below:
- the customer company name
- each material description
- each quantity

Rules:
- Return ONLY valid JSON, with no explanation and no markdown.
- Preserve product and company names exactly as written.
- Quantity must be a positive integer.
- Ignore greetings, signatures and disclaimers.
- The customer is always a company, never a person.
- Text may be in Turkish or English; keep Turkish characters as they are.

JSON format:

{{
  "customer_name": "Example Industries Ltd.",
  "items": [
    {{"material_description": "220 Ohm Resistor 1/4W", "quantity": 12}}
  ]
}}

Text:

{text}
"""


class Order(NamedTuple):
    """A validated order, before any of its names have been resolved against SAP."""

    customer_name: str
    items: list[dict[str, Any]]
    raw: dict[str, Any]
    duration_ms: int


def _client() -> OpenAI:
    Settings.require_llm()
    return OpenAI(api_key=Settings.OPENROUTER_API_KEY,
                  base_url=Settings.OPENROUTER_BASE_URL,
                  timeout=Settings.LLM_TIMEOUT_SECONDS)


def parse_response(content: str) -> dict[str, Any]:
    """Parse the model's answer, tolerating a code fence around it."""
    if not content or not content.strip():
        raise ExtractionError("the model returned an empty response")

    fenced = _FENCE.match(content)
    payload = fenced.group(1) if fenced else content.strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExtractionError(
            f"the model did not return JSON ({error.msg} at position {error.pos})"
        ) from error

    if not isinstance(parsed, dict):
        raise ExtractionError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def validate(parsed: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Check the shape, and say which field is wrong when it is not right."""
    customer = (parsed.get("customer_name") or "").strip()
    if not customer:
        raise ExtractionError("no customer_name in the extracted order")

    raw_items = parsed.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ExtractionError("no items in the extracted order")

    items: list[dict[str, Any]] = []
    for position, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ExtractionError(f"item {position} is not an object")

        description = (item.get("material_description") or "").strip()
        if not description:
            raise ExtractionError(f"item {position} has no material_description")

        try:
            quantity = int(item["quantity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ExtractionError(
                f"item {position} ({description!r}) has a non-numeric quantity: "
                f"{item.get('quantity')!r}"
            ) from error

        if quantity <= 0:
            raise ExtractionError(
                f"item {position} ({description!r}) has quantity {quantity}")

        items.append({"material_description": description, "quantity": quantity})

    return customer, items


def extract_order(text: str) -> Order:
    """Ask the model for an order and return it validated.

    One retry, because the common failure is a model that ignored "no markdown"
    once and gets it right the second time. A validation failure is not retried:
    the text genuinely does not contain an order.
    """
    if not text or not text.strip():
        raise ExtractionError("nothing to extract from: the text is empty")

    client = _client()
    started = time.monotonic()
    last_error: ExtractionError | None = None

    for attempt in range(1, Settings.LLM_MAX_ATTEMPTS + 1):
        response = client.chat.completions.create(
            model=Settings.OPENROUTER_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        )
        content = response.choices[0].message.content or ""

        try:
            parsed = parse_response(content)
        except ExtractionError as error:
            last_error = error
            logger.warning("extraction attempt %d/%d could not be parsed: %s",
                           attempt, Settings.LLM_MAX_ATTEMPTS, error)
            continue

        customer, items = validate(parsed)
        return Order(customer_name=customer, items=items, raw=parsed,
                     duration_ms=int((time.monotonic() - started) * 1000))

    raise last_error or ExtractionError("extraction failed")
