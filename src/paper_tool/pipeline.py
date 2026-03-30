"""
Core paper-processing pipeline — pure logic, no terminal UI.

All progress and LLM output is reported via the on_event callback so that
callers (CLI Rich adapter, web WebSocket adapter, tests) can decide how to
present the information.

Event schema
------------
{"type": "stage_start", "stage": str,   "label": str}
{"type": "stage_done",  "stage": str,   "label": str,  "status": "ok"|"warn"}
{"type": "llm_start",   "title": str}
{"type": "llm_token",   "text":  str}
{"type": "llm_end"}
{"type": "done",        "page_url": str}
{"type": "error",       "message": str}
"""

from __future__ import annotations

from typing import Callable


def run_pipeline(
    url: str,
    *,
    skip_llm: bool = False,
    debug: bool = False,
    force: bool = False,
    on_event: Callable[[dict], None] | None = None,
    on_confirm_force: Callable[[str], bool] | None = None,
) -> bool:
    """
    Download, analyse and write a paper to Notion.

    Parameters
    ----------
    on_event:
        Called for every pipeline event (stage progress, LLM tokens, errors …).
        Safe to be None — all events are silently dropped.
    on_confirm_force:
        Called when force=True and duplicate pages are found, with a human-
        readable prompt string.  Return True to proceed, False to abort.
        Defaults to auto-confirm (always True) when not provided.

    Returns True on success, False on any hard failure.
    """
    from paper_tool.config import get_config
    from paper_tool.downloaders import get_downloader
    from paper_tool.llm_analyzer import LLMAnalyzer, translate_captions
    from paper_tool.llm_classifier import LLMClassifier
    from paper_tool.llm_summarizer import LLMSummarizer
    from paper_tool.notion_service import NotionService
    from paper_tool.pdf_parser import extract_text

    emit = on_event or (lambda _: None)
    confirm = on_confirm_force or (lambda _msg: True)

    def _on_token(text: str) -> None:
        emit({"type": "llm_token", "text": text})

    url = url.strip()
    cfg = get_config()

    # ── Step 1: Fetch metadata ─────────────────────────────────────────────
    emit(
        {"type": "stage_start", "stage": "fetch_metadata", "label": "获取论文元数据..."}
    )
    try:
        downloader = get_downloader(url)
        metadata = downloader.fetch_metadata(url)
        emit(
            {
                "type": "stage_done",
                "stage": "fetch_metadata",
                "label": f"元数据获取成功: {metadata.title[:60]}",
                "status": "ok",
            }
        )
    except Exception as e:
        emit({"type": "error", "message": f"元数据获取失败: {e}"})
        return False

    # ── Step 2: Check for duplicates ───────────────────────────────────────
    emit(
        {
            "type": "stage_start",
            "stage": "check_duplicate",
            "label": "检查 Notion 中是否已存在...",
        }
    )
    try:
        notion = NotionService()
        existing_ids = notion.find_existing_pages(metadata.url)
        if existing_ids:
            if not force:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "check_duplicate",
                        "label": "论文已存在于 Notion，跳过",
                        "status": "warn",
                    }
                )
                return True

            msg = (
                f"检测到 {len(existing_ids)} 个同 URL 页面。"
                f"\n示例页面: {notion.get_page_url(existing_ids[0])}"
                "\n确认归档这些旧页面并覆盖导入吗？"
            )
            if not confirm(msg):
                emit(
                    {
                        "type": "stage_done",
                        "stage": "check_duplicate",
                        "label": "已取消 --force 覆盖，跳过该论文",
                        "status": "warn",
                    }
                )
                return True

            for page_id in existing_ids:
                notion.archive_page(page_id)
            emit(
                {
                    "type": "stage_done",
                    "stage": "check_duplicate",
                    "label": f"已归档 {len(existing_ids)} 个旧页面",
                    "status": "ok",
                }
            )
        else:
            emit(
                {
                    "type": "stage_done",
                    "stage": "check_duplicate",
                    "label": "未重复，继续处理",
                    "status": "ok",
                }
            )
    except Exception as e:
        emit({"type": "error", "message": f"Notion 重复检查失败: {e}"})
        return False

    # ── Step 3: Download PDF ───────────────────────────────────────────────
    emit({"type": "stage_start", "stage": "download_pdf", "label": "下载 PDF..."})
    try:
        pdf_path = downloader.download_pdf(metadata, cfg.papers_dir)
        metadata.pdf_path = str(pdf_path)
        emit(
            {
                "type": "stage_done",
                "stage": "download_pdf",
                "label": f"PDF 已保存: {pdf_path.name}",
                "status": "ok",
            }
        )
    except Exception as e:
        emit({"type": "error", "message": f"PDF 下载失败: {e}"})
        return False

    # ── Step 4: Extract text ───────────────────────────────────────────────
    from paper_tool.downloaders.arxiv import ArxivDownloader
    from paper_tool.pdf_parser import extract_text_from_latex

    char_budget = cfg.llm_max_input_tokens * 4
    paper_text: str | None = None
    tex_path = None

    if isinstance(downloader, ArxivDownloader):
        emit(
            {
                "type": "stage_start",
                "stage": "download_latex",
                "label": "下载 LaTeX 源码...",
            }
        )
        try:
            tex_path = downloader.download_latex_source(metadata, cfg.papers_dir)
            if tex_path:
                paper_text = extract_text_from_latex(tex_path, max_chars=char_budget)
                emit(
                    {
                        "type": "stage_done",
                        "stage": "download_latex",
                        "label": f"LaTeX 源码解析完成 {len(paper_text):,} 字符",
                        "status": "ok",
                    }
                )
            else:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "download_latex",
                        "label": "无 LaTeX 源码，降级到 PDF 解析",
                        "status": "warn",
                    }
                )
        except Exception as e:
            emit(
                {
                    "type": "stage_done",
                    "stage": "download_latex",
                    "label": f"LaTeX 解析失败，降级到 PDF: {e}",
                    "status": "warn",
                }
            )

    if paper_text is None:
        emit(
            {
                "type": "stage_start",
                "stage": "extract_text",
                "label": "提取 PDF 文本...",
            }
        )
        try:
            paper_text = extract_text(pdf_path, max_chars=char_budget)
            emit(
                {
                    "type": "stage_done",
                    "stage": "extract_text",
                    "label": f"PDF 文本提取完成 {len(paper_text):,} 字符",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "error", "message": f"PDF 文本提取失败: {e}"})
            return False

    pdf_text = paper_text

    # ── Step 5: Create Notion page ─────────────────────────────────────────
    emit(
        {
            "type": "stage_start",
            "stage": "create_notion_page",
            "label": "创建 Notion 页面...",
        }
    )
    try:
        page_id = notion.create_page(metadata)
        emit(
            {
                "type": "stage_done",
                "stage": "create_notion_page",
                "label": "Notion 页面创建成功",
                "status": "ok",
            }
        )
    except Exception as e:
        emit({"type": "error", "message": f"创建 Notion 页面失败: {e}"})
        return False

    # ── Step 6a: LLM classification ────────────────────────────────────────
    if not skip_llm:
        emit(
            {
                "type": "stage_start",
                "stage": "llm_classify",
                "label": "LLM 分类标注中...",
            }
        )
        try:
            available_options = notion.get_classification_options()
            classifier = LLMClassifier()
            emit({"type": "llm_start", "title": "LLM 分类标注"})
            classification = classifier.classify(
                metadata,
                available_options,
                debug=debug,
                on_token=_on_token,
            )
            emit({"type": "llm_end"})
            notion.update_classifications(page_id, classification)

            new_tags = (
                [
                    t
                    for t in classification.paper_type
                    if t not in available_options.get("paper_type", [])
                ]
                + [
                    t
                    for t in classification.research_areas
                    if t not in available_options.get("research_areas", [])
                ]
                + [
                    t
                    for t in classification.institutions
                    if t not in available_options.get("institutions", [])
                ]
            )
            suffix = f"  [新增: {', '.join(new_tags)}]" if new_tags else ""
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_classify",
                    "label": f"分类完成{suffix}",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_classify",
                    "label": f"分类失败（已跳过）: {e}",
                    "status": "warn",
                }
            )

    # ── Step 6c: LLM one-sentence summary ─────────────────────────────────
    if not skip_llm:
        emit(
            {
                "type": "stage_start",
                "stage": "llm_summarize",
                "label": "LLM 生成一句话摘要...",
            }
        )
        try:
            summarizer = LLMSummarizer()
            emit({"type": "llm_start", "title": "LLM 一句话摘要"})
            summary = summarizer.summarize(metadata, debug=debug, on_token=_on_token)
            emit({"type": "llm_end"})
            notion.update_summary(page_id, summary)
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_summarize",
                    "label": "一句话摘要写入完成",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_summarize",
                    "label": f"一句话摘要生成失败（已跳过）: {e}",
                    "status": "warn",
                }
            )

    # ── Step 6b-pre: Extract figures and tables ────────────────────────────
    figures: list = []
    tables: list = []
    if isinstance(downloader, ArxivDownloader) and tex_path is not None:
        from paper_tool.figure_extractor import convert_pdf_figures, parse_figures
        from paper_tool.table_extractor import parse_tables

        emit(
            {
                "type": "stage_start",
                "stage": "extract_figures",
                "label": "提取论文图片...",
            }
        )
        try:
            figures_dir = downloader.get_figures_dir(metadata, cfg.papers_dir)
            convert_pdf_figures(figures_dir)
            figures = parse_figures(
                tex_path,
                figures_dir,
                max_figures=cfg.max_figures,
                force_rerender=cfg.rerender_figures,
            )
            if figures:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "extract_figures",
                        "label": f"找到 {len(figures)} 张图片",
                        "status": "ok",
                    }
                )
            else:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "extract_figures",
                        "label": "未找到可用图片",
                        "status": "warn",
                    }
                )
        except Exception as e:
            emit(
                {
                    "type": "stage_done",
                    "stage": "extract_figures",
                    "label": f"图片提取失败（已跳过）: {e}",
                    "status": "warn",
                }
            )
            figures = []

        emit(
            {
                "type": "stage_start",
                "stage": "extract_tables",
                "label": "提取并渲染论文表格...",
            }
        )
        try:
            tables_dir = (
                downloader.get_figures_dir(metadata, cfg.papers_dir).parent / "tables"
            )
            tables = parse_tables(
                tex_path,
                tables_dir,
                max_tables=cfg.max_tables,
                force_rerender=cfg.rerender_tables,
            )
            if tables:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "extract_tables",
                        "label": f"找到 {len(tables)} 张表格",
                        "status": "ok",
                    }
                )
            else:
                emit(
                    {
                        "type": "stage_done",
                        "stage": "extract_tables",
                        "label": "未找到可用表格",
                        "status": "warn",
                    }
                )
        except Exception as e:
            emit(
                {
                    "type": "stage_done",
                    "stage": "extract_tables",
                    "label": f"表格提取失败（已跳过）: {e}",
                    "status": "warn",
                }
            )
            tables = []

    if not skip_llm and figures:
        emit(
            {
                "type": "stage_start",
                "stage": "translate_figures",
                "label": "翻译图片说明...",
            }
        )
        try:
            emit({"type": "llm_start", "title": "LLM 翻译图片说明"})
            figures = translate_captions(
                figures,
                stream_title="LLM 流式输出 · 图片说明翻译",
                on_token=_on_token,
            )
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "translate_figures",
                    "label": "图片说明翻译完成",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "translate_figures",
                    "label": f"翻译失败（保留原文）: {e}",
                    "status": "warn",
                }
            )

    if not skip_llm and tables:
        emit(
            {
                "type": "stage_start",
                "stage": "translate_tables",
                "label": "翻译表格说明...",
            }
        )
        try:
            emit({"type": "llm_start", "title": "LLM 翻译表格说明"})
            tables = translate_captions(
                tables,
                stream_title="LLM 流式输出 · 表格说明翻译",
                on_token=_on_token,
            )
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "translate_tables",
                    "label": "表格说明翻译完成",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "translate_tables",
                    "label": f"翻译失败（保留原文）: {e}",
                    "status": "warn",
                }
            )

    # ── Step 6b: LLM note generation ──────────────────────────────────────
    note = None
    if not skip_llm:
        emit(
            {
                "type": "stage_start",
                "stage": "llm_analyze",
                "label": f"LLM 生成笔记 ({cfg.llm_model})...",
            }
        )
        try:
            analyzer = LLMAnalyzer()
            emit({"type": "llm_start", "title": f"LLM 生成笔记 ({cfg.llm_model})"})
            note = analyzer.analyze(
                metadata,
                pdf_text,
                debug=debug,
                figures=figures if figures else None,
                tables=tables if tables else None,
                on_token=_on_token,
            )
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_analyze",
                    "label": "笔记生成完成",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit({"type": "llm_end"})
            emit(
                {
                    "type": "stage_done",
                    "stage": "llm_analyze",
                    "label": f"笔记生成失败（已跳过）: {e}",
                    "status": "warn",
                }
            )

    # ── Step 7: Write to Notion ────────────────────────────────────────────
    has_visuals = bool(figures or tables)
    if note is not None:
        emit(
            {
                "type": "stage_start",
                "stage": "write_notion",
                "label": "将笔记和图表写入 Notion...",
            }
        )
        try:
            if has_visuals:
                n = notion.append_note_with_figures(page_id, note, figures, tables)
                total_vis = len(figures) + len(tables)
                emit(
                    {
                        "type": "stage_done",
                        "stage": "write_notion",
                        "label": f"笔记写入完成，上传 {n}/{total_vis} 张图表",
                        "status": "ok",
                    }
                )
            else:
                notion.append_note(page_id, note)
                emit(
                    {
                        "type": "stage_done",
                        "stage": "write_notion",
                        "label": "笔记写入完成",
                        "status": "ok",
                    }
                )
        except Exception as e:
            emit(
                {
                    "type": "stage_done",
                    "stage": "write_notion",
                    "label": f"笔记写入失败: {e}",
                    "status": "warn",
                }
            )
            emit({"type": "error", "message": f"笔记写入失败: {e}"})
            return False
    elif has_visuals:
        emit(
            {
                "type": "stage_start",
                "stage": "upload_visuals",
                "label": "上传论文图表...",
            }
        )
        try:
            n = notion.append_figures(page_id, figures + tables)
            total_vis = len(figures) + len(tables)
            emit(
                {
                    "type": "stage_done",
                    "stage": "upload_visuals",
                    "label": f"上传 {n}/{total_vis} 张图表完成",
                    "status": "ok",
                }
            )
        except Exception as e:
            emit(
                {
                    "type": "stage_done",
                    "stage": "upload_visuals",
                    "label": f"图表上传失败: {e}",
                    "status": "warn",
                }
            )
            emit({"type": "error", "message": f"图表上传失败: {e}"})
            return False

    page_url = notion.get_page_url(page_id)
    emit({"type": "done", "page_url": page_url})
    return True
