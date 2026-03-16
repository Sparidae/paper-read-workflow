"""paper-tool CLI - main entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

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


def _process_paper(url: str, skip_llm: bool = False, debug: bool = False) -> bool:
    """
    Core pipeline: download -> extract -> analyze -> write to Notion.
    Returns True on success, False on failure.
    """
    from paper_tool.downloaders import get_downloader
    from paper_tool.pdf_parser import extract_text
    from paper_tool.llm_analyzer import LLMAnalyzer
    from paper_tool.llm_classifier import LLMClassifier
    from paper_tool.llm_summarizer import LLMSummarizer
    from paper_tool.notion_service import NotionService
    from paper_tool.config import get_config

    cfg = get_config()
    url = url.strip()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:

        def _done(task_id: int, description: str) -> None:
            """Mark a task as finished: freeze timer and stop spinner."""
            progress.update(task_id, description=description, total=1, completed=1)

        # ── Step 1: Fetch metadata ─────────────────────────────────────────
        task = progress.add_task("[cyan]获取论文元数据...", total=None)
        try:
            downloader = get_downloader(url)
            metadata = downloader.fetch_metadata(url)
            _done(task, f"[green]✓ 元数据获取成功: {metadata.title[:60]}")
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[ERROR] 元数据获取失败: {e}")
            return False

        # ── Step 2: Check for duplicates ──────────────────────────────────
        task2 = progress.add_task("[cyan]检查 Notion 中是否已存在...", total=None)
        try:
            notion = NotionService()
            existing_id = notion.find_existing_page(metadata.url)
            if existing_id:
                _done(task2, "[yellow]⚠ 论文已存在于 Notion，跳过")
                progress.stop()
                console.print(
                    f"\n[yellow]论文已存在: {notion.get_page_url(existing_id)}[/yellow]"
                )
                return True
            _done(task2, "[green]✓ 未重复，继续处理")
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[ERROR] Notion 连接失败: {e}")
            return False

        # ── Step 3: Download PDF ──────────────────────────────────────────
        task3 = progress.add_task("[cyan]下载 PDF...", total=None)
        try:
            pdf_path = downloader.download_pdf(metadata, cfg.papers_dir)
            metadata.pdf_path = str(pdf_path)
            _done(task3, f"[green]✓ PDF 已保存: {pdf_path.name}")
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[ERROR] PDF 下载失败: {e}")
            return False

        # ── Step 4: Extract text (LaTeX preferred, fallback to PDF) ──────
        from paper_tool.models import PaperSource
        from paper_tool.downloaders.arxiv import ArxivDownloader
        from paper_tool.pdf_parser import extract_text_from_latex

        char_budget = cfg.llm_max_input_tokens * 4
        paper_text: str | None = None
        tex_path: "Path | None" = None
        text_source = "PDF"

        if isinstance(downloader, ArxivDownloader):
            task4 = progress.add_task("[cyan]下载 LaTeX 源码...", total=None)
            try:
                tex_path = downloader.download_latex_source(metadata, cfg.papers_dir)
                if tex_path:
                    paper_text = extract_text_from_latex(tex_path, max_chars=char_budget)
                    text_source = "LaTeX"
                    _done(task4, f"[green]✓ LaTeX 源码解析完成 {len(paper_text):,} 字符")
                else:
                    _done(task4, "[yellow]⚠ 无 LaTeX 源码，降级到 PDF 解析")
            except Exception as e:
                _done(task4, f"[yellow]⚠ LaTeX 解析失败，降级到 PDF: {e}")

        if paper_text is None:
            task4b = progress.add_task("[cyan]提取 PDF 文本...", total=None)
            try:
                paper_text = extract_text(pdf_path, max_chars=char_budget)
                _done(task4b, f"[green]✓ PDF 文本提取完成 {len(paper_text):,} 字符")
            except Exception as e:
                progress.stop()
                error_console.print(f"\n[ERROR] PDF 文本提取失败: {e}")
                return False

        pdf_text = paper_text

        # ── Step 5: Create Notion page ────────────────────────────────────
        task5 = progress.add_task("[cyan]创建 Notion 页面...", total=None)
        try:
            page_id = notion.create_page(metadata)
            _done(task5, "[green]✓ Notion 页面创建成功")
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[ERROR] 创建 Notion 页面失败: {e}")
            return False

        # ── Step 6a: LLM classification (title + abstract only) ──────────
        if not skip_llm:
            task6a = progress.add_task("[cyan]LLM 分类标注中...", total=None)
            try:
                available_options = notion.get_classification_options()
                classifier = LLMClassifier()
                classification = classifier.classify(metadata, available_options, debug=debug)
                notion.update_classifications(page_id, classification)

                new_tags: list[str] = []
                new_tags += [t for t in classification.paper_type
                             if t not in available_options.get("paper_type", [])]
                new_tags += [t for t in classification.research_areas
                             if t not in available_options.get("research_areas", [])]
                new_tags += [t for t in classification.institutions
                             if t not in available_options.get("institutions", [])]

                suffix = f"  [新增: {', '.join(new_tags)}]" if new_tags else ""
                _done(task6a, f"[green]✓ 分类完成{suffix}")
            except Exception as e:
                _done(task6a, f"[yellow]⚠ 分类失败（已跳过）: {e}")

        # ── Step 6c: LLM one-sentence summary (title + abstract only) ────
        if not skip_llm:
            task6c = progress.add_task("[cyan]LLM 生成一句话摘要...", total=None)
            try:
                summarizer = LLMSummarizer()
                summary = summarizer.summarize(metadata, debug=debug)
                notion.update_summary(page_id, summary)
                _done(task6c, "[green]✓ 一句话摘要写入完成")
            except Exception as e:
                _done(task6c, f"[yellow]⚠ 一句话摘要生成失败（已跳过）: {e}")

        # ── Step 6b-pre: Extract and translate figures (before note gen) ───
        figures = []
        if isinstance(downloader, ArxivDownloader) and tex_path is not None:
            from paper_tool.figure_extractor import convert_pdf_figures, parse_figures

            task_fig = progress.add_task("[cyan]提取论文图片...", total=None)
            try:
                figures_dir = downloader.get_figures_dir(metadata, cfg.papers_dir)
                n_converted = convert_pdf_figures(figures_dir)
                if n_converted:
                    progress.console.print(
                        f"  [dim]已将 {n_converted} 个 PDF 图片转换为 PNG[/dim]"
                    )
                figures = parse_figures(tex_path, figures_dir)
                if figures:
                    _done(task_fig, f"[green]✓ 找到 {len(figures)} 张图片")
                else:
                    _done(task_fig, "[yellow]⚠ 未找到可用图片")
            except Exception as e:
                _done(task_fig, f"[yellow]⚠ 图片提取失败（已跳过）: {e}")
                figures = []

        if not skip_llm and figures:
            from paper_tool.llm_analyzer import translate_captions

            task_trans = progress.add_task("[cyan]翻译图片说明...", total=None)
            try:
                figures = translate_captions(figures)
                _done(task_trans, "[green]✓ 图片说明翻译完成")
            except Exception as e:
                _done(task_trans, f"[yellow]⚠ 翻译失败（保留原文）: {e}")

        # ── Step 6b: LLM note generation (with figure info for placement) ─
        if not skip_llm:
            task6b = progress.add_task(
                f"[cyan]LLM 生成笔记 ({cfg.llm_model})...", total=None
            )
            try:
                analyzer = LLMAnalyzer()
                note = analyzer.analyze(
                    metadata, pdf_text, debug=debug,
                    figures=figures if figures else None,
                )
                _done(task6b, "[green]✓ 笔记生成完成")
            except Exception as e:
                _done(task6b, f"[yellow]⚠ 笔记生成失败（已跳过）: {e}")
                note = None
        else:
            note = None

        # ── Step 7: Write note + figures to Notion ────────────────────────
        if note is not None:
            task7 = progress.add_task("[cyan]将笔记和图片写入 Notion...", total=None)
            try:
                if figures:
                    n = notion.append_note_with_figures(page_id, note, figures)
                    _done(task7, f"[green]✓ 笔记写入完成，上传 {n}/{len(figures)} 张图片")
                else:
                    notion.append_note(page_id, note)
                    _done(task7, "[green]✓ 笔记写入完成")
            except Exception as e:
                _done(task7, f"[yellow]⚠ 笔记写入失败: {e}")
        elif figures:
            task7 = progress.add_task("[cyan]上传论文图片...", total=None)
            try:
                n = notion.append_figures(page_id, figures)
                _done(task7, f"[green]✓ 上传 {n}/{len(figures)} 张图片完成")
            except Exception as e:
                _done(task7, f"[yellow]⚠ 图片上传失败（已跳过）: {e}")

        progress.stop()

    page_url = notion.get_page_url(page_id)
    console.print(
        Panel(
            Text.assemble(
                ("论文: ", "bold"),
                (metadata.title + "\n", "cyan"),
                ("作者: ", "bold"),
                (metadata.authors_str[:100] + "\n", ""),
                ("来源: ", "bold"),
                (metadata.source.value + "\n", ""),
                ("目录:  ", "bold"),
                (str(pdf_path.parent) + "\n", "dim"),
                ("Notion: ", "bold"),
                (page_url, "link " + page_url),
            ),
            title="[green]✓ 添加成功",
            border_style="green",
        )
    )
    return True


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
) -> None:
    """添加一篇论文：下载 PDF、写入 Notion、生成 AI 笔记。"""
    success = _process_paper(url, skip_llm=skip_llm, debug=debug)
    if not success:
        raise typer.Exit(code=1)


@app.command()
def batch(
    file: Path = typer.Argument(
        ..., help="包含论文链接的文本文件，每行一个 URL"
    ),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="跳过 LLM 分析"
    ),
    continue_on_error: bool = typer.Option(
        True, "--continue-on-error/--stop-on-error",
        help="遇到错误时是否继续处理下一篇"
    ),
    debug: bool = typer.Option(
        False, "--debug", help="打印 LLM 原始 prompt 和回包，用于排查问题"
    ),
) -> None:
    """批量添加论文（从文件读取 URL 列表，每行一个）。"""
    if not file.exists():
        error_console.print(f"[ERROR] 文件不存在: {file}")
        raise typer.Exit(code=1)

    urls = [
        line.strip()
        for line in file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not urls:
        console.print("[yellow]文件中没有找到有效的 URL[/yellow]")
        return

    console.print(f"[bold]共找到 {len(urls)} 个 URL，开始处理...[/bold]\n")

    success_count = 0
    fail_count = 0

    for i, url in enumerate(urls, 1):
        console.print(f"[bold dim]─── [{i}/{len(urls)}] {url} ───[/bold dim]")
        success = _process_paper(url, skip_llm=skip_llm, debug=debug)
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
            f"成功: [green]{success_count}[/green]  失败: [red]{fail_count}[/red]  共: {len(urls)}",
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
        overwrite = typer.confirm(f".env 文件已存在 ({env_path})，是否覆盖？", default=False)
        if not overwrite:
            console.print("已取消")
            return

    console.print("[bold]paper-tool 配置初始化[/bold]\n")
    console.print("请依次输入以下配置（直接回车可跳过可选项）：\n")

    notion_token = typer.prompt("Notion Integration Token (secret_...)", default="")
    notion_db_id = typer.prompt("Notion 数据库 ID", default="")

    console.print("\n[dim]LLM API Keys（根据你使用的模型填写，其余留空）[/dim]")
    openai_key = typer.prompt("OpenAI API Key (sk-...)", default="", hide_input=True)
    openai_base_url = typer.prompt(
        "OpenAI 兼容端点 Base URL（留空使用官方 api.openai.com）",
        default="",
    )
    anthropic_key = typer.prompt("Anthropic API Key (sk-ant-...)", default="", hide_input=True)
    gemini_key = typer.prompt("Google Gemini API Key", default="", hide_input=True)

    console.print("\n[dim]OpenReview 账号（可选，用于需要登录才能下载的论文）[/dim]")
    or_user = typer.prompt("OpenReview 用户名", default="")
    or_pass = typer.prompt("OpenReview 密码", default="", hide_input=True)

    lines = [
        "# paper-tool 环境配置\n",
        f"NOTION_TOKEN={notion_token}\n",
        f"NOTION_DATABASE_ID={notion_db_id}\n",
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
    """检查 Notion 数据库的实际属性，并与 config.yaml 的映射进行对比。"""
    from rich.table import Table
    from paper_tool.config import get_config

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

    # ── 对比 config.yaml 中的属性映射 ────────────────────────────────────
    mapped_props = cfg.notion_properties
    check_table = Table(title="config.yaml 映射检查", show_lines=True)
    check_table.add_column("配置键", style="cyan")
    check_table.add_column("映射到的属性名", style="yellow")
    check_table.add_column("实际类型", style="dim")
    check_table.add_column("状态")

    all_ok = True
    for key, mapped_name in mapped_props.items():
        if mapped_name in actual_props:
            ptype = actual_props[mapped_name]
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
            "或在 config.yaml 的 notion.properties 中修改属性名映射。[/yellow]"
        )
        raise typer.Exit(code=1)


@app.command()
def chat(
    paper: str = typer.Argument(
        ...,
        help="论文文件名、Arxiv ID 关键词或完整路径（如 2603.08706、Agentic）",
    ),
    debug: bool = typer.Option(False, "--debug", help="打印调试信息"),
) -> None:
    """与论文进行多轮对话问答（含完整论文上下文）。

    \b
    交互命令：
      /reset   清空对话历史（保留论文上下文）
      /exit    退出
    """
    from paper_tool.config import get_config
    from paper_tool.llm_chat import ChatSession, find_paper_file
    from rich.markdown import Markdown
    from rich.rule import Rule

    cfg = get_config()

    # ── 找到论文文件 ──────────────────────────────────────────────────────
    try:
        file_path = find_paper_file(paper, cfg.papers_dir)
    except FileNotFoundError as e:
        error_console.print(f"[ERROR] {e}")
        raise typer.Exit(code=1)

    # For new per-paper subdirectory structure, show the directory name (more informative)
    # than the generic "paper.tex" / "paper.pdf" filename.
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

        console.print(f"[bold blue]AI[/bold blue] [dim](第 {session.turn_count + 1} 轮)[/dim]")
        try:
            answer = session.ask(question, debug=debug)
        except Exception as e:
            console.print(f"[red]请求失败: {e}[/red]")
            # Roll back the user message so the failed turn doesn't corrupt history
            if session.messages and session.messages[-1]["role"] == "user":
                session.messages.pop()
            continue

        console.print(Markdown(answer))
        console.print(Rule(style="dim"))


if __name__ == "__main__":
    app()
