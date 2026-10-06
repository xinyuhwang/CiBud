"""Background worker: claims jobs and dispatches them to registered handlers.

Handlers are ``async def handler(job: Job) -> dict | None``. Each claim, and each
completion or failure, is its own short transaction, so a handler's (possibly slow) LLM
calls never hold a database lock.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cibud.db.session import transaction
from cibud.jobs import queue
from cibud.models.job import Job

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[dict[str, Any] | None]]


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        handlers: dict[str, Handler],
        worker_id: str | None = None,
    ) -> None:
        self.factory = factory
        self.handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run_once(self) -> Job | None:
        """Claim and run one job. Returns the claimed job, or None if the queue was empty."""
        with transaction(self.factory) as session:
            job = queue.claim(session, self.worker_id, types=list(self.handlers))
        if job is None:
            return None

        try:
            result = await self.handlers[job.type](job)
        except Exception as exc:
            log.exception("job %s (%s) failed", job.id, job.type)
            with transaction(self.factory) as session:
                queue.fail(session, job.id, f"{type(exc).__name__}: {exc}")
        else:
            with transaction(self.factory) as session:
                queue.complete(session, job.id, result)
        return job

    async def run_forever(self, poll_interval: float = 1.0) -> None:
        log.info("worker %s handling %s", self.worker_id, sorted(self.handlers))
        while True:
            if await self.run_once() is None:
                await asyncio.sleep(poll_interval)
