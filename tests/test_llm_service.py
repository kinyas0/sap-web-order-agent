"""The parsing and validation the extraction step depends on.

These are the failures that actually happened: a model that fenced its JSON, a
quantity written as a word, an email that was a question rather than an order.
"""

from __future__ import annotations

import pytest

from services.errors import ExtractionError
from services.llm_service import parse_response, validate

ORDER = '{"customer_name": "Example Ltd.", "items": ' \
        '[{"material_description": "220 Ohm Resistor", "quantity": 12}]}'


class TestParseResponse:

    def test_plain_json(self):
        assert parse_response(ORDER)["customer_name"] == "Example Ltd."

    @pytest.mark.parametrize("wrapper", ["```json\n{body}\n```", "```\n{body}\n```"])
    def test_code_fence_is_stripped(self, wrapper):
        # The single most common way this broke: "no markdown" is a request, not a
        # guarantee, and the bare json.loads it used to hit raised on the backticks.
        assert parse_response(wrapper.format(body=ORDER))["items"][0]["quantity"] == 12

    def test_empty_response_says_so(self):
        with pytest.raises(ExtractionError, match="empty"):
            parse_response("   ")

    def test_prose_is_reported_as_not_json(self):
        with pytest.raises(ExtractionError, match="did not return JSON"):
            parse_response("I could not find an order in this email.")

    def test_json_array_is_rejected(self):
        with pytest.raises(ExtractionError, match="expected a JSON object"):
            parse_response("[1, 2, 3]")


class TestValidate:

    def test_accepts_a_well_formed_order(self):
        customer, items = validate({
            "customer_name": "  Example Ltd. ",
            "items": [{"material_description": " 220 Ohm ", "quantity": "12"}],
        })
        assert customer == "Example Ltd."
        assert items == [{"material_description": "220 Ohm", "quantity": 12}]

    def test_missing_customer(self):
        with pytest.raises(ExtractionError, match="customer_name"):
            validate({"items": [{"material_description": "x", "quantity": 1}]})

    def test_no_items(self):
        with pytest.raises(ExtractionError, match="no items"):
            validate({"customer_name": "Example Ltd.", "items": []})

    def test_non_numeric_quantity_names_the_item(self):
        with pytest.raises(ExtractionError, match=r"220 Ohm.*non-numeric"):
            validate({"customer_name": "Example Ltd.", "items": [
                {"material_description": "220 Ohm", "quantity": "about twelve"}]})

    @pytest.mark.parametrize("quantity", [0, -3])
    def test_quantity_must_be_positive(self, quantity):
        with pytest.raises(ExtractionError, match="quantity"):
            validate({"customer_name": "Example Ltd.", "items": [
                {"material_description": "220 Ohm", "quantity": quantity}]})

    def test_item_missing_description(self):
        with pytest.raises(ExtractionError, match="material_description"):
            validate({"customer_name": "Example Ltd.",
                      "items": [{"quantity": 5}]})
