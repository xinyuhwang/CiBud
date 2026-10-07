"""Client for a GROBID server (design doc §5, §7.1)."""

import httpx

# Request coordinates for sentences, headings, and bibliography entries so passages can be
# highlighted in the PDF viewer. Header consolidation stays off: metadata resolution against
# Crossref/OpenAlex is CiBud's own step, with its own provenance.
FULLTEXT_OPTIONS: dict[str, str | list[str]] = {
    "segmentSentences": "1",
    "consolidateHeader": "0",
    "consolidateCitations": "0",
    "teiCoordinates": ["s", "head", "biblStruct", "figure"],
}


class GrobidError(Exception):
    """GROBID could not process the document."""


class GrobidUnavailable(GrobidError):
    """Transient: server down or busy (HTTP 503). Worth retrying."""


class GrobidClient:
    def __init__(
        self, base_url: str, timeout: float = 180.0, client: httpx.AsyncClient | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def is_alive(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/api/isalive")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.text.strip() == "true"

    async def process_fulltext(self, pdf: bytes, filename: str = "paper.pdf") -> str:
        """Return TEI XML for a PDF."""
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/processFulltextDocument",
                files={"input": (filename, pdf, "application/pdf")},
                data=FULLTEXT_OPTIONS,
            )
        except httpx.HTTPError as exc:
            raise GrobidUnavailable(f"cannot reach GROBID at {self.base_url}: {exc}") from exc
        if resp.status_code == 503:
            raise GrobidUnavailable("GROBID is busy (503)")
        if resp.status_code == 204:
            raise GrobidError("GROBID found no content in the PDF")
        if resp.status_code != 200:
            raise GrobidError(f"GROBID returned {resp.status_code}: {resp.text[:200]}")
        return resp.text

    async def aclose(self) -> None:
        await self._client.aclose()
