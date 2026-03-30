#!/usr/bin/env python3
"""
探测 alphaxiv.org 论文页面使用的内部 API 端点。

策略：
1. 抓取论文页 HTML，找所有 <script src="/_next/..."> 标签
2. 下载与 /abs/ 路由相关的 JS chunk
3. 在 JS 中搜索 API 调用模式（fetch、/api/、supabase、graphql 等）
4. 同时暴力尝试常见 REST 路径

用法:
    uv run python scripts/find_alphaxiv_api.py 2602.06039
"""

import asyncio
import re
import sys

import httpx
from rich.console import Console

console = Console()

ARXIV_ID = sys.argv[1] if len(sys.argv) > 1 else "2602.06039"
PAGE_URL = f"https://alphaxiv.org/abs/{ARXIV_ID}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# API 路径模板
CANDIDATE_PATHS = [
    "/api/paper/{id}",
    "/api/papers/{id}",
    "/api/post/{id}",
    "/api/posts/{id}",
    "/api/discussion/{id}",
    "/api/arxiv/{id}",
    "/api/v1/paper/{id}",
    "/api/v1/papers/{id}",
    "/api/thread/{id}",
]

# JS 中搜索 API 模式的正则
API_PATTERNS = [
    (r"/api/[a-zA-Z0-9_/-]+", "API 路径"),
    (r'fetch\(["`\']([^`\'"\)]+)[`\'"]', "fetch 调用"),
    (r'"([^"]*arxiv[^"]*)"', "含 arxiv 字符串"),
    (r'likes?["\s]*:', "likes 字段"),
    (r'upvote["\s]*:', "upvote 字段"),
    (r"supabase", "Supabase"),
    (r"graphql", "GraphQL"),
    (r'NEXT_PUBLIC_[A-Z_]+\s*=\s*["\']([^"\']+)["\']', "环境变量"),
]


async def probe(arxiv_id: str):
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # ── 1. 抓 HTML ──────────────────────────────────────────────
        console.rule("[bold]步骤 1：抓取 HTML")
        resp = await client.get(PAGE_URL, timeout=20)
        console.print(f"HTTP {resp.status_code}，HTML 长度 {len(resp.text)} 字符")
        html = resp.text

        # 找所有 _next/static script 标签
        scripts = re.findall(r'<script[^>]+src="(/_next/static/[^"]+)"', html)
        console.print(f"找到 {len(scripts)} 个 Next.js JS chunk")

        # ── 2. 找与 abs 路由相关的 chunk ─────────────────────────────
        console.rule("[bold]步骤 2：搜索相关 JS chunk")

        # 优先找 pages/abs 或 app/abs，其次是 main/app chunk
        priority = []
        for s in scripts:
            lower = s.lower()
            if "abs" in lower or "paper" in lower or "discussion" in lower:
                priority.append(("🎯 路由相关", s))
        for s in scripts:
            if s not in [x[1] for x in priority]:
                lower = s.lower()
                if "main" in lower or "app" in lower or "layout" in lower:
                    priority.append(("📦 框架层", s))

        console.print(f"优先分析 {len(priority)} 个 chunk（共 {len(scripts)} 个）")
        found_apis = set()

        for label, src in priority[:6]:  # 最多检查 6 个
            url = f"https://alphaxiv.org{src}"
            try:
                r = await client.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                js = r.text
                console.print(
                    f"\n[cyan]{label}[/cyan] {src[-60:]}  ({len(js) // 1024}KB)"
                )

                for pattern, desc in API_PATTERNS:
                    matches = re.findall(pattern, js, re.IGNORECASE)
                    unique = set(matches)
                    if unique:
                        # 过滤太短或太通用的
                        interesting = [
                            m for m in unique if len(m) > 4 and "node_modules" not in m
                        ]
                        if interesting:
                            console.print(f"  [{desc}] {list(interesting)[:8]}")
                            if desc == "API 路径":
                                found_apis.update(interesting)
            except Exception as e:
                console.print(f"  [red]下载失败: {e}[/red]")

        # ── 3. 暴力尝试常见 REST 路径 ──────────────────────────────
        console.rule("[bold]步骤 3：暴力尝试 REST 路径")
        for path in CANDIDATE_PATHS:
            url = "https://alphaxiv.org" + path.format(id=arxiv_id)
            try:
                r = await client.get(url, timeout=8)
                marker = "✓" if r.status_code == 200 else f"HTTP {r.status_code}"
                if r.status_code == 200:
                    console.print(f"  [green bold]{marker}  {url}[/green bold]")
                    console.print(f"  响应: {r.text[:500]}")
                else:
                    console.print(f"  [dim]{marker}  {url}[/dim]")
            except Exception as e:
                console.print(f"  [red]ERR  {url}  ({e})[/red]")

        # ── 4. 汇总发现的 API 路径 ─────────────────────────────────
        if found_apis:
            console.rule("[bold green]发现的 API 路径")
            for api in sorted(found_apis):
                console.print(f"  {api}")
        else:
            console.rule("[yellow]未找到明显 API 路径，可能使用 GraphQL 或 Supabase")


if __name__ == "__main__":
    asyncio.run(probe(ARXIV_ID))
