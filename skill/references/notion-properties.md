# Notion Database Properties

How `backends/notion/schema.yaml` maps to what `notion_service.py` writes.

## Schema Definition

```yaml
# backends/notion/schema.yaml
title: "Papers"
properties:
  论文笔记:    # title property (page title)
    type: title
  作者:        # authors
    type: rich_text
  一句话摘要:  # one-sentence summary
    type: rich_text
  来源:        # source (Arxiv / OpenReview)
    type: select
  论文链接:    # paper URL
    type: url
  发表日期:    # publication date
    type: date
  添加日期:    # date added to Notion
    type: date
  研究领域:    # research area tags
    type: multi_select
  论文类型:    # paper type (survey, method, etc.)
    type: multi_select
  来源机构:    # institution
    type: multi_select
  引用量:      # citation count
    type: number
  阅读状态:    # reading status (checkbox or select)
    type: checkbox
```

## Property Mapping

The `config.yaml` `notion.properties` section maps logical keys to actual Notion column names. This allows using Chinese column names while the code references logical keys:

| Logical Key | Default Notion Name | Type | Written By |
|-------------|-------------------|------|------------|
| `title` | 论文笔记 | title | `create_page()` |
| `authors` | 作者 | rich_text | `create_page()` |
| `abstract` | 一句话摘要 | rich_text | `update_summary()` |
| `source` | 来源 | select | `create_page()` |
| `url` | 论文链接 | url | `create_page()` |
| `published_date` | 发表日期 | date | `create_page()` |
| `added_date` | 添加日期 | date | `create_page()` |
| `tags` | 研究领域 | multi_select | `update_classifications()` |
| `paper_type` | 论文类型 | multi_select | `update_classifications()` |
| `institution` | 来源机构 | multi_select | `update_classifications()` |
| `status` | 阅读状态 | checkbox/select | `create_page()` |
| — | 引用量 | number | `update_citation_count()` |

## Note Content Structure

### Freeform Mode (`note_format: "freeform"`)

The LLM's raw Markdown output is converted to Notion blocks:
- `# Heading` → heading_1 block
- `## Heading` → heading_2 block
- `### Heading` → heading_3 block
- `- list item` → bulleted_list_item block
- `---` → divider block
- `$$...$$` → equation block (LaTeX)
- `[FIGURE:N]` / `[TABLE:N]` → image block (inline placement)
- `**bold**`, `*italic*`, `` `code` ``, `$math$` → inline rich text annotations
- `[text](url)` → inline link

Unreferenced figures/tables are appended at the end under "论文核心图表" / "论文核心表格" sections.

### JSON Mode (`note_format: "json"`)

The LLM outputs structured JSON parsed into `PaperNote`:
- `overview` → H2 "概述" section
- `research_problem` → H2 "研究问题" section
- `methodology` → H2 "方法" section
- `contributions` → H2 "贡献" bullet list
- `experiments` → H2 "实验" section
- `limitations` → H2 "局限性" section
- `key_takeaways` → H2 "关键收获" bullet list

## Figure/Table Upload Flow

1. `_prepare_image_for_upload(image_path)` — flattens transparent background to white (Notion compatibility)
2. `POST /v1/file_uploads` — get signed upload URL
3. `POST multipart` to upload URL — upload the image
4. Return `file_upload_id` — embedded in image blocks

## Classification Options

`LLMClassifier` fetches existing select/multi_select options from the Notion database before classifying. The LLM is instructed to prefer existing options but may propose new ones. New options are differentiated from existing via `selected` vs `new` fields in the classification JSON.
