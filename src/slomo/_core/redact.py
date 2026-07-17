"""Automatic redaction of secrets in payloads and variable snapshots.

Runs on JSON-safe trees (after ``to_jsonable``) so key matching is uniform.
User-configured patterns merge additively with the defaults.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

DEFAULT_KEY_PATTERNS = [
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth",
    "cookie",
    "set-cookie",
    "session_key",
    "sessionid",
    "private_key",
    "access_key",
    "jwt",
    "bearer",
    "credential",
    "ssn",
    "x-api-key",
]

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{40,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

DEFAULT_VALUE_PATTERNS = [_JWT_RE, _AWS_KEY_RE, _BEARER_RE, _LONG_HEX_RE]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(
        self,
        key_patterns: list[str] | None = None,
        value_patterns: list[str] | None = None,
        *,
        include_defaults: bool = True,
    ) -> None:
        keys = list(DEFAULT_KEY_PATTERNS) if include_defaults else []
        keys.extend(key_patterns or [])
        self._key_res = [re.compile(re.escape(k), re.IGNORECASE) for k in keys]
        values: list[re.Pattern[str]] = list(DEFAULT_VALUE_PATTERNS) if include_defaults else []
        for p in value_patterns or []:
            try:
                values.append(re.compile(p))
            except re.error:
                pass
        self._value_res = values
        self._check_cards = include_defaults

    def _key_matches(self, key: str) -> bool:
        return any(r.search(key) for r in self._key_res)

    def _redact_str(self, value: str) -> str:
        for r in self._value_res:
            value = r.sub(REDACTED, value)
        if self._check_cards:

            def _card(m: re.Match[str]) -> str:
                digits = re.sub(r"[ -]", "", m.group(0))
                return REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

            value = _CARD_RE.sub(_card, value)
        return value

    def redact(self, data: Any) -> Any:
        """Walk a jsonable tree; secret keys and secret-shaped values -> [REDACTED]."""
        if isinstance(data, str):
            return self._redact_str(data)
        if isinstance(data, list):
            return [self.redact(v) for v in data]
        if isinstance(data, dict):
            out: dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(k, str) and self._key_matches(k):
                    out[k] = REDACTED
                else:
                    out[k] = self.redact(v)
            return out
        return data

    def redact_headers(self, headers: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(k): (REDACTED if self._key_matches(str(k)) else self._redact_str(str(v)))
            for k, v in headers.items()
        }
