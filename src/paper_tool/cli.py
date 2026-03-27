"""paper-tool CLI - main entry point."""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
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

def _now_ts() -> str:
    """Current local time as HH:MM:SS for progress logs."""
    return time.strftime("%H:%M:%S", time.localtime())


def _fmt_elapsed(elapsed: float) -> str:
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    minutes, seconds = divmod(int(elapsed), 60)
    return f"{minutes}m{seconds:02d}s"


class _StepProgress:
    """Per-step logger backed by a pipeline progress bar."""

    def __init__(self, tracker: "_PipelineProgress", name: str) -> None:
        self._tracker = tracker
        self.name = name
        self._start = 0.0

    def __enter__(self) -> "_StepProgress":
        self._start = time.monotonic()
        self._tracker._start_step(self.name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._tracker._finish_step()

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._start)

    def done(self, detail: str = "") -> None:
        suffix = f"：{detail}" if detail else ""
        self._tracker._log(
            f"[dim][{_now_ts()}][/dim] [green]完成[/green]：{self.name}{suffix} "
            f"[dim]({_fmt_elapsed(self.elapsed())})[/dim]"
        )

    def warn(self, detail: str) -> None:
        self._tracker._log(
            f"[dim][{_now_ts()}][/dim] [yellow]警告[/yellow]：{self.name}：{detail} "
            f"[dim]({_fmt_elapsed(self.elapsed())})[/dim]"
        )

    def fail(self, detail: str) -> None:
        self._tracker._log(
            f"[dim][{_now_ts()}][/dim] [ERROR] {self.name}失败：{detail} "
            f"[dim]({_fmt_elapsed(self.elapsed())})[/dim]",
            error=True,
        )


class _PipelineProgress:
    """Track end-to-end pipeline progress for one paper."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._progress: Progress | None = None
        self._task_id: int | None = None
        self._total_steps = 0
        self._done_steps = 0

    def __enter__(self) -> "_PipelineProgress":
        if self._enabled:
            self._progress = Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("{task.description}", justify="left"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            )
            self._progress.__enter__()
            self._task_id = self._progress.add_task("[cyan]准备开始[/cyan]", total=0)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._progress is not None:
            self._progress.__exit__(exc_type, exc, tb)

    def step(self, name: str) -> _StepProgress:
        return _StepProgress(self, name)

    def _start_step(self, name: str) -> None:
        self._total_steps += 1
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                total=self._total_steps,
                completed=self._done_steps,
                description=f"[cyan]{name}[/cyan]",
            )
            return
        console.print(f"[dim][{_now_ts()}][/dim] [cyan]开始[/cyan]：{name}")

    def _finish_step(self) -> None:
        self._done_steps += 1
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                completed=self._done_steps,
                description=f"[green]阶段进度 {self._done_steps}/{self._total_steps}[/green]",
            )

    def _log(self, message: str, *, error: bool = False) -> None:
        if error:
            error_console.print(message)
            return
        if self._progress is not None:
            self._progress.console.print(message)
            return
        console.print(message)


def _process_paper(
    url: str,
    skip_llm: bool = False,
    debug: bool = False,
    stream: bool = False,
    force: bool = False,
) -> bool:
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
    with _PipelineProgress(enabled=console.is_terminal) as pipeline:
        # ── Step 1: Fetch metadata ─────────────────────────────────────────
        with pipeline.step("获取论文元数据") as step:
            try:
                downloader = get_downloader(url)
                metadata = downloader.fetch_metadata(url)
                step.done(f"{metadata.title[:60]}")
            except Exception as e:
                step.fail(str(e))
                return False

        # ── Step 2: Check for duplicates ───────────────────────────────────
        with pipeline.step("检查 Notion 中是否已存在") as step:
            try:
                notion = NotionService()
                existing_ids = notion.find_existing_pages(metadata.url)
                if existing_ids:
                    if force:
                        for existing_id in existing_ids:
                            notion.archive_page(existing_id)
                        step.warn(f"已归档 {len(existing_ids)} 条旧记录，继续重建")
                    else:
                        existing_id = existing_ids[0]
                        step.warn("论文已存在于 Notion，跳过")
                        console.print(f"[yellow]论文已存在: {notion.get_page_url(existing_id)}[/yellow]")
                        console.print("[dim]如需覆盖重建，请添加 --force[/dim]")
                        return True
                else:
                    step.done("未重复，继续处理")
            except Exception as e:
                step.fail(f"Notion 连接失败: {e}")
                return False

        # ── Step 3: Download PDF ───────────────────────────────────────────
        with pipeline.step("下载 PDF") as step:
            try:
                pdf_path = downloader.download_pdf(metadata, cfg.papers_dir)
                metadata.pdf_path = str(pdf_path)
                step.done(f"已保存: {pdf_path.name}")
            except Exception as e:
                step.fail(str(e))
                return False

        # ── Step 4: Extract text (LaTeX preferred, fallback to PDF) ───────
        from paper_tool.downloaders.arxiv import ArxivDownloader
        from paper_tool.pdf_parser import extract_text_from_latex

        char_budget = cfg.llm_max_input_tokens * 4
        paper_text: str | None = None
        tex_path: "Path | None" = None

        if isinstance(downloader, ArxivDownloader):
            with pipeline.step("下载 LaTeX 源码并解析文本") as step:
                try:
                    tex_path = downloader.download_latex_source(metadata, cfg.papers_dir)
                    if tex_path:
                        paper_text = extract_text_from_latex(tex_path, max_chars=char_budget)
                        step.done(f"LaTeX 解析完成 {len(paper_text):,} 字符")
                    else:
                        step.warn("无 LaTeX 源码，降级到 PDF 解析")
                except Exception as e:
                    step.warn(f"LaTeX 解析失败，降级到 PDF: {e}")

        if paper_text is None:
            with pipeline.step("提取 PDF 文本") as step:
                try:
                    paper_text = extract_text(pdf_path, max_chars=char_budget)
                    step.done(f"PDF 文本提取完成 {len(paper_text):,} 字符")
                except Exception as e:
                    step.fail(str(e))
                    return False

        pdf_text = paper_text

        # ── Step 5: Create Notion page ─────────────────────────────────────
        with pipeline.step("创建 Notion 页面") as step:
            try:
                page_id = notion.create_page(metadata)
                step.done("创建成功")
            except Exception as e:
                step.fail(str(e))
                return False

        # ── Step 6a: LLM classification (title + abstract only) ───────────
        if not skip_llm:
            with pipeline.step("LLM 分类标注") as step:
                try:
                    available_options = notion.get_classification_options()
                    classifier = LLMClassifier()
                    classification = classifier.classify(
                        metadata,
                        available_options,
                        debug=debug,
                        stream=stream,
                    )
                    notion.update_classifications(page_id, classification)

                    new_tags: list[str] = []
                    new_tags += [t for t in classification.paper_type
                                 if t not in available_options.get("paper_type", [])]
                    new_tags += [t for t in classification.research_areas
                                 if t not in available_options.get("research_areas", [])]
                    new_tags += [t for t in classification.institutions
                                 if t not in available_options.get("institutions", [])]

                    suffix = f"新增: {', '.join(new_tags)}" if new_tags else "完成"
                    step.done(suffix)
                except Exception as e:
                    step.warn(f"分类失败（已跳过）: {e}")

        # ── Step 6c: LLM one-sentence summary (title + abstract only) ─────
        if not skip_llm:
            with pipeline.step("LLM 生成一句话摘要") as step:
                try:
                    summarizer = LLMSummarizer()
                    summary = summarizer.summarize(metadata, debug=debug, stream=stream)
                    notion.update_summary(page_id, summary)
                    step.done("一句话摘要写入完成")
                except Exception as e:
                    step.warn(f"一句话摘要生成失败（已跳过）: {e}")

        # ── Step 6b-pre: Extract and translate figures (before note gen) ──
        figures = []
        tables = []
        if isinstance(downloader, ArxivDownloader) and tex_path is not None:
            from paper_tool.figure_extractor import convert_pdf_figures, parse_figures
            from paper_tool.table_extractor import parse_tables

            with pipeline.step("提取论文图片") as step:
                try:
                    figures_dir = downloader.get_figures_dir(metadata, cfg.papers_dir)
                    n_converted = convert_pdf_figures(figures_dir)
                    figures = parse_figures(
                        tex_path,
                        figures_dir,
                        max_figures=cfg.max_figures,
                        force_rerender=cfg.rerender_figures,
                    )
                    if figures:
                        backend_counts: dict[str, int] = {}
                        for fig in figures:
                            backend = fig.render_backend or "unknown"
                            backend_counts[backend] = backend_counts.get(backend, 0) + 1
                        backend_summary = ", ".join(
                            f"{name}={count}" for name, count in sorted(backend_counts.items())
                        )
                        convert_msg = f"，PDF→PNG {n_converted} 张" if n_converted else ""
                        step.done(f"找到 {len(figures)} 张图片 [{backend_summary}]{convert_msg}")
                    else:
                        step.warn("未找到可用图片")
                except Exception as e:
                    step.warn(f"图片提取失败（已跳过）: {e}")
                    figures = []

            with pipeline.step("提取并渲染论文表格") as step:
                try:
                    tables_dir = downloader.get_figures_dir(metadata, cfg.papers_dir).parent / "tables"
                    tables = parse_tables(
                        tex_path,
                        tables_dir,
                        max_tables=cfg.max_tables,
                        force_rerender=cfg.rerender_tables,
                    )
                    if tables:
                        backend_counts: dict[str, int] = {}
                        for table in tables:
                            backend = table.render_backend or "unknown"
                            backend_counts[backend] = backend_counts.get(backend, 0) + 1
                        backend_summary = ", ".join(
                            f"{name}={count}" for name, count in sorted(backend_counts.items())
                        )
                        step.done(f"找到 {len(tables)} 张表格 [{backend_summary}]")
                    else:
                        step.warn("未找到可用表格")
                except Exception as e:
                    step.warn(f"表格提取失败（已跳过）: {e}")
                    tables = []

        if not skip_llm and figures:
            from paper_tool.llm_analyzer import translate_captions

            with pipeline.step("翻译图片说明") as step:
                try:
                    figures = translate_captions(
                        figures,
                        stream=stream,
                        stream_title="LLM 流式输出 · 图片说明翻译",
                    )
                    step.done("图片说明翻译完成")
                except Exception as e:
                    step.warn(f"翻译失败（保留原文）: {e}")

        if not skip_llm and tables:
            from paper_tool.llm_analyzer import translate_captions

            with pipeline.step("翻译表格说明") as step:
                try:
                    tables = translate_captions(
                        tables,
                        stream=stream,
                        stream_title="LLM 流式输出 · 表格说明翻译",
                    )
                    step.done("表格说明翻译完成")
                except Exception as e:
                    step.warn(f"翻译失败（保留原文）: {e}")

        # ── Step 6b: LLM note generation (with figure info for placement) ─
        if not skip_llm:
            with pipeline.step(f"LLM 生成笔记 ({cfg.llm_model})") as step:
                try:
                    analyzer = LLMAnalyzer()
                    note = analyzer.analyze(
                        metadata,
                        pdf_text,
                        debug=debug,
                        figures=figures if figures else None,
                        tables=tables if tables else None,
                        stream=stream,
                    )
                    step.done("笔记生成完成")
                except Exception as e:
                    step.warn(f"笔记生成失败（已跳过）: {e}")
                    note = None
        else:
            note = None

        # ── Step 7: Write note + figures + tables to Notion ───────────────
        has_visuals = bool(figures or tables)
        if note is not None:
            with pipeline.step("将笔记和图表写入 Notion") as step:
                try:
                    if has_visuals:
                        n = notion.append_note_with_figures(page_id, note, figures, tables)
                        total_vis = len(figures) + len(tables)
                        step.done(f"笔记写入完成，上传 {n}/{total_vis} 张图表")
                    else:
                        notion.append_note(page_id, note)
                        step.done("笔记写入完成")
                except Exception as e:
                    step.fail(f"笔记写入失败: {e}")
                    return False
        elif has_visuals:
            with pipeline.step("上传论文图表") as step:
                try:
                    n = notion.append_figures(page_id, figures + tables)
                    total_vis = len(figures) + len(tables)
                    step.done(f"上传 {n}/{total_vis} 张图表完成")
                except Exception as e:
                    step.fail(f"图表上传失败: {e}")
                    return False

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
    stream: bool = typer.Option(
        False, "--stream/--no-stream", help="实时显示 LLM token 流式输出（默认关闭）"
    ),
    force: bool = typer.Option(
        False, "--force", help="强制覆盖：若页面已存在则先归档旧页面再重建"
    ),
) -> None:
    """添加一篇论文：下载 PDF、写入 Notion、生成 AI 笔记。"""
    success = _process_paper(
        url, skip_llm=skip_llm, debug=debug, stream=stream, force=force
    )
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
    stream: bool = typer.Option(
        False, "--stream/--no-stream", help="实时显示 LLM token 流式输出（默认关闭）"
    ),
    force: bool = typer.Option(
        False, "--force", help="强制覆盖：若页面已存在则先归档旧页面再重建"
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
        success = _process_paper(
            url, skip_llm=skip_llm, debug=debug, stream=stream, force=force
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
    stream: bool = typer.Option(
        True, "--stream/--no-stream", help="实时显示 LLM 流式输出"
    ),
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
            answer = session.ask(question, debug=debug, stream=stream)
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
