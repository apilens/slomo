"""Heuristic issue classification: (category, severity, confidence)."""

from __future__ import annotations

import enum
import re
from typing import Any

from slomo._core.events import Severity


class Category(enum.StrEnum):
    NULL_REFERENCE = "Null Reference"
    NETWORK = "Network"
    DATABASE = "Database"
    FILESYSTEM = "Filesystem"
    AUTHENTICATION = "Authentication"
    AUTHORIZATION = "Authorization"
    VALIDATION = "Validation"
    TIMEOUT = "Timeout"
    MEMORY = "Memory"
    RESOURCE_EXHAUSTION = "Resource Exhaustion"
    CONFIGURATION = "Configuration"
    DEPENDENCY = "Dependency"
    PROGRAMMING_ERROR = "Programming Error"
    UNKNOWN = "Unknown"


_NONE_RE = re.compile(r"\bNoneType\b|\bis not subscriptable\b.*None|'NoneType'")
_TIMEOUT_RE = re.compile(r"(?i)\btim(?:ed?)[ -]?out\b|\btimeout\b")
_AUTHZ_RE = re.compile(r"(?i)\b(403|forbidden|permission denied|not authorized|access denied)\b")
_AUTHN_RE = re.compile(r"(?i)\b(401|unauthorized|authentication|invalid credentials|login)\b")
_DB_MODULE_RE = re.compile(r"(?i)sqlite3|psycopg|pymysql|mysql|sqlalchemy|asyncpg|pymongo|redis")
_NET_MODULE_RE = re.compile(r"(?i)socket|urllib|requests|httpx|http\.client|aiohttp|ssl|dns")


def classify(
    exc_type: str,
    exc_module: str,
    message: str,
    frames: list[dict[str, Any]] | None = None,
) -> tuple[Category, Severity, float]:
    msg = message or ""
    module = exc_module or ""

    if _TIMEOUT_RE.search(msg) or exc_type in ("TimeoutError", "ConnectTimeout", "ReadTimeout"):
        return Category.TIMEOUT, Severity.ERROR, 0.9
    if exc_type in ("AttributeError", "TypeError") and _NONE_RE.search(msg):
        return Category.NULL_REFERENCE, Severity.ERROR, 0.95
    if exc_type in ("MemoryError",):
        return Category.MEMORY, Severity.CRITICAL, 0.95
    if exc_type in ("RecursionError",) or "too many open files" in msg.lower():
        return Category.RESOURCE_EXHAUSTION, Severity.CRITICAL, 0.9
    if exc_type in ("ModuleNotFoundError", "ImportError"):
        return Category.DEPENDENCY, Severity.ERROR, 0.9
    if _DB_MODULE_RE.search(module) or exc_type in (
        "OperationalError",
        "IntegrityError",
        "DatabaseError",
        "ProgrammingError",
        "InterfaceError",
    ):
        return Category.DATABASE, Severity.ERROR, 0.85
    if _NET_MODULE_RE.search(module) or exc_type in (
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "SSLError",
        "gaierror",
    ):
        return Category.NETWORK, Severity.ERROR, 0.85
    if exc_type in (
        "FileNotFoundError",
        "IsADirectoryError",
        "NotADirectoryError",
        "FileExistsError",
    ):
        return Category.FILESYSTEM, Severity.ERROR, 0.9
    if exc_type == "PermissionError":
        return Category.AUTHORIZATION, Severity.ERROR, 0.7
    if _AUTHN_RE.search(msg):
        return Category.AUTHENTICATION, Severity.ERROR, 0.7
    if _AUTHZ_RE.search(msg):
        return Category.AUTHORIZATION, Severity.ERROR, 0.7
    if exc_type in ("KeyError",) and re.search(r"(?i)env|config|setting", msg):
        return Category.CONFIGURATION, Severity.ERROR, 0.7
    if exc_type in ("AssertionError", "ValueError") or "validation" in exc_type.lower():
        return Category.VALIDATION, Severity.ERROR, 0.65
    if exc_type == "OSError":
        return Category.FILESYSTEM, Severity.ERROR, 0.5
    if exc_type in (
        "TypeError",
        "AttributeError",
        "IndexError",
        "KeyError",
        "UnboundLocalError",
        "NameError",
        "NotImplementedError",
        "ZeroDivisionError",
        "StopIteration",
    ):
        return Category.PROGRAMMING_ERROR, Severity.ERROR, 0.6
    return Category.UNKNOWN, Severity.ERROR, 0.2
