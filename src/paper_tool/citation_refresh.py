"""Periodic citation refresh for the configured Notion database."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx

from paper_tool.citations import extract_arxiv_id, query_semantic_scholar_batch
from paper_tool.config import get_config
from paper_tool.notion_service import NotionService
from paper_tool.notion_setup import normalize_notion_id

_STATE_FILENAME = ".citation_refresh_state.json"


@dataclass(slots=True)
class CitationRefreshStats:
    total_pages: int = 0
    arxiv_pages: int = 0
    updated_pages: int = 0
    no_data_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0


def _state_path() -> Path:
    return get_config().papers_dir / _STATE_FILENAME


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"databases": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("databases", {})
            return data
    except Exception:
        pass
    return {"databases": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _db_state_key() -> str:
    return normalize_notion_id(get_config().notion_database_id)


def _last_refresh_at(state: dict) -> datetime | None:
    raw = (
        state.get("databases", {})
        .get(_db_state_key(), {})
        .get("last_citation_refresh_at", "")
    )
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _mark_refresh_success(state: dict, now: datetime) -> dict:
    databases = state.setdefault("databases", {})
    databases[_db_state_key()] = {
        "last_citation_refresh_at": now.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    }
    return state


def _is_refresh_due(now: datetime, interval_days: int, state: dict) -> bool:
    last = _last_refresh_at(state)
    if last is None:
        return True
    return now >= last + timedelta(days=interval_days)


def _stats_label(stats: CitationRefreshStats) -> str:
    return (
        "引用量刷新完成: "
        f"总页数 {stats.total_pages}，"
        f"可解析 {stats.arxiv_pages}，"
        f"更新 {stats.updated_pages}，"
        f"无数据 {stats.no_data_pages}，"
        f"跳过 {stats.skipped_pages}，"
        f"失败 {stats.failed_pages}"
    )


def refresh_all_citations() -> CitationRefreshStats:
    notion = NotionService()
    pages = notion.list_database_pages()

    stats = CitationRefreshStats(total_pages=len(pages))
    arxiv_to_pages: dict[str, list[str]] = {}

    for page in pages:
        page_id = page.get("id")
        if not isinstance(page_id, str):
            stats.skipped_pages += 1
            continue

        paper_url = notion.get_page_source_url(page)
        if not paper_url:
            stats.skipped_pages += 1
            continue

        arxiv_id = extract_arxiv_id(paper_url)
        if not arxiv_id:
            stats.skipped_pages += 1
            continue

        arxiv_to_pages.setdefault(arxiv_id, []).append(page_id)
        stats.arxiv_pages += 1

    if not arxiv_to_pages:
        return stats

    async def _query() -> dict[str, dict | None]:
        async with httpx.AsyncClient(
            headers={"User-Agent": "paper-tool/0.1"}
        ) as client:
            return await query_semantic_scholar_batch(
                client,
                list(arxiv_to_pages.keys()),
                fields="citationCount",
            )

    results = asyncio.run(_query())

    for arxiv_id, page_ids in arxiv_to_pages.items():
        paper = results.get(arxiv_id)
        if paper is None:
            stats.no_data_pages += len(page_ids)
            continue

        citation_count = paper.get("citationCount")
        if citation_count is None:
            stats.no_data_pages += len(page_ids)
            continue

        for page_id in page_ids:
            try:
                notion.update_citation_count(page_id, int(citation_count))
                stats.updated_pages += 1
            except Exception:
                stats.failed_pages += 1

    return stats


def maybe_refresh_citations(
    *,
    force: bool = False,
    on_event: Callable[[dict], None] | None = None,
) -> bool:
    emit = on_event or (lambda _event: None)
    cfg = get_config()
    state_path = _state_path()
    state = _load_state(state_path)
    now = datetime.now(UTC)

    if not force:
        emit(
            {
                "type": "stage_start",
                "stage": "check_citations_refresh",
                "label": "检查引用量刷新间隔...",
            }
        )
        if not _is_refresh_due(now, cfg.citations_refresh_interval_days, state):
            emit(
                {
                    "type": "stage_done",
                    "stage": "check_citations_refresh",
                    "label": "未到刷新时间，跳过",
                    "status": "ok",
                }
            )
            return True
        emit(
            {
                "type": "stage_done",
                "stage": "check_citations_refresh",
                "label": "达到刷新时间，开始刷新",
                "status": "ok",
            }
        )

    emit(
        {
            "type": "stage_start",
            "stage": "refresh_citations",
            "label": "刷新数据库引用量...",
        }
    )
    try:
        stats = refresh_all_citations()
    except Exception as e:
        emit(
            {
                "type": "stage_done",
                "stage": "refresh_citations",
                "label": f"引用量刷新失败: {e}",
                "status": "warn",
            }
        )
        return False

    if stats.failed_pages > 0:
        emit(
            {
                "type": "stage_done",
                "stage": "refresh_citations",
                "label": _stats_label(stats),
                "status": "warn",
            }
        )
        return False

    _save_state(state_path, _mark_refresh_success(state, now))
    emit(
        {
            "type": "stage_done",
            "stage": "refresh_citations",
            "label": _stats_label(stats),
            "status": "ok",
        }
    )
    return True
