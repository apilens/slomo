# slomo examples

Each example is a self-contained script that plants a realistic bug, records
it, and tells you which `slomo` commands to run afterwards. Run any of them a few
times, then explore the recording from your terminal.

| Example | Shows | Extra deps |
|---|---|---|
| [`demo_app.py`](demo_app.py) | The 60-second demo: `enable()`, `@track`, `snapshot()`, sqlite hook, a Null Reference crash | none |
| [`autotrace_demo.py`](autotrace_demo.py) | Zero decorators: one `enable()` records every function call in project code automatically | none |
| [`fastapi_app.py`](fastapi_app.py) | FastAPI lifespan integration, tracked async routes, request middleware events, background-task failures | `fastapi`, `uvicorn` |
| [`flask_app.py`](flask_app.py) | Flask at import time, tracked views, per-request events via `before/after_request` | `flask` |
| [`background_worker.py`](background_worker.py) | Worker threads — crashes captured via `threading.excepthook`, tracked generators, per-job snapshots | none |
| [`async_pipeline.py`](async_pipeline.py) | asyncio concurrency — async functions & async generators, retries with snapshots, interleaved span trees | none |
| [`redaction_demo.py`](redaction_demo.py) | Secret redaction — by key name, by value pattern (JWT/bearer/card numbers), and `capture_args=False` | none |

## Quick start

```bash
pip install slomo fastapi uvicorn flask   # frameworks only needed for the web examples

python examples/demo_app.py          # crashes on purpose — that's the demo
python examples/demo_app.py
```

Then explore:

```bash
slomo sessions              # every recorded run of your process
slomo issues                # crashes deduplicated into issues, with counts
slomo doctor SM-xxxxxxxx    # diagnosis: category, variables at crash time, similar issues
slomo replay                # step through the latest session event by event
slomo replay SM-xxxxxxxx    # jump straight to the crash
```

## Do I need `@track`?

Usually not. Auto-tracing records every function call in project code —
including web-framework handlers, whose exceptions frameworks convert to 500
responses before `sys.excepthook` ever runs. The moment an exception escapes
your handler, it's on tape.

Reach for `@track` when you want more than the default: force-tracing code
that lives outside the project root, naming a span, or controlling arg/result
capture per function. If you do decorate a route handler, the route decorator
goes on the **outside**, `@track` on the **inside**:

```python
@app.get("/checkout/{sku}")   # FastAPI — or @app.route(...) for Flask
@track
async def checkout(sku: str):
    ...
```

Tracked functions are never double-recorded by auto-trace.

## Where recordings go

Everything lands in `.slomo/` in the working directory you launched the
process from — one folder per session, JSONL as the source of truth. Add
`.slomo/` to your `.gitignore` (recordings are local debugging data, not
something to commit).
