"""``cibud-worker``: run background jobs (PDF extraction, ...)."""

import argparse
import asyncio
import logging

from cibud.api.deps import get_store
from cibud.db.session import session_factory
from cibud.ingestion.grobid import GrobidClient
from cibud.ingestion.service import EXTRACT_PDF, make_extract_handler
from cibud.jobs.worker import Handler, Worker
from cibud.settings import get_settings


async def _run(once: bool) -> None:
    settings = get_settings()
    factory = session_factory()
    grobid = GrobidClient(settings.grobid_url, timeout=settings.grobid_timeout_seconds)
    handlers: dict[str, Handler] = {
        EXTRACT_PDF: make_extract_handler(factory, get_store(), grobid),
    }
    worker = Worker(factory, handlers)
    try:
        if once:
            while await worker.run_once() is not None:
                pass
        else:
            await worker.run_forever()
    finally:
        await grobid.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cibud-worker")
    parser.add_argument("--once", action="store_true", help="drain the queue, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()
