#!/usr/bin/env python3
"""
从论文推荐列表中提取 arxiv ID，同时查询两个数据源，筛选高关注度论文：

  - Semantic Scholar：citationCount / influentialCitationCount（学术引用）
  - Hugging Face Papers：upvotes（ML 从业者社区关注度）

两个数据源均免费、无需认证、支持 arxiv ID 直查。
输入文件和输出结果统一放在 scripts/data/ 目录（已加入 .gitignore）。

用法:
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --min-upvotes 1 --top 30
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --sort citations
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

import httpx
from rich.console import Console
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

# Hugging Face Papers API
HF_PAPER_URL = "https://huggingface.co/api/papers/{arxiv_id}"


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

    async with httpx.AsyncClient() as client:
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
                "citations": (paper.get("citationCount") or 0) if paper else 0,
                "influential": (paper.get("influentialCitationCount") or 0)
                if paper
                else 0,
                "hf_upvotes": hf_results.get(arxiv_id, 0),
            }
        )

    # 排序
    sort_key = args.sort
    if sort_key == "upvotes":
        enriched.sort(key=lambda x: (x["hf_upvotes"], x["influential"]), reverse=True)
    else:  # citations（默认）
        enriched.sort(key=lambda x: (x["influential"], x["citations"]), reverse=True)

    # 过滤
    filtered = [
        r
        for r in enriched
        if r["citations"] >= args.min_cites or r["hf_upvotes"] >= args.min_upvotes
    ]
    top = filtered[: args.top]

    hf_count = sum(1 for r in enriched if r["hf_upvotes"] > 0)

    # 显示 HF 错误统计
    if hf_errors:
        parts = ", ".join(f"{k}: {v}" for k, v in hf_errors.most_common())
        console.print(f"[dim]HF 查询统计：{parts}[/dim]")

    # 展示表格
    sort_label = "HF Upvotes" if sort_key == "upvotes" else "Inf.Cite"
    table = Table(
        title=f"Top {len(top)} 论文（排序：{sort_label}）",
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("HF♥", style="bold yellow", width=6, justify="right")
    table.add_column("Inf.Cite", style="bold magenta", width=9, justify="right")
    table.add_column("Cite", style="bold green", width=6, justify="right")
    table.add_column("Year", style="dim", width=6)
    table.add_column("ArXiv ID", style="cyan", width=14)
    table.add_column("Title")

    for i, paper in enumerate(top, 1):
        title = paper["title"]
        if len(title) > 75:
            title = title[:72] + "..."
        hf = str(paper["hf_upvotes"]) if paper["hf_upvotes"] else "[dim]-[/dim]"
        table.add_row(
            str(i),
            hf,
            str(paper["influential"]),
            str(paper["citations"]),
            str(paper["year"]),
            paper["arxiv_id"],
            title,
        )

    console.print(table)
    console.print(
        f"\n[dim]S2 找到 {len(enriched) - not_found_s2} 篇（{not_found_s2} 篇未收录）"
        f"；HF Papers 收录 {hf_count} 篇[/dim]"
    )
    console.print(
        "[dim]HF♥ = Hugging Face 社区 upvotes；Inf.Cite = S2 高影响力引用数[/dim]"
    )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "rank",
                    "hf_upvotes",
                    "influential_citations",
                    "citations",
                    "year",
                    "arxiv_id",
                    "title",
                    "arxiv_url",
                    "hf_url",
                ],
            )
            writer.writeheader()
            for i, paper in enumerate(filtered, 1):
                writer.writerow(
                    {
                        "rank": i,
                        "hf_upvotes": paper["hf_upvotes"],
                        "influential_citations": paper["influential"],
                        "citations": paper["citations"],
                        "year": paper["year"],
                        "arxiv_id": paper["arxiv_id"],
                        "title": paper["title"],
                        "arxiv_url": f"https://arxiv.org/abs/{paper['arxiv_id']}",
                        "hf_url": f"https://huggingface.co/papers/{paper['arxiv_id']}",
                    }
                )
        console.print(f"[green]已保存至 {args.output}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="从论文列表提取 arxiv ID 并按 Semantic Scholar 引用量排序"
    )
    parser.add_argument("input", help="包含论文链接的 markdown/文本文件路径")
    parser.add_argument(
        "--min-cites",
        type=int,
        default=0,
        metavar="N",
        help="最低引用数过滤（默认 0）",
    )
    parser.add_argument(
        "--min-upvotes",
        type=int,
        default=0,
        metavar="N",
        help="最低 HF upvotes 过滤（默认 0）",
    )
    parser.add_argument(
        "--sort",
        choices=["citations", "upvotes"],
        default="upvotes",
        help="排序依据：citations（S2 高影响力引用）或 upvotes（HF 社区，默认）",
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
