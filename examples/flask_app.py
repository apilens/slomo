"""Flask + slomo: same shop, WSGI edition.

What this demonstrates:

* ``enable()`` at import time — the simplest possible integration
* ``@track`` on view functions (route decorator outside, ``@track`` inside)
* Flask's error handling swallows exceptions before ``sys.excepthook`` runs,
  but tracked views record the exception at the moment it escapes the view —
  so the 500s still show up in ``slomo issues``
* ``before_request``/``after_request`` emitting one custom event per request

Run it:

    pip install flask
    python examples/flask_app.py

Traffic:

    curl http://127.0.0.1:5000/products
    curl http://127.0.0.1:5000/checkout/widget    # works
    curl http://127.0.0.1:5000/checkout/gadget    # 500 — recorded

Then: ``slomo sessions``, ``slomo issues``, ``slomo doctor <issue-id>``.
"""

import logging
import sqlite3
import time

from flask import Flask, g, jsonify, request

import slomo
from slomo import track

slomo.enable(labels={"service": "shop-api", "example": "flask"})

log = logging.getLogger("shop.flask")
app = Flask(__name__)

db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER)")
db.execute("INSERT INTO inventory VALUES ('widget', 5)")


@app.before_request
def start_timer():
    g.t0 = time.perf_counter()


@app.after_request
def record_request(response):
    slomo.event(
        "http.request",
        severity="error" if response.status_code >= 500 else "info",
        method=request.method,
        path=request.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - g.t0) * 1000, 2),
    )
    return response


@track
def load_inventory(sku: str):
    row = db.execute("SELECT sku, qty FROM inventory WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        log.warning("inventory miss for sku=%s", sku)
    return row


@app.route("/products")
@track
def products():
    rows = db.execute("SELECT sku, qty FROM inventory").fetchall()
    return jsonify([{"sku": r[0], "qty": r[1]} for r in rows])


@app.route("/checkout/<sku>")
@track
def checkout(sku: str):
    inventory = load_inventory(sku)
    slomo.snapshot("before-charge", sku=sku, inventory=inventory)
    # BUG: inventory is None for unknown skus
    return jsonify({"sku": inventory[0], "qty": inventory[1]})


if __name__ == "__main__":
    app.run(port=5000)
