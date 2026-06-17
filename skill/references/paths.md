# Paths Reference

## Project Layout

仓库按 [Agent Skills](https://github.com/agentskills/agentskills) 规范分为「用户工作区」和「Skill 本体」两部分：

```text
<project_root>/                    # 用户工作区：数据 + 配置
├── config.yaml                    # 运行时配置
├── config.yaml.example            # 配置模板
├── .env                           # 密钥（NOTION_TOKEN、API keys）
├── .env.example                   # 密钥模板
├── README.md                      # 面向人类的使用说明
├── CLAUDE.md                      # agent 开发约定
├── papers/                        # 下载的论文（可通过 storage.papers_dir 配置）
│   └── <arxiv_id>_<title>/        # 单篇论文目录
│       ├── paper.pdf              # 下载的 PDF
│       ├── paper.tex              # 合并后的 LaTeX 源码（arxiv 才有）
│       ├── source/                # 原始 LaTeX 源码树（arxiv 才有）
│       ├── figures/               # 提取/渲染后的图片 PNG
│       ├── tables/                # 渲染后的表格 PNG
│       ├── debug/                 # 每张图/表的渲染日志与 JSON 状态
│       ├── metadata.json          # 论文元数据
│       ├── text.txt               # 提取的纯文本
│       ├── visuals.json           # 图/表清单
│       ├── classification.json    # LLM 分类结果
│       ├── summary.txt            # 一句话摘要
│       ├── captions.json          # 翻译后的图注/表注
│       └── notes.md               # 完整中文阅读笔记
│
└── skill/                         # Agent Skill 本体
    ├── SKILL.md                   # skill 元数据 + agent 指令
    ├── scripts/                   # 可执行脚本
    ├── references/                # 参考文档
    │   ├── latex-failure-patterns.md
    │   ├── notion-properties.md
    │   └── paths.md
    └── assets/                    # 模板/资源
        ├── prompts/               # LLM 提示词模板
        │   ├── analyzer.md
        │   ├── classifier.md
        │   ├── render_repair.md
        │   └── summarizer.md
        └── schema.yaml           # Notion 数据库属性定义
```

## Config File Reference

### config.yaml — all keys

```yaml
llm:
  max_input_tokens: 100000         # 超过此值截断论文文本
  max_output_tokens: 100000        # 笔记生成最大 token
  classifier_max_tokens: 8000      # 分类 token 预算
  temperature: 1                   # 0.0 = 确定性输出
  note_format: "freeform"          # "json" | "freeform"
  summarizer_max_tokens: 8000      # 摘要 token 预算
  translator_max_tokens: 16000     # 图注翻译 token 预算
  max_figures: 15                  # 每篇论文最多提取图片数
  rerender_figures: true           # 强制重新渲染（跳过缓存）
  max_tables: 10                   # 每篇论文最多提取表格数
  rerender_tables: true            # 强制重新渲染（跳过缓存）
  enable_render_repair: true       # 启用图表渲染自动修复循环
  enable_llm_render_repair: false  # 规则修复失败后启用 LLM 兜底
  repair_max_attempts: 3           # 每图/表最大修复尝试次数
  repair_model: ""                 # LLM 修复模型（留空复用 OPENAI_MODEL）
  repair_max_output_tokens: 16000  # LLM 修复最大输出 token
  analyzer_prompt: "skill/assets/prompts/analyzer.md"
  classifier_prompt: "skill/assets/prompts/classifier.md"
  summarizer_prompt: "skill/assets/prompts/summarizer.md"

storage:
  papers_dir: "papers"             # 相对项目根或绝对路径

notion:
  properties:                      # 逻辑名 → Notion 列名映射
    title: "论文笔记"
    authors: "作者"
    abstract: "一句话摘要"
    source: "来源"
    url: "论文链接"
    published_date: "发表日期"
    added_date: "添加日期"
    tags: "研究领域"
    paper_type: "论文类型"
    institution: "来源机构"
    status: "阅读状态"
  status_type: "checkbox"          # "select" | "checkbox"
```

### .env — all keys

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_TOKEN` | Yes | Notion Integration Token (`secret_xxx`) |
| `NOTION_DATABASE_ID` | Yes | 32-char hex database ID |
| `NOTION_PARENT_PAGE_ID` | No | 自动建库时的父页面 ID |
| `OPENAI_MODEL` | Yes | 模型名（如 `gpt-4o`） |
| `OPENAI_API_KEY` | Yes | API key |
| `OPENAI_BASE_URL` | No | 自定义端点（DeepSeek、Kimi 等） |
| `OPENAI_VISION_MODEL` | No | 多模态模型（默认复用文本模型） |
| `OPENAI_VISION_API_KEY` | No | 视觉 API key |
| `OPENAI_VISION_BASE_URL` | No | 视觉端点 |
| `OPENREVIEW_USERNAME` | No | OpenReview 用户名 |
| `OPENREVIEW_PASSWORD` | No | OpenReview 密码 |

## URL Format Support

| Source | URL Pattern |
|--------|------------|
| Arxiv | `arxiv.org/abs/<id>` |
| Arxiv PDF | `arxiv.org/pdf/<id>` |
| alphaXiv | `alphaxiv.org/abs/<id>` |
| ar5iv | `ar5iv.labs.google.com/abs/<id>` |
| HuggingFace | `huggingface.co/papers/<id>` |
| OpenReview | `openreview.net/forum?id=<id>` |
| Bare ID | `2301.12345` (auto-detected as arxiv) |

## Config Search Order

1. `config.yaml` 和 `.env` 从 **cwd 向上查找**（最多 10 层）。
2. 脚本通过 `find_project_root()` 定位到项目根目录后，再相对定位 `skill/assets/prompts/` 和 `backends/notion/schema.yaml`。
3. 若 `config.yaml` 不存在，可用 `config.yaml.example` 作为模板手动复制。
