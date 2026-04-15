"""paper-tool CLI - main entry point."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s,\"'\]>]+")
_SUPPORTED_DOMAINS = frozenset(
    [
        "arxiv.org",
        "alphaxiv.org",
        "ar5iv.labs.google.com",
        "openreview.net",
        "huggingface.co",
    ]
)


def _extract_urls(text: str) -> list[str]:
    """从任意文本中提取去重后的支持域名论文链接。"""

    def _is_supported(url: str) -> bool:
        lower = url.lower()
        return any(d in lower for d in _SUPPORTED_DOMAINS)

    def _normalize(url: str) -> str:
        return url.replace("alphaxiv.org", "arxiv.org").rstrip("/")

    seen: set[str] = set()
    result: list[str] = []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(".,;)>")
        if not _is_supported(url):
            continue
        key = _normalize(url)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


app = typer.Typer(
    name="paper-tool",
    help="Automated paper reading tool for Arxiv and OpenReview.",
    add_completion=False,
    rich_markup_mode="rich",
)

config_app = typer.Typer(help="Manage configuration.")
app.add_typer(config_app, name="config")

console = Console()
error_console = Console(stderr=True, style="bold red")


@app.callback()
def _app_init() -> None:
    from paper_tool.logging_setup import setup_logging

    setup_logging()


def _process_paper(
    url: str,
    skip_llm: bool = False,
    debug: bool = False,
    stream: bool = False,
    force: bool = False,
) -> bool:
    """
    Rich CLI adapter: drives run_pipeline() and renders progress + LLM stream
    in the terminal.  Returns True on success, False on failure.
    """

    from paper_tool.llm_stream import StreamWindow
    from paper_tool.pipeline import run_pipeline

    # ── Rich progress bar ──────────────────────────────────────────────────
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
    progress.start()

    _current_task: list[int] = []
    _stream_win: list[StreamWindow | None] = [None]
    _result: dict = {}

    def on_event(event: dict) -> None:
        t = event["type"]

        if t == "stage_start":
            task_id = progress.add_task(f"[cyan]{event['label']}", total=None)
            _current_task.clear()
            _current_task.append(task_id)

        elif t == "stage_done":
            if _current_task:
                status = event.get("status", "ok")
                icon = "✓" if status == "ok" else "⚠"
                color = "green" if status == "ok" else "yellow"
                progress.update(
                    _current_task[-1],
                    description=f"[{color}]{icon} {event['label']}",
                    total=1,
                    completed=1,
                )

        elif t == "llm_start":
            if stream:
                win = StreamWindow(event.get("title", "LLM 输出"), height=8)
                win.__enter__()
                _stream_win[0] = win

        elif t == "llm_token":
            if _stream_win[0] is not None:
                _stream_win[0].append(event["text"])

        elif t == "llm_end":
            if _stream_win[0] is not None:
                _stream_win[0].__exit__(None, None, None)
                _stream_win[0] = None

        elif t == "error":
            progress.stop()
            error_console.print(f"\n[ERROR] {event['message']}")

        elif t == "done":
            _result.update(event)
            progress.stop()

    def on_confirm_force(msg: str) -> bool:
        progress.stop()
        result = typer.confirm(msg, default=False)
        progress.start()
        return result

    def on_confirm_llm(summary: dict) -> bool:
        from rich.table import Table as RichTable

        tbl = RichTable(title="图表渲染结果", show_header=True, header_style="bold")
        tbl.add_column("类型", style="cyan")
        tbl.add_column("总数", justify="right")
        tbl.add_column("LaTeX", justify="right", style="green")
        tbl.add_column("Matplotlib", justify="right")

        if summary["figures_total"]:
            tbl.add_row(
                "图片",
                str(summary["figures_total"]),
                str(summary["figures_total"]),
                "-",
            )
        if summary["tables_total"]:
            mpl = summary["tables_matplotlib"]
            mpl_str = f"[bold red]{mpl}[/bold red]" if mpl else str(mpl)
            tbl.add_row(
                "表格",
                str(summary["tables_total"]),
                str(summary["tables_latex"]),
                mpl_str,
            )
        # 在 Live 运行时打印，表格永久写入终端历史，不会被后续 Live 刷新覆盖
        progress.console.print(tbl)
        progress.stop()
        result = typer.confirm("继续进行 LLM 分析？", default=True)
        progress.start()
        return result

    success = run_pipeline(
        url,
        skip_llm=skip_llm,
        debug=debug,
        force=force,
        on_event=on_event,
        on_confirm_force=on_confirm_force,
        on_confirm_llm=on_confirm_llm,
    )

    if _stream_win[0] is not None:
        _stream_win[0].__exit__(None, None, None)
    try:
        progress.stop()
    except Exception:
        pass

    if success and "page_url" in _result:
        page_url = _result["page_url"]
        console.print(
            Panel(
                Text.assemble(
                    ("Notion: ", "bold"),
                    (page_url, "link " + page_url),
                ),
                title="[green]✓ 添加成功",
                border_style="green",
            )
        )

    return success


def _run_citation_refresh(*, force: bool) -> bool:
    from paper_tool.citation_refresh import maybe_refresh_citations

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
    progress.start()
    current_task: list[int] = []

    def on_event(event: dict) -> None:
        t = event["type"]
        if t == "stage_start":
            task_id = progress.add_task(f"[cyan]{event['label']}", total=None)
            current_task.clear()
            current_task.append(task_id)
        elif t == "stage_done" and current_task:
            status = event.get("status", "ok")
            icon = "✓" if status == "ok" else "⚠"
            color = "green" if status == "ok" else "yellow"
            progress.update(
                current_task[-1],
                description=f"[{color}]{icon} {event['label']}",
                total=1,
                completed=1,
            )

    try:
        success = maybe_refresh_citations(force=force, on_event=on_event)
    finally:
        try:
            progress.stop()
        except Exception:
            pass

    return success


def _ensure_notion_database_ready() -> None:
    from paper_tool.config import get_config
    from paper_tool.notion_setup import check_database

    result = check_database()
    if result.ok:
        return

    cfg = get_config()
    error_console.print(f"[ERROR] Notion 自检失败: {result.reason}")

    if not cfg.notion_parent_page_id:
        console.print(
            "[yellow]未配置 `NOTION_PARENT_PAGE_ID`。"
            "首次建库前请先在 `.env` 中填写它。[/yellow]"
        )

    console.print("[yellow]请先运行预置建库脚本，然后再重试当前命令：[/yellow]")
    console.print("[bold]uv run python scripts/create_notion_db.py[/bold]")
    raise typer.Exit(code=1)


# ── Commands ─────────────────────────────────────────────────────────────────


@app.command()
def add(
    url: str = typer.Argument(..., help="Arxiv 或 OpenReview 论文链接"),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="跳过 LLM 分析（只保存元数据到 Notion）"
    ),
    debug: bool = typer.Option(
        False, "--debug", help="打印 LLM 原始 prompt 和回包，用于排查分类/笔记问题"
    ),
    stream: bool = typer.Option(
        False, "--stream", help="在固定小窗口实时显示 LLM 流式输出"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="若 Notion 中已存在同 URL 论文，二次确认后归档旧页面并重新导入",
    ),
) -> None:
    """添加一篇论文：下载 PDF、写入 Notion、生成 AI 笔记。"""
    log.info(
        "CMD add  url=%s  skip_llm=%s  debug=%s  force=%s", url, skip_llm, debug, force
    )
    _ensure_notion_database_ready()
    success = _process_paper(
        url,
        skip_llm=skip_llm,
        debug=debug,
        stream=stream,
        force=force,
    )
    if not success:
        raise typer.Exit(code=1)


@app.command()
def batch(
    file: Path = typer.Argument(
        ..., help="包含论文链接的文件（支持 txt/csv/md 等任意格式，自动提取链接）"
    ),
    skip_llm: bool = typer.Option(False, "--skip-llm", help="跳过 LLM 分析"),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error/--stop-on-error",
        help="遇到错误时是否继续处理下一篇（默认出错即停）",
    ),
    debug: bool = typer.Option(
        False, "--debug", help="打印 LLM 原始 prompt 和回包，用于排查问题"
    ),
    stream: bool = typer.Option(
        False, "--stream", help="在固定小窗口实时显示 LLM 流式输出"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="若 Notion 中已存在同 URL 论文，二次确认后归档旧页面并重新导入",
    ),
) -> None:
    """批量添加论文（自动从文件中提取论文链接，支持 txt/csv/md 等任意格式）。"""
    log.info(
        "CMD batch  file=%s  skip_llm=%s  continue_on_error=%s",
        file,
        skip_llm,
        continue_on_error,
    )
    _ensure_notion_database_ready()
    if not file.exists():
        error_console.print(f"[ERROR] 文件不存在: {file}")
        raise typer.Exit(code=1)

    urls = _extract_urls(file.read_text())

    if not urls:
        console.print("[yellow]文件中没有找到有效的 URL[/yellow]")
        return

    console.print(f"[bold]共找到 {len(urls)} 个 URL，开始处理...[/bold]\n")

    success_count = 0
    fail_count = 0

    for i, url in enumerate(urls, 1):
        console.print(f"[bold dim]─── [{i}/{len(urls)}] {url} ───[/bold dim]")
        success = _process_paper(
            url,
            skip_llm=skip_llm,
            debug=debug,
            stream=stream,
            force=force,
        )
        if success:
            success_count += 1
        else:
            fail_count += 1
            if not continue_on_error:
                console.print("[red]遇到错误，停止处理[/red]")
                break
        console.print()

    console.print(
        Panel(
            "成功: "
            f"[green]{success_count}[/green]  "
            f"失败: [red]{fail_count}[/red]  "
            f"共: {len(urls)}",
            title="批量处理完成",
        )
    )
    if fail_count > 0:
        raise typer.Exit(code=1)


# ── Config subcommands ────────────────────────────────────────────────────────


@config_app.command("show")
def config_show() -> None:
    """显示当前配置（API key 脱敏显示）。"""
    from paper_tool.config import get_config

    try:
        cfg = get_config()
        cfg.show()
    except Exception as e:
        error_console.print(f"[ERROR] 加载配置失败: {e}")
        raise typer.Exit(code=1)


@config_app.command("init")
def config_init() -> None:
    """交互式引导创建 .env 文件。"""
    from paper_tool.config import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"

    if env_path.exists():
        overwrite = typer.confirm(
            f".env 文件已存在 ({env_path})，是否覆盖？", default=False
        )
        if not overwrite:
            console.print("已取消")
            return

    console.print("[bold]paper-tool 配置初始化[/bold]\n")
    console.print("请依次输入以下配置（直接回车可跳过可选项）：\n")

    notion_token = typer.prompt("Notion Integration Token (secret_...)", default="")
    notion_db_id = typer.prompt(
        "Notion 数据库 ID（可选；已有数据库时填写）",
        default="",
    )
    notion_parent_page_id = typer.prompt(
        "Notion 父页面 ID（可选，用于脚本创建数据库）",
        default="",
    )

    console.print("\n[dim]LLM API Keys（根据你使用的模型填写，其余留空）[/dim]")
    openai_key = typer.prompt("OpenAI API Key (sk-...)", default="", hide_input=True)
    openai_base_url = typer.prompt(
        "OpenAI 兼容端点 Base URL（留空使用官方 api.openai.com）",
        default="",
    )
    anthropic_key = typer.prompt(
        "Anthropic API Key (sk-ant-...)", default="", hide_input=True
    )
    gemini_key = typer.prompt("Google Gemini API Key", default="", hide_input=True)

    console.print("\n[dim]OpenReview 账号（可选，用于需要登录才能下载的论文）[/dim]")
    or_user = typer.prompt("OpenReview 用户名", default="")
    or_pass = typer.prompt("OpenReview 密码", default="", hide_input=True)

    lines = [
        "# paper-tool 环境配置\n",
        f"NOTION_TOKEN={notion_token}\n",
        f"NOTION_DATABASE_ID={notion_db_id}\n",
        f"NOTION_PARENT_PAGE_ID={notion_parent_page_id}\n",
        f"OPENAI_API_KEY={openai_key}\n",
        f"OPENAI_BASE_URL={openai_base_url}\n",
        f"ANTHROPIC_API_KEY={anthropic_key}\n",
        f"GEMINI_API_KEY={gemini_key}\n",
        f"OPENREVIEW_USERNAME={or_user}\n",
        f"OPENREVIEW_PASSWORD={or_pass}\n",
    ]

    env_path.write_text("".join(lines))
    console.print(f"\n[green]✓ 配置已保存到 {env_path}[/green]")
    console.print("[dim]请运行 [bold]paper-tool config show[/bold] 确认配置正确[/dim]")


@config_app.command("check-db")
def config_check_db() -> None:
    """检查 Notion 数据库的实际属性，并与 notion_schema.yaml 进行对比。"""
    from rich.table import Table

    from paper_tool.config import get_config
    from paper_tool.notion_setup import expected_property_types

    try:
        cfg = get_config()
    except Exception as e:
        error_console.print(f"[ERROR] 加载配置失败: {e}")
        raise typer.Exit(code=1)

    console.print(f"[dim]数据库 ID: {cfg.notion_database_id}[/dim]\n")

    # ── 拉取数据库 schema ─────────────────────────────────────────────────
    try:
        from notion_client import Client

        client = Client(auth=cfg.notion_token)
        db = client.databases.retrieve(database_id=cfg.notion_database_id)
    except Exception as e:
        error_console.print(f"[ERROR] 无法访问 Notion 数据库: {e}")
        raise typer.Exit(code=1)

    db_title = ""
    for part in db.get("title", []):
        db_title += part.get("plain_text", "")
    console.print(f"[bold]数据库名称:[/bold] {db_title or '(未知)'}\n")

    actual_props: dict[str, str] = {
        name: prop.get("type", "unknown")
        for name, prop in db.get("properties", {}).items()
    }
    expected_props = expected_property_types(cfg)

    # ── 展示数据库中所有属性 ──────────────────────────────────────────────
    all_table = Table(title="数据库实际属性", show_lines=True)
    all_table.add_column("属性名", style="cyan")
    all_table.add_column("类型", style="dim")

    type_emoji = {
        "title": "🔤",
        "rich_text": "📝",
        "select": "🔘",
        "multi_select": "🏷️",
        "date": "📅",
        "url": "🔗",
        "number": "🔢",
        "checkbox": "☑️",
        "files": "📎",
        "people": "👤",
        "relation": "🔀",
        "formula": "🧮",
        "rollup": "📊",
        "created_time": "⏱️",
        "last_edited_time": "⏱️",
    }

    for name, ptype in sorted(actual_props.items()):
        icon = type_emoji.get(ptype, "❓")
        all_table.add_row(name, f"{icon} {ptype}")

    console.print(all_table)
    console.print()

    # ── 对比 schema 中的属性映射 ────────────────────────────────────────
    mapped_props = cfg.notion_properties
    check_table = Table(title="notion_schema.yaml 映射检查", show_lines=True)
    check_table.add_column("配置键", style="cyan")
    check_table.add_column("映射到的属性名", style="yellow")
    check_table.add_column("实际类型", style="dim")
    check_table.add_column("状态")

    all_ok = True
    for key, mapped_name in mapped_props.items():
        if not mapped_name:
            continue

        expected_type = expected_props.get(mapped_name, "")
        if mapped_name in actual_props:
            ptype = actual_props[mapped_name]
            if expected_type and ptype != expected_type:
                status = f"[red]✗ 类型不匹配（期望 {expected_type}）[/red]"
                all_ok = False
            else:
                status = "[green]✓ 存在[/green]"
        else:
            ptype = "-"
            status = "[red]✗ 不存在[/red]"
            all_ok = False
        check_table.add_row(key, mapped_name, ptype, status)

    console.print(check_table)

    if all_ok:
        console.print("\n[green]✓ 所有映射属性均存在，配置正确。[/green]")
    else:
        console.print(
            "\n[yellow]⚠ 部分属性不存在。"
            "请检查 Notion 数据库是否缺少对应属性，"
            "如果想直接新建一个匹配 schema 的数据库，请运行 "
            "[bold]uv run python scripts/create_notion_db.py[/bold]。[/yellow]"
        )
        raise typer.Exit(code=1)


@app.command()
def chat(
    paper: str = typer.Argument(
        ...,
        help="论文文件名、Arxiv ID 关键词或完整路径（如 2603.08706、Agentic）",
    ),
    debug: bool = typer.Option(False, "--debug", help="打印调试信息"),
    stream: bool = typer.Option(
        False, "--stream", help="在固定小窗口实时显示 LLM 流式输出"
    ),
) -> None:
    """与论文进行多轮对话问答（含完整论文上下文）。

    \b
    交互命令：
      /reset   清空对话历史（保留论文上下文）
      /exit    退出
    """
    from rich.markdown import Markdown
    from rich.rule import Rule

    from paper_tool.config import get_config
    from paper_tool.llm_chat import ChatSession, find_paper_file

    cfg = get_config()

    # ── 找到论文文件 ──────────────────────────────────────────────────────
    try:
        file_path = find_paper_file(paper, cfg.papers_dir)
    except FileNotFoundError as e:
        error_console.print(f"[ERROR] {e}")
        raise typer.Exit(code=1)

    # For new per-paper subdirectory structure, show the directory name
    # rather than the generic "paper.tex" / "paper.pdf" filename.
    display_name = (
        file_path.parent.name
        if file_path.name in ("paper.tex", "paper.pdf")
        else file_path.name
    )
    console.print(f"\n[bold cyan]论文[/bold cyan]: {display_name}")
    console.print(f"[dim]模型: {cfg.llm_model}  |  加载论文文本中...[/dim]")

    try:
        session = ChatSession(file_path, title=file_path.stem.replace("_", " "))
    except Exception as e:
        error_console.print(f"[ERROR] 加载论文失败: {e}")
        raise typer.Exit(code=1)

    console.print(
        f"[dim]已加载 {session.paper_char_count:,} 字符  |  "
        f"/reset 清空历史  /exit 退出[/dim]"
    )
    console.print(Rule(style="dim"))

    # ── 对话循环 ─────────────────────────────────────────────────────────
    while True:
        try:
            question = console.input("[bold green]你[/bold green] › ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/dim]")
            break

        if not question:
            continue

        if question.lower() in ("/exit", "/quit", "exit", "quit"):
            console.print("[dim]再见[/dim]")
            break

        if question.lower() == "/reset":
            session.reset()
            console.print("[dim]✓ 对话历史已清空[/dim]")
            console.print(Rule(style="dim"))
            continue

        console.print(
            f"[bold blue]AI[/bold blue] [dim](第 {session.turn_count + 1} 轮)[/dim]"
        )
        try:
            answer = session.ask(question, debug=debug, stream=stream)
        except Exception as e:
            console.print(f"[red]请求失败: {e}[/red]")
            # Roll back the user message so the failed turn doesn't corrupt history
            if session.messages and session.messages[-1]["role"] == "user":
                session.messages.pop()
            continue

        console.print(Markdown(answer))
        console.print(Rule(style="dim"))


@app.command("refresh-citations")
def refresh_citations() -> None:
    """立即刷新当前 Notion 数据库中的引用量。"""
    _ensure_notion_database_ready()
    success = _run_citation_refresh(force=True)
    if not success:
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
) -> None:
    """启动 Web 前端界面服务。"""
    import uvicorn

    console.print(f"[bold green]paper-tool Web UI[/bold green]  http://{host}:{port}")
    uvicorn.run(
        "paper_tool.server:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    app()
