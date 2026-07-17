"""Auto-trace: the whole app recorded with ONE line — no decorators.

There is no @track anywhere in this file. ``enable()`` instruments the
process via ``sys.monitoring`` (stdlib, Python 3.12+): every function call
in *your* project code is recorded automatically — enter, arguments, exit,
result, duration, and any exception that escapes — while the stdlib and
third-party packages stay untraced (their call sites are switched off
inside the interpreter after the first hit, so they cost nothing).

Run it a few times:

    python examples/autotrace_demo.py

Then:

    slomo sessions
    slomo issues        # the crash below, with full argument capture
    slomo replay        # the whole call tree: main -> checkout -> load_inventory -> SQL

Tune it in .slomo/config.toml:

    [hooks.autotrace]
    enabled = true          # or SLOMO_AUTOTRACE=0 to switch off
    capture_args = true
    capture_results = true
    # include = ["/opt/shared-lib/*"]   # trace extra paths outside the project
    # exclude = ["*/generated/*"]       # mute noisy project paths
"""

import sqlite3

import slomo

slomo.enable()  # <- the only slomo line in the whole app


def load_inventory(db: sqlite3.Connection, sku: str):
    return db.execute("SELECT sku, qty FROM inventory WHERE sku = ?", (sku,)).fetchone()


def apply_discount(price: float, code: str) -> float:
    rates = {"WELCOME10": 0.10, "VIP": 0.25}
    return price * (1 - rates[code])  # BUG #2: unknown codes -> KeyError


def checkout(db: sqlite3.Connection, sku: str, discount: str | None = None):
    inventory = load_inventory(db, sku)
    price = 9.99
    if discount:
        price = apply_discount(price, discount)
    # BUG #1: inventory is None for unknown skus -> TypeError
    return {"sku": inventory[0], "qty": inventory[1], "price": round(price, 2)}


def main() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER)")
    db.execute("INSERT INTO inventory VALUES ('widget', 5)")
    print("checkout widget:", checkout(db, "widget", discount="WELCOME10"))
    print("checkout gadget:", checkout(db, "gadget"))  # crashes — and it's all on tape


if __name__ == "__main__":
    main()
