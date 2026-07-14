import pyrfc

from config.settings import Settings


def get_connection():
    return pyrfc.Connection(**Settings.SAP_CONFIG)


def find_customer_code(customer_name, scorer_threshold=None):
    from rapidfuzz import process, fuzz

    threshold = scorer_threshold or Settings.MATCH_SCORE_THRESHOLD

    result = get_connection().call(
        "RFC_READ_TABLE",
        QUERY_TABLE="KNA1",
        DELIMITER="|",
        FIELDS=[
            {"FIELDNAME": "KUNNR"},
            {"FIELDNAME": "NAME1"}
        ]
    )

    customers = []

    for row in result["DATA"]:
        splitted = row["WA"].split("|")
        customers.append({
            "kunnr": splitted[0].strip(),
            "name1": splitted[1].strip()
        })

    customer_names = [c["name1"] for c in customers]

    best_match = process.extractOne(
        customer_name,
        customer_names,
        scorer=fuzz.token_sort_ratio
    )

    if not best_match:
        raise Exception("Müşteri bulunamadı.")

    matched_name, score = best_match[0], best_match[1]

    if score < threshold:
        raise Exception("Müşteri eşleşme skoru düşük.")

    for customer in customers:
        if customer["name1"] == matched_name:
            return customer["kunnr"].zfill(10)

    raise Exception("Customer code bulunamadı.")


def find_material_code(material_description, scorer_threshold=None):
    from rapidfuzz import process, fuzz

    threshold = scorer_threshold or Settings.MATCH_SCORE_THRESHOLD

    result = get_connection().call(
        "RFC_READ_TABLE",
        QUERY_TABLE="MAKT",
        DELIMITER="|",
        FIELDS=[
            {"FIELDNAME": "MATNR"},
            {"FIELDNAME": "MAKTX"}
        ]
    )

    materials = []

    for row in result["DATA"]:
        splitted = row["WA"].split("|")
        materials.append({
            "matnr": splitted[0].strip(),
            "maktx": splitted[1].strip()
        })

    material_names = [m["maktx"] for m in materials]

    best_match = process.extractOne(
        material_description,
        material_names,
        scorer=fuzz.token_sort_ratio
    )

    if not best_match:
        raise Exception("Malzeme bulunamadı.")

    matched_name, score = best_match[0], best_match[1]

    if score < threshold:
        raise Exception("Malzeme eşleşme skoru düşük.")

    for material in materials:
        if material["maktx"] == matched_name:
            return material["matnr"].zfill(18)

    raise Exception("Material code bulunamadı.")


def create_sales_order(payload):
    conn = get_connection()

    order_items_in = []
    order_items_inx = []
    order_schedules_in = []
    order_schedules_inx = []

    for idx, item in enumerate(payload["items"], start=1):
        itm_number = str(idx * 10).zfill(6)

        order_items_in.append({
            "ITM_NUMBER": itm_number,
            "MATERIAL": item["material"],
            "TARGET_QTY": str(item["quantity"])
        })

        order_items_inx.append({
            "ITM_NUMBER": itm_number,
            "UPDATEFLAG": "I",
            "MATERIAL": "X",
            "TARGET_QTY": "X"
        })

        order_schedules_in.append({
            "ITM_NUMBER": itm_number,
            "SCHED_LINE": "0001",
            "REQ_QTY": str(item["quantity"])
        })

        order_schedules_inx.append({
            "ITM_NUMBER": itm_number,
            "SCHED_LINE": "0001",
            "UPDATEFLAG": "I",
            "REQ_QTY": "X"
        })

    result = conn.call(
        "BAPI_SALESORDER_CREATEFROMDAT2",

        ORDER_HEADER_IN={
            "DOC_TYPE": "TA",
            "SALES_ORG": "0001",
            "DISTR_CHAN": "01",
            "DIVISION": "01"
        },

        ORDER_HEADER_INX={
            "UPDATEFLAG": "I",
            "DOC_TYPE": "X",
            "SALES_ORG": "X",
            "DISTR_CHAN": "X",
            "DIVISION": "X"
        },

        ORDER_PARTNERS=[
            {"PARTN_ROLE": "AG", "PARTN_NUMB": payload["customer"]},
            {"PARTN_ROLE": "WE", "PARTN_NUMB": payload["customer"]}
        ],

        ORDER_ITEMS_IN=order_items_in,
        ORDER_ITEMS_INX=order_items_inx,
        ORDER_SCHEDULES_IN=order_schedules_in,
        ORDER_SCHEDULES_INX=order_schedules_inx
    )

    has_error = False

    for msg in result["RETURN"]:
        print(f"{msg['TYPE']} - {msg['MESSAGE']}")
        if msg["TYPE"] in ["E", "A"]:
            has_error = True

    sales_doc = result.get("SALESDOCUMENT")

    if not has_error and sales_doc:
        conn.call("BAPI_TRANSACTION_COMMIT", WAIT="X")
        return sales_doc

    conn.call("BAPI_TRANSACTION_ROLLBACK")
    raise Exception("SAP siparişi oluşturulamadı.")
