"""Redaction + slomo: record everything, leak nothing.

What this demonstrates:

* argument names like ``password``, ``api_key``, ``token`` are redacted by
  default before anything is written to disk — ``@track``'s captured args,
  ``snapshot()`` variables, and ``event()`` payloads all pass through the
  same redactor
* value patterns (bearer tokens, AWS-style keys) are scrubbed even when the
  variable name looks innocent
* extend the rules per project in ``.slomo/config.toml``::

      [redaction]
      extra_keys = ["internal_id"]
      extra_patterns = ["MYCO-[0-9]+"]

* ``capture_args=False`` / ``capture_result=False`` for functions whose
  values you'd rather not record at all

Run it, then verify nothing sensitive was stored:

    python examples/redaction_demo.py
    slomo sessions
    grep -ri "hunter2" .slomo/sessions/ || echo "secret not on disk ✔"
"""

import slomo
from slomo import track

slomo.enable(labels={"example": "redaction"})


@track
def login(username: str, password: str) -> dict:
    """`password` is redacted in the recorded args — by name."""
    session = {"user": username, "token": "Bearer sk-live-abc123def456"}
    # `token` is redacted by key name; the bearer value would also be caught
    # by the default value patterns.
    slomo.snapshot("session-created", session=session)
    return session


@track(capture_args=False, capture_result=False)
def rotate_credentials(old_secret: str, new_secret: str) -> str:
    """Belt and braces: don't capture args or result at all."""
    return f"rotated ({len(old_secret)} -> {len(new_secret)} chars)"


def main() -> None:
    session = login("amit", password="hunter2-secret")
    print("logged in:", session["user"])

    print(rotate_credentials("hunter2-secret", "correct-horse-battery-staple"))

    # Event payloads are redacted too. The card number below isn't caught by
    # key name — it's caught by the Luhn-checked value pattern.
    slomo.event("billing.charge", customer="amit", card_number="4242424242424242")

    slomo.flush()
    print("\nnow inspect: slomo replay  (secrets show as [REDACTED])")


if __name__ == "__main__":
    main()
