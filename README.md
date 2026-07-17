# slomo

**The black box flight recorder for Python applications.**

Stop asking developers to reproduce bugs. Start letting them replay exactly what happened.

```bash
pip install slomo
```

```python
from slomo import enable

enable()
```

That's it. Zero configuration. No dashboard, no browser, no Docker, no external
database, no account, no telemetry. Everything is recorded locally under
`.slomo/` and explored entirely from your terminal.

---

## The 60-second demo

```python
# app.py
import sqlite3
import slomo
from slomo import track

slomo.enable()

@track
def load_inventory(db, sku):
    return db.execute("SELECT sku, qty FROM inventory WHERE sku = ?", (sku,)).fetchone()

@track
def checkout(db, sku):
    inventory = load_inventory(db, sku)
    return {"sku": inventory[0], "qty": inventory[1]}   # BUG: inventory can be None

db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER)")
db.execute("INSERT INTO inventory VALUES ('widget', 5)")
checkout(db, "widget")   # works
checkout(db, "gadget")   # crashes
```

Run it a few times, then:

```console
$ slomo issues
┃ issue        ┃ title                                      ┃ category       ┃ count ┃
│ SM-8b6f710a  │ TypeError: 'NoneType' object is not subsc… │ Null Reference │     5 │

$ slomo doctor SM-8b6f710a
Category              Null Reference  (95% confidence)
Occurrences           5  (5 unhandled) across 5 session(s)
Likely root cause     TypeError raised in checkout() at app.py:14.
                      Variable 'inventory' was None at the crash site.
First bad function    checkout()
First bad variable    inventory
Suggested fix         Guard against None before the failing access at app.py:14
                      — trace where the value is produced and handle the missing case.
Context just before the crash:
  13:04:52.133 sql.query   SELECT sku, qty FROM inventory WHERE sku = ?
  13:04:52.133 sql.result  0 rows, 9µs

$ slomo replay SM-8b6f710a        # step through the crash, event by event
```

## Using it with FastAPI, Flask, workers, asyncio…

Web frameworks turn exceptions into 500 responses before `sys.excepthook` can
see them — but auto-tracing records the exception the moment it escapes your
handler, so those 500s land in `slomo issues` with args, locals, and the SQL/HTTP
calls that led up to them. No decorators needed:

```python
from fastapi import FastAPI
import slomo

slomo.enable()   # that's the whole integration
app = FastAPI()

@app.get("/checkout/{sku}")
async def checkout(sku: str): ...
```

The [`examples/`](examples/) directory has complete, runnable apps — each one
plants a realistic bug and shows the `slomo` commands that catch it:

- [`autotrace_demo.py`](examples/autotrace_demo.py) — the whole app on tape with one line, zero decorators
- [`fastapi_app.py`](examples/fastapi_app.py) — lifespan integration, tracked async routes, per-request middleware events, background-task failures
- [`flask_app.py`](examples/flask_app.py) — tracked views, per-request events
- [`background_worker.py`](examples/background_worker.py) — worker-thread crashes you'd otherwise never see
- [`async_pipeline.py`](examples/async_pipeline.py) — asyncio concurrency, retries, async generators
- [`redaction_demo.py`](examples/redaction_demo.py) — proof your secrets never hit disk

## What gets recorded

After `enable()`, automatically:

- **Every function call in your project code** — enter, arguments, exit, result, duration, and any exception that escapes, via `sys.monitoring` (PEP 669, stdlib). Only files in your project are traced; stdlib and third-party call sites are switched off inside the interpreter after their first hit, so they cost nothing
- **Unhandled exceptions** — main thread, worker threads, unraisable errors, with structured tracebacks and **local variables from the crashing frames**
- **SQL queries** — `sqlite3` out of the box, everything else via SQLAlchemy's event API (Postgres, MySQL, ...)
- **HTTP calls** — `requests` and `httpx` (sync + async), request/response pairs correlated
- **Log records** — `logging` WARNING and above
- Session metadata: argv, cwd, python version, host, pid, exit status

Opt-in, where you want more than the automatic capture:

```python
from slomo import track, snapshot, event

@track                              # force-trace code outside the project root,
def process_order(order_id): ...    # or take control of arg/result capture

snapshot("before-retry", user=user, attempt=n)   # explicit variable snapshot
event("cache.warmed", entries=1042)              # custom event
```

Auto-tracing is tunable in `.slomo/config.toml` (`[hooks.autotrace]`:
`enabled`, `capture_args`, `capture_results`, `include`/`exclude` globs) or
switched off entirely with `SLOMO_AUTOTRACE=0`. Functions carrying
`@track` are never recorded twice.

## The CLI

| Command | What it does |
|---|---|
| `slomo` | interactive shell (history + tab completion) |
| `slomo sessions` / `slomo session show\|inspect\|delete ID` | list runs; `inspect` draws the span tree |
| `slomo issues` / `slomo issue show\|occurrences\|timeline\|resolve\|reopen\|explain ID` | crashes grouped by fingerprint |
| `slomo doctor ISSUE` | heuristic root-cause diagnosis |
| `slomo replay ISSUE\|SESSION` | interactive step-through (`n`/`p`/`j`/`t`/`/search`/`i`nspect/`v`ars) |
| `slomo timeline [REF] [--follow] [--errors]` | chronological feed; `--follow` live-tails a running app |
| `slomo search QUERY` | full-text + field filters: `slomo search timeout module=checkout user=42` |
| `slomo vars` / `slomo http` / `slomo sql` | typed views over a session or issue |
| `slomo stats [--rebuild-index]` | totals, categories, storage |
| `slomo export json\|markdown\|csv\|html` | shareable exports |
| `slomo prune` | delete oldest sessions beyond the retention limit |

## How issues work

A crash is **not** an issue — it's an *incident*. Incidents are fingerprinted
(exception type + normalized stack + normalized message, line numbers and
volatile ids excluded) so the same bug tomorrow lands in the same issue with
`occurrences += 1`, instead of burying you in duplicates. Issues get automatic
**category** (Null Reference, Network, Database, Timeout, ...), **severity**,
**stability** (one-time / intermittent / recurring), and a **confidence** score.
`slomo issue resolve` marks one fixed — it auto-reopens if it ever comes back.
Near-miss crashes are surfaced as "possibly related" (never auto-merged).

## Privacy

Values under keys like `password`, `token`, `authorization`, `cookie`,
`api_key`, ... and secret-shaped values (JWTs, bearer tokens, AWS keys,
Luhn-valid card numbers) are redacted **at capture time** — secrets never
touch disk. Add your own rules in `.slomo/config.toml`:

```toml
[redaction]
extra_keys = ["internal_id"]
extra_patterns = ["MYCO-[0-9]+"]
```

## Design guarantees

- **Fast**: `enable()` < 5 ms (enforced by a test); events go through one
  lock-free queue put; a background thread batches writes. The recorder
  imports zero CLI dependencies.
- **Crash-safe**: append-only JSONL, fsync on the crash path, tolerant reader
  — a `kill -9` loses at most the final partial line.
- **Never breaks your app**: every hook callback is exception-isolated;
  storage failures are swallowed; backpressure drops low-severity events
  rather than blocking.
- **Multi-process-safe**: one session per process; forked children start
  their own session (labeled `forked_from`).
- **Rebuildable**: JSONL timelines are the source of truth; the SQLite issue
  index is a cache (`slomo stats --rebuild-index`).

## Storage layout

```
.slomo/
  config.toml
  sessions/<timestamp>-<id>/
    metadata.json
    timeline.jsonl
    snapshots/          # oversized variable captures
    attachments/
  issues/index.sqlite   # derived, rebuildable
  exports/
  cache/
```

Requires Python 3.12+. MIT licensed.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```
