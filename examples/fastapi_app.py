"""FastAPI + slomo: a small shop API with a flight recorder.

What this demonstrates:

* ``enable()`` / ``disable()`` wired into the app lifespan
* ``@track`` on async and sync route handlers (order matters: the route
  decorator goes *outside* ``@track``) — exceptions raised inside a tracked
  handler are recorded even though FastAPI converts them to a 500 response
  before ``sys.excepthook`` ever fires
* an HTTP middleware that records one custom event per request
* the sqlite3 hook picking up every query automatically
* ``snapshot()`` to capture local state right before the buggy line
* a background task whose failure is also captured

Run it:

    pip install fastapi uvicorn
    python examples/fastapi_app.py

Then generate some traffic:

    curl http://127.0.0.1:8000/products
    curl http://127.0.0.1:8000/checkout/widget     # works
    curl http://127.0.0.1:8000/checkout/gadget     # 500 — Null Reference bug
    curl "http://127.0.0.1:8000/price/widget?qty=0" # 500 — ZeroDivisionError
    curl -X POST http://127.0.0.1:8000/restock/widget

Finally, explore the recording:

    slomo sessions
    slomo issues
    slomo doctor <issue-id>
    slomo replay <session-id>
"""

import contextlib
import logging
import sqlite3
import threading
import time

from fastapi import BackgroundTasks, FastAPI, Request

import slomo
from slomo import track

log = logging.getLogger("shop.api")

# One shared in-memory DB. FastAPI runs sync handlers in a threadpool, so the
# connection must allow cross-thread use; the lock keeps sqlite happy.
db = sqlite3.connect(":memory:", check_same_thread=False)
db_lock = threading.Lock()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Start recording when the server starts. Everything below —
    # sqlite queries, warnings, exceptions, custom events — lands in
    # .slomo/ automatically.
    slomo.enable(labels={"service": "shop-api", "example": "fastapi"})
    with db_lock:
        db.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER, price REAL)")
        db.executemany(
            "INSERT INTO inventory VALUES (?, ?, ?)",
            [("widget", 5, 9.99), ("doohickey", 12, 3.50)],
        )
    yield
    # Make sure the tail of the buffer hits disk before the process exits.
    slomo.flush()
    slomo.disable()


app = FastAPI(title="slomo shop demo", lifespan=lifespan)


@app.middleware("http")
async def record_requests(request: Request, call_next):
    """One custom event per request: method, path, status, duration."""
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        slomo.event(
            "http.request",
            severity="error",
            method=request.method,
            path=request.url.path,
            status=500,
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        raise
    slomo.event(
        "http.request",
        severity="error" if response.status_code >= 500 else "info",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - t0) * 1000, 2),
    )
    return response


# ---------------------------------------------------------------------------
# Data access — plain sync functions, spans nest under the route that calls
# them so `slomo replay` shows the full tree: route -> load_inventory -> SQL.
# ---------------------------------------------------------------------------


@track
def load_inventory(sku: str):
    with db_lock:
        row = db.execute("SELECT sku, qty, price FROM inventory WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        log.warning("inventory miss for sku=%s", sku)  # captured by the logging hook
    return row


@track
def notify_warehouse(sku: str, qty: int) -> None:
    """Background task. Fails for oversized restocks — and slomo still
    sees it, even though FastAPI background failures never reach your logs
    by default."""
    if qty > 100:
        raise ValueError(f"warehouse rejects restock of {qty} units")
    log.info("warehouse notified: %s x%s", sku, qty)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/products")
@track
async def list_products():
    with db_lock:
        rows = db.execute("SELECT sku, qty, price FROM inventory").fetchall()
    return [{"sku": r[0], "qty": r[1], "price": r[2]} for r in rows]


@app.get("/checkout/{sku}")
@track
async def checkout(sku: str):
    inventory = load_inventory(sku)
    # Capture the exact local state right before the risky line. When this
    # blows up, `slomo doctor` shows you inventory was None — no repro needed.
    slomo.snapshot("before-charge", sku=sku, inventory=inventory)
    # BUG: inventory is None for unknown skus -> TypeError (Null Reference)
    order = {"sku": inventory[0], "qty": 1, "total": inventory[2]}
    slomo.event("checkout.completed", sku=sku, total=order["total"])
    return order


@app.get("/price/{sku}")
@track
async def unit_price(sku: str, qty: int = 1):
    inventory = load_inventory(sku)
    if inventory is None:
        return {"error": "unknown sku"}
    # BUG: qty=0 -> ZeroDivisionError. A second, distinct issue fingerprint,
    # so `slomo issues` shows it separately from the checkout bug.
    return {"sku": sku, "bulk_unit_price": inventory[2] * inventory[1] / qty}


@app.post("/restock/{sku}")
@track
async def restock(sku: str, qty: int = 10, background: BackgroundTasks = None):
    with db_lock:
        db.execute("UPDATE inventory SET qty = qty + ? WHERE sku = ?", (qty, sku))
        db.commit()
    background.add_task(notify_warehouse, sku, qty)
    return {"sku": sku, "restocked": qty}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
