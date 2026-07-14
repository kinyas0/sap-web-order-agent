import os
from dotenv import load_dotenv

load_dotenv()


class Settings:

    SAP_CONFIG = {
        "ashost": os.getenv("SAP_ASHOST"),
        "sysnr": os.getenv("SAP_SYSNR"),
        "client": os.getenv("SAP_CLIENT"),
        "user": os.getenv("SAP_USER"),
        "passwd": os.getenv("SAP_PASSWORD"),
        "lang": os.getenv("SAP_LANG", "EN")
    }

    DB_CONFIG = {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD")
    }

    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")

    MATCH_SCORE_THRESHOLD = int(os.getenv("MATCH_SCORE_THRESHOLD", "80"))
