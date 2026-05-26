# Response: Adding Paper https://arxiv.org/abs/2301.12345 to Notion

好的，我来帮你把这篇论文添加到 Notion。最简单的做法是直接用 `run_pipeline()` 一步完成：

```python
from paper_tool.pipeline import run_pipeline

success = run_pipeline("https://arxiv.org/abs/2301.12345")
```

这个函数内部会自动执行完整的流程：下载 PDF、提取文本和图表、LLM 分类/总结/精读分析、写 Notion 页面。完成后 Notion 里会有一篇结构化的阅读笔记，包含：

- 论文元数据（标题、作者、年份、链接）
- LLM 生成的分类标签（领域、方法、贡献类型等）
- 一句话总结
- 完整的阅读笔记（背景、方法、贡献、实验、局限性等）
- 图表（Figure 和 Table 以图片形式嵌入）

如果需要更多控制（比如跳过某些步骤、使用自定义参数），可以按 step-by-step 方式直接调用各个函数：

```python
from paper_tool.config import PipelineContext
from paper_tool.pipeline import download_paper, extract_paper_text
from paper_tool.llm_classifier import LLMClassifier
from paper_tool.llm_summarizer import LLMSummarizer
from paper_tool.llm_analyzer import LLMAnalyzer, translate_captions
from paper_tool.notion_service import NotionService

ctx = PipelineContext.from_config()

# 1. 下载
result = download_paper("https://arxiv.org/abs/2301.12345", ctx.papers_dir)

# 2. 提取文本和图表
ext = extract_paper_text(
    result.downloader, result.metadata, result.pdf_path, ctx.papers_dir,
    max_input_tokens=ctx.llm_max_input_tokens,
    max_figures=ctx.max_figures,
    max_tables=ctx.max_tables,
)

# 3. 创建 Notion 页面
notion = NotionService.from_context(ctx)
page_id = notion.create_page(result.metadata)

# 4. 分类
classifier = LLMClassifier.from_context(ctx)
options = notion.get_classification_options()
classification = classifier.classify(result.metadata, options)
notion.update_classifications(page_id, classification)

# 5. 总结
summarizer = LLMSummarizer.from_context(ctx)
summary = summarizer.summarize(result.metadata)
notion.update_summary(page_id, summary)

# 6. 翻译图表标题
if ext.figures:
    ext.figures = translate_captions(ext.figures, model=ctx.llm_model)

# 7. LLM 精读生成笔记
analyzer = LLMAnalyzer.from_context(ctx)
note = analyzer.analyze(result.metadata, ext.paper_text,
                        figures=ext.figures, tables=ext.tables)

# 8. 写回 Notion（含图表）
notion.append_note_with_figures(page_id, note, ext.figures, ext.tables)
```

要我现在执行 `run_pipeline()` 吗？还是你想调整某些参数（比如跳过 LLM 分析、指定自定义 prompt 等）？
