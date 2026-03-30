#!/usr/bin/env python3
"""
从论文推荐列表中提取 arxiv ID，同时查询三个数据源，筛选高关注度论文：

  - alphaXiv：likes（页面 JSON-LD interactionStatistic）
  - Hugging Face Papers：upvotes（ML 社区关注度）
  - Semantic Scholar：citationCount / influentialCitationCount（学术引用）

输入文件和输出结果统一放在 scripts/data/ 目录（已加入 .gitignore）。

用法:
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --min-likes 1 --top 30
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --sort hf
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --sort citations
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

console = Console()

# 匹配 arxiv 链接中的 ID，忽略版本号
ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)

# 从 markdown 粗体链接提取标题: **[Title](url)**
TITLE_RE = re.compile(r"\*\*\[([^\]]+)\]")

# Semantic Scholar 批量查询接口（一次最多 500 篇）
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = "citationCount,influentialCitationCount,title,year,externalIds"
S2_BATCH_SIZE = 500

# alphaXiv 页面（点赞数在 HTML JSON-LD 里）
ALPHAXIV_PAGE_URL = "https://alphaxiv.org/abs/{arxiv_id}"

# Hugging Face Papers API
HF_PAPER_URL = "https://huggingface.co/api/papers/{arxiv_id}"

# JSON-LD 匹配
JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_papers(text: str) -> list[dict]:
    """从 markdown/文本中提取唯一 arxiv ID 及标题。"""
    seen: dict[str, dict] = {}
    for line in text.splitlines():
        ids = ARXIV_ID_RE.findall(line)
        if not ids:
            continue
        title_match = TITLE_RE.search(line)
        title = title_match.group(1) if title_match else ""
        for arxiv_id in ids:
            if arxiv_id not in seen:
                seen[arxiv_id] = {"arxiv_id": arxiv_id, "title": title}
    return list(seen.values())


async def query_alphaxiv_likes(
    client: httpx.AsyncClient, arxiv_ids: list[str], concurrency: int = 8
) -> tuple[dict[str, int], Counter]:
    """
    从 alphaxiv 页面 JSON-LD 提取点赞数（LikeAction interactionStatistic）。
    返回 ({arxiv_id: likes}, error_counter)。
    """
    results: dict[str, int] = {}
    errors: Counter = Counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(arxiv_id: str):
        async with semaphore:
            url = ALPHAXIV_PAGE_URL.format(arxiv_id=arxiv_id)
            try:
                resp = await client.get(url, timeout=20, follow_redirects=True)
                if resp.status_code != 200:
                    errors[f"http_{resp.status_code}"] += 1
                    return
                m = JSONLD_RE.search(resp.text)
                if not m:
                    errors["no_jsonld"] += 1
                    return
                data = json.loads(m.group(1))
                for stat in data.get("interactionStatistic", []):
                    action = stat.get("interactionType", {}).get("@type", "")
                    if action == "LikeAction":
                        results[arxiv_id] = int(stat.get("userInteractionCount", 0))
                        return
                errors["no_likes_in_jsonld"] += 1
            except json.JSONDecodeError:
                errors["json_parse_error"] += 1
            except httpx.TimeoutException:
                errors["timeout"] += 1
            except Exception as e:
                errors[f"{type(e).__name__}"] += 1

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("查询 alphaXiv likes...", total=len(arxiv_ids))
        for coro in asyncio.as_completed([fetch_one(aid) for aid in arxiv_ids]):
            await coro
            progress.advance(task_id)

    return results, errors


async def query_s2_batch(
    client: httpx.AsyncClient, arxiv_ids: list[str]
) -> dict[str, dict]:
    """
    批量查询 Semantic Scholar，返回 {arxiv_id: paper_data} 映射。
    对找不到的论文返回 None 值。
    """
    results: dict[str, dict] = {}

    # 按批次拆分（每批最多 500）
    for i in range(0, len(arxiv_ids), S2_BATCH_SIZE):
        batch = arxiv_ids[i : i + S2_BATCH_SIZE]
        ids_payload = [f"arXiv:{aid}" for aid in batch]
        console.print(
            f"[dim]查询 Semantic Scholar 第 {i // S2_BATCH_SIZE + 1} 批"
            f"（{len(batch)} 篇）...[/dim]"
        )
        try:
            resp = await client.post(
                S2_BATCH_URL,
                params={"fields": S2_FIELDS},
                json={"ids": ids_payload},
                timeout=60,
            )
            if resp.status_code != 200:
                console.print(
                    f"[red]Semantic Scholar 返回 HTTP {resp.status_code}：{resp.text[:300]}[/red]"
                )
                # 把这批全部标记为 None
                for aid in batch:
                    results[aid] = None
                continue

            data = resp.json()
            for arxiv_id, paper in zip(batch, data):
                results[arxiv_id] = paper  # paper 可能是 None（S2 找不到该论文）

        except httpx.TimeoutException:
            console.print("[red]请求超时[/red]")
            for aid in batch:
                results[aid] = None
        except Exception as e:
            console.print(f"[red]请求异常：{e}[/red]")
            for aid in batch:
                results[aid] = None

    return results


async def query_hf_upvotes(
    client: httpx.AsyncClient, arxiv_ids: list[str], concurrency: int = 10
) -> tuple[dict[str, int], Counter]:
    """
    逐篇查询 HuggingFace Papers upvotes，带进度条。
    返回 ({arxiv_id: upvotes}, error_counter)。
    """
    results: dict[str, int] = {}
    errors: Counter = Counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(arxiv_id: str):
        async with semaphore:
            url = HF_PAPER_URL.format(arxiv_id=arxiv_id)
            try:
                resp = await client.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    results[arxiv_id] = data.get("upvotes", 0) or 0
                elif resp.status_code == 404:
                    errors["not_in_hf_papers"] += 1
                else:
                    errors[f"http_{resp.status_code}"] += 1
            except httpx.TimeoutException:
                errors["timeout"] += 1
            except Exception as e:
                errors[f"{type(e).__name__}"] += 1

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("查询 HF Papers...", total=len(arxiv_ids))
        coros = [fetch_one(aid) for aid in arxiv_ids]
        for coro in asyncio.as_completed(coros):
            await coro
            progress.advance(task_id)

    return results, errors


async def run(args):
    text = Path(args.input).read_text(encoding="utf-8")
    papers = extract_papers(text)
    console.print(f"[bold]提取到 {len(papers)} 篇唯一论文[/bold]")

    if not papers:
        console.print("[red]未找到 arxiv ID，请检查输入文件格式[/red]")
        sys.exit(1)

    arxiv_ids = [p["arxiv_id"] for p in papers]
    title_map = {p["arxiv_id"]: p["title"] for p in papers}

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 Chrome/120"}
    ) as client:
        ax_results, ax_errors = await query_alphaxiv_likes(client, arxiv_ids)
        s2_results = await query_s2_batch(client, arxiv_ids)
        hf_results, hf_errors = await query_hf_upvotes(client, arxiv_ids)

    # 合并结果
    enriched = []
    not_found_s2 = 0
    for arxiv_id in arxiv_ids:
        paper = s2_results.get(arxiv_id)
        if paper is None:
            not_found_s2 += 1
        enriched.append(
            {
                "arxiv_id": arxiv_id,
                "title": title_map.get(arxiv_id, "")
                or (paper.get("title", "") if paper else ""),
                "year": (paper.get("year") or "") if paper else "",
                "ax_likes": ax_results.get(arxiv_id, 0),
                "citations": (paper.get("citationCount") or 0) if paper else 0,
                "influential": (paper.get("influentialCitationCount") or 0)
                if paper
                else 0,
                "hf_upvotes": hf_results.get(arxiv_id, 0),
            }
        )

    # 排序
    sort_key = args.sort
    if sort_key == "hf":
        enriched.sort(key=lambda x: (x["hf_upvotes"], x["ax_likes"]), reverse=True)
    elif sort_key == "citations":
        enriched.sort(key=lambda x: (x["influential"], x["citations"]), reverse=True)
    else:  # alphaxiv（默认）
        enriched.sort(key=lambda x: (x["ax_likes"], x["hf_upvotes"]), reverse=True)

    # 过滤
    filtered = [
        r
        for r in enriched
        if r["ax_likes"] >= args.min_likes
        or r["citations"] >= args.min_cites
        or r["hf_upvotes"] >= args.min_upvotes
    ]
    top = filtered[: args.top]

    ax_count = sum(1 for r in enriched if r["ax_likes"] > 0)
    hf_count = sum(1 for r in enriched if r["hf_upvotes"] > 0)

    # 错误统计
    if ax_errors:
        parts = ", ".join(f"{k}: {v}" for k, v in ax_errors.most_common(3))
        console.print(
            f"[dim]alphaXiv 查询：{ax_count} 篇有数据，失败分布：{parts}[/dim]"
        )
    # 显示 HF 错误统计
    if hf_errors:
        parts = ", ".join(f"{k}: {v}" for k, v in hf_errors.most_common())
        console.print(f"[dim]HF 查询统计：{parts}[/dim]")

    # 展示表格
    sort_labels = {
        "alphaxiv": "alphaXiv Likes",
        "hf": "HF Upvotes",
        "citations": "Inf.Cite",
    }
    table = Table(
        title=f"Top {len(top)} 论文（排序：{sort_labels.get(sort_key, sort_key)}）",
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("αLikes", style="bold red", width=8, justify="right")
    table.add_column("HF♥", style="bold yellow", width=6, justify="right")
    table.add_column("Inf.Cite", style="bold magenta", width=9, justify="right")
    table.add_column("Cite", style="bold green", width=6, justify="right")
    table.add_column("Year", style="dim", width=6)
    table.add_column("ArXiv ID", style="cyan", width=14)
    table.add_column("Title")

    def fmt(v):
        return str(v) if v else "[dim]-[/dim]"

    for i, paper in enumerate(top, 1):
        title = paper["title"]
        if len(title) > 65:
            title = title[:62] + "..."
        table.add_row(
            str(i),
            fmt(paper["ax_likes"]),
            fmt(paper["hf_upvotes"]),
            str(paper["influential"]),
            str(paper["citations"]),
            str(paper["year"]),
            paper["arxiv_id"],
            title,
        )

    console.print(table)
    console.print(
        f"\n[dim]alphaXiv {ax_count} 篇有点赞；HF Papers {hf_count} 篇有 upvotes；"
        f"S2 {len(enriched) - not_found_s2} 篇有引用数据[/dim]"
    )
    console.print(
        "[dim]αLikes = alphaXiv 点赞；HF♥ = HF 社区 upvotes；Inf.Cite = S2 高影响力引用数[/dim]"
    )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "rank",
                    "ax_likes",
                    "hf_upvotes",
                    "influential_citations",
                    "citations",
                    "year",
                    "arxiv_id",
                    "title",
                    "arxiv_url",
                    "alphaxiv_url",
                    "hf_url",
                ],
            )
            writer.writeheader()
            for i, paper in enumerate(filtered, 1):
                writer.writerow(
                    {
                        "rank": i,
                        "ax_likes": paper["ax_likes"],
                        "hf_upvotes": paper["hf_upvotes"],
                        "influential_citations": paper["influential"],
                        "citations": paper["citations"],
                        "year": paper["year"],
                        "arxiv_id": paper["arxiv_id"],
                        "title": paper["title"],
                        "arxiv_url": f"https://arxiv.org/abs/{paper['arxiv_id']}",
                        "alphaxiv_url": f"https://alphaxiv.org/abs/{paper['arxiv_id']}",
                        "hf_url": f"https://huggingface.co/papers/{paper['arxiv_id']}",
                    }
                )
        console.print(f"[green]已保存至 {args.output}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="从论文列表提取 arxiv ID，查询 alphaXiv/HF/S2 三源数据并排序"
    )
    parser.add_argument("input", help="包含论文链接的 markdown/文本文件路径")
    parser.add_argument(
        "--min-likes",
        type=int,
        default=0,
        metavar="N",
        help="最低 alphaXiv 点赞数（默认 0）",
    )
    parser.add_argument(
        "--min-cites", type=int, default=0, metavar="N", help="最低引用数过滤（默认 0）"
    )
    parser.add_argument(
        "--min-upvotes",
        type=int,
        default=0,
        metavar="N",
        help="最低 HF upvotes（默认 0）",
    )
    parser.add_argument(
        "--sort",
        choices=["alphaxiv", "hf", "citations"],
        default="alphaxiv",
        help="排序依据：alphaxiv（默认）/ hf / citations",
    )
    parser.add_argument(
        "--top", type=int, default=50, metavar="N", help="显示前 N 篇（默认 50）"
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="结果保存为 CSV 文件（默认：<输入文件名>_citations.csv）",
    )
    args = parser.parse_args()
    if args.output is None:
        stem = Path(args.input).stem.lstrip(".")
        args.output = str(Path(args.input).parent / f"{stem}_citations.csv")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
