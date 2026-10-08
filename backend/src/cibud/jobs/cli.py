"""``cibud-worker``: run background jobs (extraction, metadata resolution, imports)."""

import argparse
import asyncio
import logging

import httpx

from cibud.api.deps import get_store
from cibud.db.session import session_factory
from cibud.imports.service import ENRICH_IMPORT, PdfFetcher, make_enrich_handler
from cibud.ingestion.grobid import GrobidClient
from cibud.ingestion.service import EXTRACT_PDF, make_extract_handler
from cibud.jobs.worker import Handler, Worker
from cibud.metadata.service import RESOLVE_METADATA, make_resolve_handler
from cibud.metadata.sources import Registries
from cibud.settings import get_settings


async def _run(once: bool) -> None:
    settings = get_settings()
    factory = session_factory()
    grobid = GrobidClient(settings.grobid_url, timeout=settings.grobid_timeout_seconds)
    user_agent = "CiBud/0.1" + (
        f" (mailto:{settings.contact_email})" if settings.contact_email else ""
    )
    http = httpx.AsyncClient(timeout=30, headers={"User-Agent": user_agent}, follow_redirects=True)
    registries = Registries(http, settings.contact_email)
    handlers: dict[str, Handler] = {
        EXTRACT_PDF: make_extract_handler(factory, get_store(), grobid),
        RESOLVE_METADATA: make_resolve_handler(factory, registries),
        ENRICH_IMPORT: make_enrich_handler(
            factory, registries, get_store(), PdfFetcher(http, settings.max_upload_mb * 1024 * 1024)
        ),
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
        await http.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cibud-worker")
    parser.add_argument("--once", action="store_true", help="drain the queue, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()
