"""Citation lookup helpers backed by Semantic Scholar."""

from __future__ import annotations

import re

import httpx

_ARXIV_ID_RE = re.compile(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?")
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = "citationCount,influentialCitationCount,title,year"
S2_BATCH_SIZE = 500


def extract_arxiv_id(text: str) -> str | None:
    """Extract a normalized arXiv ID from text or URL."""
    m = _ARXIV_ID_RE.search(text)
    if not m:
        return None
    return re.sub(r"v\d+$", "", m.group(0))


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

    for i in range(0, len(arxiv_ids), batch_size):
        batch = arxiv_ids[i : i + batch_size]
        try:
            resp = await client.post(
                S2_BATCH_URL,
                params={"fields": fields},
                json={"ids": [f"arXiv:{aid}" for aid in batch]},
                timeout=60,
            )
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
