#!/usr/bin/env python3
"""
从论文推荐列表中提取 arxiv ID，查询 alphaxiv 点赞量，筛选高价值论文。

输入文件和输出结果统一放在 scripts/data/ 目录（已加入 .gitignore）。

用法:
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --min-likes 5 --top 30
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --output scripts/data/out.csv
    uv run python scripts/rank_by_likes.py scripts/data/my_list.md --debug
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional

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

# Next.js SSR 内嵌数据
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

# 从 markdown 粗体链接提取标题: **[Title](url)**
TITLE_RE = re.compile(r"\*\*\[([^\]]+)\]")

ALPHAXIV_BASE = "https://alphaxiv.org/abs"
ALPHAXIV_API_BASE = "https://api.alphaxiv.org"

# alphaxiv JSON 中点赞数的候选字段名
LIKES_KEYS = ("likes", "likeCount", "like_count", "upvotes", "voteCount", "vote_count")

# 尝试的 REST API 路径（按优先级）
API_PATHS = [
    "/papers/{id}",
    "/v1/papers/{id}",
    "/api/papers/{id}",
    "/v2/papers/{id}",
]


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


def _find_likes_in_obj(obj, depth=0) -> Optional[int]:
    """递归在 JSON 对象中查找点赞数字段（最多 5 层）。"""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        for key in LIKES_KEYS:
            if key in obj:
                val = obj[key]
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and val.isdigit():
                    return int(val)
        for v in obj.values():
            result = _find_likes_in_obj(v, depth + 1)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj[:5]:  # 只检查前几个元素
            result = _find_likes_in_obj(item, depth + 1)
            if result is not None:
                return result
    return None


async def _try_rest_api(
    client: httpx.AsyncClient, arxiv_id: str, verbose: bool
) -> tuple[Optional[int], str]:
    """尝试直接调用 alphaxiv REST API，返回 (likes, error_reason)。"""
    for path in API_PATHS:
        url = ALPHAXIV_API_BASE + path.format(id=arxiv_id)
        try:
            resp = await client.get(url, timeout=10)
            if verbose:
                console.print(f"  REST {url} → HTTP {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if verbose:
                        console.print(
                            f"  响应: {json.dumps(data, ensure_ascii=False)[:1000]}"
                        )
                    result = _find_likes_in_obj(data)
                    if result is not None:
                        return result, ""
                except Exception as e:
                    if verbose:
                        console.print(f"  JSON 解析失败: {e}")
        except httpx.TimeoutException:
            if verbose:
                console.print(f"  REST {url} → 超时")
        except Exception as e:
            if verbose:
                console.print(f"  REST {url} → {type(e).__name__}: {e}")
    return None, "rest_api_miss"


async def _try_html_scrape(
    client: httpx.AsyncClient, arxiv_id: str, verbose: bool
) -> tuple[Optional[int], str]:
    """爬取 alphaxiv 页面，从 __NEXT_DATA__ 或 HTML 提取点赞数。"""
    url = f"{ALPHAXIV_BASE}/{arxiv_id}"
    try:
        resp = await client.get(url, follow_redirects=True, timeout=20)
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as e:
        return None, f"conn_error:{type(e).__name__}"

    if verbose:
        console.print(f"  HTML {url} → HTTP {resp.status_code}")

    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"

    html = resp.text

    # 优先从 Next.js __NEXT_DATA__ 中解析
    next_match = NEXT_DATA_RE.search(html)
    if next_match:
        try:
            data = json.loads(next_match.group(1))
            if verbose:
                page_props = data.get("props", {}).get("pageProps", {})
                console.print(
                    f"  __NEXT_DATA__ pageProps keys: {list(page_props.keys())}"
                )
                console.print(
                    f"  __NEXT_DATA__ (前 2000 字符):\n"
                    f"  {json.dumps(data, ensure_ascii=False)[:2000]}"
                )
            result = _find_likes_in_obj(data)
            if result is not None:
                return result, ""
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            if verbose:
                console.print(f"  __NEXT_DATA__ 解析失败: {e}")

    # 备用：HTML 正则
    for pattern in [
        r'"likes"\s*:\s*(\d+)',
        r'"likeCount"\s*:\s*(\d+)',
        r'"like_count"\s*:\s*(\d+)',
        r'"upvotes"\s*:\s*(\d+)',
        r'data-likes="(\d+)"',
        r'aria-label="(\d+)\s*like',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return int(m.group(1)), ""

    if verbose:
        console.print(f"  页面前 2000 字符:\n  {html[:2000]}")

    return None, "no_likes_field"


async def fetch_likes(
    client: httpx.AsyncClient,
    arxiv_id: str,
    verbose: bool = False,
    delay: float = 0.0,
) -> tuple[Optional[int], str]:
    """返回 (likes, error_reason)，error_reason 为空字符串表示成功。"""
    if delay:
        await asyncio.sleep(delay)

    likes, reason = await _try_rest_api(client, arxiv_id, verbose)
    if likes is not None:
        return likes, ""

    likes, reason = await _try_html_scrape(client, arxiv_id, verbose)
    return likes, reason


async def run(args):
    text = Path(args.input).read_text(encoding="utf-8")
    papers = extract_papers(text)
    console.print(f"[bold]提取到 {len(papers)} 篇唯一论文[/bold]")

    if not papers:
        console.print("[red]未找到 arxiv ID，请检查输入文件格式[/red]")
        sys.exit(1)

    # --test 模式：只跑前 N 篇，全程 verbose，帮助诊断
    if args.test:
        test_papers = papers[: args.test]
        console.print(
            f"[yellow]--test 模式：只处理前 {len(test_papers)} 篇，输出详细日志[/yellow]"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(headers=headers) as client:
            for p in test_papers:
                console.rule(f"[cyan]{p['arxiv_id']}[/cyan]")
                likes, reason = await fetch_likes(client, p["arxiv_id"], verbose=True)
                if likes is not None:
                    console.print(f"[green]✓ likes = {likes}[/green]")
                else:
                    console.print(f"[red]✗ 失败原因: {reason}[/red]")
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    error_counter: Counter = Counter()

    async def fetch_one(client, paper):
        async with semaphore:
            likes, reason = await fetch_likes(
                client,
                paper["arxiv_id"],
                verbose=False,
                delay=args.delay,
            )
            if likes is None:
                error_counter[reason or "unknown"] += 1
            return {**paper, "likes": likes}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    results = []
    async with httpx.AsyncClient(headers=headers) as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("查询 alphaxiv...", total=len(papers))
            coros = [fetch_one(client, p) for p in papers]
            for coro in asyncio.as_completed(coros):
                result = await coro
                results.append(result)
                progress.advance(task_id)

    # 分组：有数据 / 无数据
    with_likes = [r for r in results if r["likes"] is not None]
    no_data = [r for r in results if r["likes"] is None]

    # 过滤并排序
    filtered = [r for r in with_likes if r["likes"] >= args.min_likes]
    filtered.sort(key=lambda x: x["likes"], reverse=True)
    top = filtered[: args.top]

    # 展示表格
    table = Table(
        title=f"Top {len(top)} 论文（按 alphaxiv 点赞排序，最低 {args.min_likes} 赞）",
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Likes", style="bold green", width=7, justify="right")
    table.add_column("ArXiv ID", style="cyan", width=14)
    table.add_column("Title")

    for i, paper in enumerate(top, 1):
        title = paper["title"]
        if len(title) > 90:
            title = title[:87] + "..."
        table.add_row(str(i), str(paper["likes"]), paper["arxiv_id"], title)

    console.print(table)
    console.print(
        f"\n[dim]共 {len(with_likes)} 篇有点赞数据"
        f"（其中 {len(filtered)} 篇 >= {args.min_likes} 赞）"
        f"，{len(no_data)} 篇无数据[/dim]"
    )

    # 如果全部失败，显示错误分布帮助诊断
    if no_data and not with_likes:
        console.print("[bold red]所有请求均失败，错误分布：[/bold red]")
        for reason, count in error_counter.most_common():
            console.print(f"  {reason}: {count} 篇")
        console.print(
            "[yellow]提示：运行 --test 3 查看详细请求日志，诊断具体原因[/yellow]"
        )

    if args.output and with_likes:
        all_sorted = sorted(with_likes, key=lambda x: x["likes"], reverse=True)
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["rank", "likes", "arxiv_id", "title", "url"]
            )
            writer.writeheader()
            for i, paper in enumerate(all_sorted, 1):
                writer.writerow(
                    {
                        "rank": i,
                        "likes": paper["likes"],
                        "arxiv_id": paper["arxiv_id"],
                        "title": paper["title"],
                        "url": f"https://alphaxiv.org/abs/{paper['arxiv_id']}",
                    }
                )
        console.print(f"[green]已保存至 {args.output}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="从论文列表提取 arxiv ID 并按 alphaxiv 点赞量排序"
    )
    parser.add_argument("input", help="包含论文链接的 markdown/文本文件路径")
    parser.add_argument(
        "--min-likes", type=int, default=0, metavar="N", help="最低点赞数过滤（默认 0）"
    )
    parser.add_argument(
        "--top", type=int, default=50, metavar="N", help="显示前 N 篇（默认 50）"
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, metavar="N", help="并发请求数（默认 8）"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        metavar="SEC",
        help="每请求间隔秒数（默认 0.1）",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="结果保存为 CSV 文件（默认：<输入文件名>_likes.csv）",
    )
    parser.add_argument(
        "--test",
        type=int,
        metavar="N",
        default=0,
        help="诊断模式：只处理前 N 篇并打印详细请求日志（如 --test 3）",
    )
    args = parser.parse_args()
    if args.output is None:
        stem = Path(args.input).stem.lstrip(".")
        args.output = str(Path(args.input).parent / f"{stem}_likes.csv")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
