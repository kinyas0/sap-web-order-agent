"""Configuration, read once from the environment and validated on the way in.

Values arrive from ``.env`` as strings. Coercing them here - and failing loudly on
a required one that is missing - beats discovering halfway through a sales order
that ``DB_PORT`` was the string ``"5432"`` and the pool would not take it.
"""

from __future__ import annotations

import os
from typing import ClassVar
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


class Settings:
    """Everything the services need, in one place."""

    # -- SAP ---------------------------------------------------------------

    SAP_CONFIG: ClassVar[dict[str, str | None]] = {
        "ashost": os.getenv("SAP_ASHOST"),
        "sysnr": os.getenv("SAP_SYSNR"),
        "client": os.getenv("SAP_CLIENT"),
        "user": os.getenv("SAP_USER"),
        "passwd": os.getenv("SAP_PASSWORD"),
        "lang": os.getenv("SAP_LANG", "EN"),
    }

    #: Sales area the order is booked into. Hardcoding these made the service
    #: usable in exactly one client; they belong in configuration.
    SAP_DOC_TYPE = os.getenv("SAP_DOC_TYPE", "TA")
    SAP_SALES_ORG = os.getenv("SAP_SALES_ORG", "0001")
    SAP_DISTR_CHAN = os.getenv("SAP_DISTR_CHAN", "01")
    SAP_DIVISION = os.getenv("SAP_DIVISION", "01")

    #: Language key for material descriptions in MAKT. Without it the lookup gets
    #: every translation of every material and fuzzy-matches across languages.
    SAP_MATERIAL_LANGUAGE = os.getenv("SAP_MATERIAL_LANGUAGE", "E")

    #: Hard cap on rows pulled per master-data read. RFC_READ_TABLE has no server
    #: side paging worth the name, so this is the guard against reading a table
    #: that has grown since the last time anyone looked.
    SAP_READ_ROW_LIMIT = _int("SAP_READ_ROW_LIMIT", 20_000)

    #: How long master data stays cached in-process. The old code re-read all of
    #: KNA1 and all of MAKT for every single line of every order.
    SAP_CACHE_TTL_SECONDS = _int("SAP_CACHE_TTL_SECONDS", 900)

    # -- database ----------------------------------------------------------

    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = _int("DB_PORT", 5432)
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")

    DB_POOL_MAX_SIZE = _int("DB_POOL_MAX_SIZE", 4)
    DB_CONNECT_TIMEOUT_SECONDS = _int("DB_CONNECT_TIMEOUT_SECONDS", 5)

    @classmethod
    def database_configured(cls) -> bool:
        """The audit trail is optional; this says whether it was asked for."""
        return bool(cls.DB_HOST and cls.DB_NAME and cls.DB_USER)

    @classmethod
    def database_url(cls) -> str:
        password = f":{quote(cls.DB_PASSWORD, safe='')}" if cls.DB_PASSWORD else ""
        return (f"postgresql://{quote(cls.DB_USER or '', safe='')}{password}"
                f"@{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}")

    # -- language model ----------------------------------------------------

    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL",
                                    "https://openrouter.ai/api/v1")
    LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 60)
    LLM_MAX_ATTEMPTS = _int("LLM_MAX_ATTEMPTS", 2)

    # -- matching ----------------------------------------------------------

    MATCH_SCORE_THRESHOLD = _int("MATCH_SCORE_THRESHOLD", 70)

    # -- logging -----------------------------------------------------------

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def require_sap(cls) -> None:
        """Fail before the first RFC call rather than inside it."""
        missing = [key for key in ("ashost", "sysnr", "client", "user", "passwd")
                   if not cls.SAP_CONFIG.get(key)]
        if missing:
            names = ", ".join(f"SAP_{key.upper()}" for key in missing)
            raise RuntimeError(f"SAP connection is not configured: {names} missing")

    @classmethod
    def require_llm(cls) -> None:
        if not cls.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
