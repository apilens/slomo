"""Demo app for slomo: does sqlite + logging work, then crashes on a
None attribute — the canonical Null Reference issue.

Run it a few times, then explore with `slomo sessions`, `slomo issues`, `slomo doctor`.
"""

import logging
import sqlite3

import slomo
from slomo import track

slomo.enable()

log = logging.getLogger("demo.checkout")


@track
def load_inventory(db: sqlite3.Connection, sku: str):
    row = db.execute("SELECT sku, qty FROM inventory WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        log.warning("inventory miss for sku=%s", sku)
    return row


@track
def checkout(db: sqlite3.Connection, sku: str, password: str = "hunter2-secret"):
    inventory = load_inventory(db, sku)
    slomo.snapshot("before-charge", sku=sku, inventory=inventory)
    # BUG: inventory can be None for unknown skus
    return {"sku": inventory[0], "qty": inventory[1]}


def main() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER)")
    db.execute("INSERT INTO inventory VALUES ('widget', 5)")
    print("checkout widget:", checkout(db, "widget"))
    print("checkout gadget:", checkout(db, "gadget"))  # crashes


if __name__ == "__main__":
    main()
