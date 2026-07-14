import psycopg
from psycopg.rows import dict_row

from config.settings import Settings


class DBService:

    def __init__(self):
        self.conn = psycopg.connect(
            **Settings.DB_CONFIG,
            row_factory=dict_row
        )

    # -------------------------------------------------
    # GENERIC
    # -------------------------------------------------

    def execute(self, query, params=None):
        with self.conn.cursor() as cur:
            cur.execute(query, params)
        self.conn.commit()

    def fetch_one(self, query, params=None):
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchone()
        self.conn.commit()
        return result

    def fetch_all(self, query, params=None):
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchall()
        return result

    # -------------------------------------------------
    # TRANSACTION
    # -------------------------------------------------

    def create_transaction(self, transaction_type, source):
        query = """
        INSERT INTO transactions
        (transaction_type, current_state, source)
        VALUES (%s, %s, %s)
        RETURNING id
        """
        result = self.fetch_one(query, (transaction_type, "STARTED", source))
        return result["id"]

    def update_transaction_state(self, transaction_id, new_state):
        query = """
        UPDATE transactions
        SET current_state = %s
        WHERE id = %s
        """
        self.execute(query, (new_state, transaction_id))

    # -------------------------------------------------
    # AGENT RUNS
    # -------------------------------------------------

    def log_agent_run(self, transaction_id, agent_name, status, input_data=None, output_data=None):
        query = """
        INSERT INTO agent_runs
        (transaction_id, agent_name, status, input_data, output_data)
        VALUES (%s, %s, %s, %s, %s)
        """
        self.execute(query, (transaction_id, agent_name, status, input_data, output_data))

    # -------------------------------------------------
    # ERP ACTIONS
    # -------------------------------------------------

    def log_erp_action(self, transaction_id, action_type, status, request_data=None, response_data=None):
        query = """
        INSERT INTO erp_actions
        (transaction_id, erp_system, action_type, status, request_data, response_data)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        self.execute(query, (transaction_id, "SAP", action_type, status, request_data, response_data))

    # -------------------------------------------------
    # ERRORS
    # -------------------------------------------------

    def log_error(self, transaction_id, service_name, error_message):
        query = """
        INSERT INTO system_errors
        (transaction_id, service_name, error_message)
        VALUES (%s, %s, %s)
        """
        self.execute(query, (transaction_id, service_name, error_message))
