"""Citation lookup helpers backed by Semantic Scholar."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

_ARXIV_ID_RE = re.compile(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?")
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = "citationCount,influentialCitationCount,title,year"
S2_BATCH_SIZE = 500

_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY = 2.0
_RETRY_MAX_DELAY = 60.0
_BATCH_INTERVAL = 1.0

log = logging.getLogger(__name__)


def extract_arxiv_id(text: str) -> str | None:
    """Extract a normalized arXiv ID from text or URL."""
    m = _ARXIV_ID_RE.search(text)
    if not m:
        return None
    return re.sub(r"v\d+$", "", m.group(0))


def _parse_retry_after(resp: httpx.Response) -> float:
    header = resp.headers.get("Retry-After", "")
    if not header:
        return 0.0
    try:
        return float(header)
    except ValueError:
        return 0.0


async def _post_batch(
    client: httpx.AsyncClient,
    batch: list[str],
    fields: str,
) -> httpx.Response:
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        resp = await client.post(
            S2_BATCH_URL,
            params={"fields": fields},
            json={"ids": [f"arXiv:{aid}" for aid in batch]},
            timeout=60,
        )
        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            delay = (
                retry_after
                if retry_after > 0
                else min(_RETRY_BASE_DELAY * (2**attempt), _RETRY_MAX_DELAY)
            )
            if attempt < _RETRY_MAX_ATTEMPTS - 1:
                log.warning(
                    "Semantic Scholar 429 (attempt %d/%d), sleeping %.1fs",
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
        elif resp.status_code >= 500:
            if attempt < _RETRY_MAX_ATTEMPTS - 1:
                delay = min(_RETRY_BASE_DELAY * (2**attempt), _RETRY_MAX_DELAY)
                log.warning(
                    "Semantic Scholar %d (attempt %d/%d), retrying in %.1fs",
                    resp.status_code,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
        return resp
    return resp


async def query_semantic_scholar_batch(
    client: httpx.AsyncClient,
    arxiv_ids: list[str],
    *,
    fields: str = S2_FIELDS,
    batch_size: int = S2_BATCH_SIZE,
    raise_on_error: bool = True,
) -> dict[str, dict | None]:
    """Batch query Semantic Scholar using arXiv IDs."""
    results: dict[str, dict | None] = {}

    for idx, i in enumerate(range(0, len(arxiv_ids), batch_size)):
        if idx > 0:
            await asyncio.sleep(_BATCH_INTERVAL)

        batch = arxiv_ids[i : i + batch_size]
        try:
            resp = await _post_batch(client, batch, fields)
            if resp.status_code != 200:
                if raise_on_error:
                    raise RuntimeError(
                        f"Semantic Scholar HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                for aid in batch:
                    results[aid] = None
                continue

            payload = resp.json()
            for arxiv_id, paper in zip(batch, payload):
                results[arxiv_id] = paper
        except Exception:
            if raise_on_error:
                raise
            for aid in batch:
                results[aid] = None

    return results
