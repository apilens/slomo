"""Background worker + slomo: threads, queues, and crashes you'd
otherwise never see.

What this demonstrates:

* exceptions in worker threads are captured via ``threading.excepthook`` —
  normally these print to stderr and vanish; here they become issues
* ``@track`` on a generator (the job feed) — the span covers the whole
  iteration, and a crash mid-iteration is attributed to it
* ``snapshot()`` before each risky step, so ``slomo doctor`` can show exactly
  which job was in flight when a worker died
* one recording session covering the main thread and all workers

Run it a few times:

    python examples/background_worker.py

Then:

    slomo sessions
    slomo issues        # the poisoned job surfaces as its own issue
    slomo doctor <issue-id>
"""

import logging
import queue
import random
import threading
import time

import slomo
from slomo import track

slomo.enable(labels={"service": "image-worker", "example": "threads"})

log = logging.getLogger("worker")
jobs: queue.Queue = queue.Queue()


@track
def job_feed(n: int):
    """Generator producing jobs; spans work for generators too."""
    for i in range(n):
        # every run has one poisoned job with a non-numeric size
        size = "huge" if i == n - 1 else random.randint(100, 4000)
        yield {"job_id": f"img-{i:03d}", "size_kb": size}


@track
def resize_image(job: dict) -> dict:
    slomo.snapshot("before-resize", job=job)
    # BUG: size_kb is sometimes a string -> TypeError in the comparison
    scale = 0.5 if job["size_kb"] > 1000 else 1.0
    time.sleep(0.01)  # pretend to do work
    return {"job_id": job["job_id"], "scale": scale}


def worker(worker_id: int) -> None:
    while True:
        job = jobs.get()
        if job is None:  # shutdown signal
            return
        # No try/except here on purpose: when resize_image blows up, the
        # thread dies — and slomo's threading.excepthook records the
        # exception and fsync-flushes before the thread is gone.
        result = resize_image(job)
        slomo.event("job.done", worker=worker_id, **result)


def main() -> None:
    threads = [threading.Thread(target=worker, args=(i,), name=f"worker-{i}") for i in range(3)]
    for t in threads:
        t.start()

    for job in job_feed(10):
        jobs.put(job)

    for _ in threads:
        jobs.put(None)
    for t in threads:
        t.join()

    slomo.flush()
    print("done — now run: slomo issues")


if __name__ == "__main__":
    main()
