#!/usr/bin/env python3
"""
从论文推荐列表中提取 arxiv ID，通过 Semantic Scholar 批量查询引用数，筛选高关注度论文。

数据来源：Semantic Scholar API（免费、无需认证、支持 arxiv ID 批量查询）
排序指标：influentialCitationCount（高影响力引用数）+ citationCount（总引用数）

输入文件和输出结果统一放在 scripts/data/ 目录（已加入 .gitignore）。

用法:
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --min-cites 1 --top 30
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --output scripts/data/out.csv
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

    # 合并结果
    enriched = []
    not_found = 0
    for arxiv_id, paper in s2_results.items():
        if paper is None:
            not_found += 1
            continue
        enriched.append(
            {
                "arxiv_id": arxiv_id,
                "title": title_map.get(arxiv_id, "") or paper.get("title", ""),
                "year": paper.get("year") or "",
                "citations": paper.get("citationCount") or 0,
                "influential": paper.get("influentialCitationCount") or 0,
            }
        )

    # 过滤并排序：先按 influential 降序，再按 citations 降序
    filtered = [r for r in enriched if r["citations"] >= args.min_cites]
    filtered.sort(key=lambda x: (x["influential"], x["citations"]), reverse=True)
    top = filtered[: args.top]

    # 展示表格
    table = Table(
        title=f"Top {len(top)} 论文（按引用量排序，最低 {args.min_cites} 次引用）",
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Inf.Cite", style="bold magenta", width=9, justify="right")
    table.add_column("Cite", style="bold green", width=6, justify="right")
    table.add_column("Year", style="dim", width=6)
    table.add_column("ArXiv ID", style="cyan", width=14)
    table.add_column("Title")

    for i, paper in enumerate(top, 1):
        title = paper["title"]
        if len(title) > 85:
            title = title[:82] + "..."
        table.add_row(
            str(i),
            str(paper["influential"]),
            str(paper["citations"]),
            str(paper["year"]),
            paper["arxiv_id"],
            title,
        )

    console.print(table)
    console.print(
        f"\n[dim]找到 {len(enriched)} 篇（{not_found} 篇 S2 未收录），"
        f"其中 {len(filtered)} 篇引用数 >= {args.min_cites}[/dim]"
    )

    if top:
        console.print(
            "[dim]Inf.Cite = 高影响力引用数（更有区分度）；Cite = 总引用数[/dim]"
        )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "rank",
                    "influential_citations",
                    "citations",
                    "year",
                    "arxiv_id",
                    "title",
                    "arxiv_url",
                    "s2_url",
                ],
            )
            writer.writeheader()
            for i, paper in enumerate(filtered, 1):
                writer.writerow(
                    {
                        "rank": i,
                        "influential_citations": paper["influential"],
                        "citations": paper["citations"],
                        "year": paper["year"],
                        "arxiv_id": paper["arxiv_id"],
                        "title": paper["title"],
                        "arxiv_url": f"https://arxiv.org/abs/{paper['arxiv_id']}",
                        "s2_url": f"https://www.semanticscholar.org/paper/arXiv:{paper['arxiv_id']}",
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
        help="最低引用数过滤（默认 0，显示所有有数据的论文）",
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
