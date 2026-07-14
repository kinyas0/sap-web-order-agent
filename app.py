import json

import streamlit as st
from openai import OpenAI

from config.settings import Settings
from services.sap_service import find_customer_code, find_material_code, create_sales_order


# -------------------------------------------------
# OPENROUTER
# -------------------------------------------------

client = OpenAI(
    api_key=Settings.OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)


# -------------------------------------------------
# AI ORDER PROCESS
# -------------------------------------------------

def process_order(order_text):

    prompt = f"""
You are an enterprise SAP sales order extraction AI.

Your task:
Extract:
- customer company name
- material descriptions
- quantities

Rules:
- Return ONLY valid JSON
- Do not explain
- Do not add markdown
- Preserve product names exactly
- Quantity must always be integer
- Ignore greetings and signatures
- Customer name is always a company name, never a person's name.
- Customer and material names may be in Turkish language. Don't mind the Turkish characters.

JSON format:

{{
  "customer_name": "Eksen Mekatronik Ltd. Şti.",
  "items": [
    {{
      "material_description": "220 Ohm Direnç 1/4W",
      "quantity": 12
    }}
  ]
}}

Order Text:

{order_text}
"""

    response = client.chat.completions.create(
        model=Settings.OPENROUTER_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )

    agent_payload = json.loads(response.choices[0].message.content)

    customer_code = find_customer_code(agent_payload["customer_name"])

    resolved_items = [
        {
            "material": find_material_code(item["material_description"]),
            "quantity": item["quantity"]
        }
        for item in agent_payload["items"]
    ]

    if len(resolved_items) == 0:
        raise Exception("Siparişte geçerli ürün bulunamadı.")

    sales_payload = {"customer": customer_code, "items": resolved_items}

    sales_doc = create_sales_order(sales_payload)

    return f"✅ Sipariş oluşturuldu.\nBelge No: {sales_doc}"


# -------------------------------------------------
# STREAMLIT
# -------------------------------------------------

st.set_page_config(page_title="SAP Sales Agent", page_icon="🤖")
st.title("🤖 SAP Sales Agent")


# -------------------------------------------------
# SESSION
# -------------------------------------------------

if "step" not in st.session_state:
    st.session_state.step = 1

if "customer_mode" not in st.session_state:
    st.session_state.customer_mode = "Text"

if "customer_input" not in st.session_state:
    st.session_state.customer_input = ""

if "material_mode" not in st.session_state:
    st.session_state.material_mode = "Text"


# -------------------------------------------------
# STEP 1 - CUSTOMER
# -------------------------------------------------

if st.session_state.step == 1:

    st.subheader("Müşteri Bilgisi")

    customer_mode = st.selectbox("Müşteri giriş tipi", ["Text", "Structured"])

    customer_input = st.text_input("Müşteri", placeholder="Örn: Eksen veya 0000000003")

    if st.button("Devam Et"):

        if customer_input.strip() == "":
            st.error("Müşteri girilmeli.")
        else:
            st.session_state.customer_mode = customer_mode
            st.session_state.customer_input = customer_input
            st.session_state.step = 2
            st.rerun()


# -------------------------------------------------
# STEP 2 - MATERIALS
# -------------------------------------------------

elif st.session_state.step == 2:

    st.subheader("Malzeme Girişi")

    material_mode = st.selectbox("Malzeme giriş tipi", ["Text", "Structured"])
    st.session_state.material_mode = material_mode

    item_count = st.number_input("Kaç kalem ürün?", min_value=1, max_value=20, value=1)

    items = []

    st.markdown("### Ürünler")

    for i in range(item_count):

        col1, col2 = st.columns([3, 1])

        with col1:
            material = st.text_input(f"Malzeme {i + 1}", key=f"material_{i}")

        with col2:
            quantity = st.number_input(f"Miktar {i + 1}", min_value=1, value=1, key=f"qty_{i}")

        items.append({"material": material, "quantity": quantity})

    if st.button("Siparişi Oluştur"):

        try:
            if st.session_state.customer_mode == "Structured":
                customer_code = st.session_state.customer_input.zfill(10)
            else:
                customer_code = find_customer_code(st.session_state.customer_input)

            resolved_items = []

            for item in items:

                if item["material"].strip() == "":
                    continue

                if material_mode == "Structured":
                    material_code = item["material"].zfill(18)
                else:
                    material_code = find_material_code(item["material"])

                resolved_items.append({"material": material_code, "quantity": item["quantity"]})

            if len(resolved_items) == 0:
                raise Exception("En az 1 ürün gerekli.")

            sales_payload = {"customer": customer_code, "items": resolved_items}

            sales_doc = create_sales_order(sales_payload)

            st.success(f"Sipariş oluşturuldu.\n\nBelge No:\n{sales_doc}")

            st.session_state.step = 1
            st.session_state.customer_mode = "Text"
            st.session_state.customer_input = ""
            st.session_state.material_mode = "Text"

            st.rerun()

        except Exception as e:
            st.error(str(e))
