"""Async pipeline + slomo: concurrent fetch -> transform -> load.

What this demonstrates:

* ``@track`` on async functions and async generators — spans nest correctly
  across ``await`` because propagation uses contextvars
* concurrent tasks under ``asyncio.gather`` each get their own span tree
* a retry loop that snapshots state before every attempt, so when the final
  attempt fails you can see all three attempts in ``slomo replay``
* ``event()`` severities: "warning" for retries, the crash itself is recorded
  automatically

Run it a few times:

    python examples/async_pipeline.py

Then:

    slomo sessions
    slomo issues
    slomo replay <session-id>   # watch the concurrent spans interleave
"""

import asyncio
import logging
import random

import slomo
from slomo import track

slomo.enable(labels={"service": "etl", "example": "asyncio"})

log = logging.getLogger("pipeline")

SOURCES = ["users", "orders", "payments", "refunds"]


@track
async def fetch_page(source: str, page: int) -> list[dict]:
    await asyncio.sleep(random.uniform(0.01, 0.05))  # pretend network call
    if source == "payments" and random.random() < 0.6:
        raise ConnectionError(f"upstream timeout fetching {source} page {page}")
    return [{"source": source, "page": page, "row": i} for i in range(3)]


@track
async def fetch_with_retry(source: str, page: int, attempts: int = 3) -> list[dict]:
    for attempt in range(1, attempts + 1):
        slomo.snapshot("before-attempt", source=source, page=page, attempt=attempt)
        try:
            return await fetch_page(source, page)
        except ConnectionError as exc:
            if attempt == attempts:
                raise  # recorded against this span, becomes an issue
            slomo.event(
                "fetch.retry", severity="warning", source=source, attempt=attempt, error=str(exc)
            )
            await asyncio.sleep(0.01 * attempt)
    return []


@track
async def stream_source(source: str):
    """Async generator: yields transformed rows page by page."""
    for page in range(2):
        for row in await fetch_with_retry(source, page):
            yield {**row, "transformed": True}


@track
async def load_source(source: str) -> int:
    count = 0
    async for _row in stream_source(source):
        count += 1
    slomo.event("load.done", source=source, rows=count)
    return count


async def main() -> None:
    results = await asyncio.gather(*(load_source(s) for s in SOURCES), return_exceptions=True)
    for source, result in zip(SOURCES, results, strict=True):
        status = f"{result} rows" if isinstance(result, int) else f"FAILED: {result}"
        print(f"{source:10s} {status}")
    slomo.flush()
    print("\ndone — now run: slomo issues")


if __name__ == "__main__":
    asyncio.run(main())
