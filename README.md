# paper-tool

一个面向个人论文阅读流程的 CLI 工具：
- 从 Arxiv / OpenReview 拉取论文
- 下载 PDF（Arxiv 额外下载 LaTeX 源码）
- 在 Notion 数据库中创建论文页面
- 用 LLM 生成中文摘要、分类和结构化阅读笔记
- 提取论文中的核心图片 / 表格并插入到 Notion
- 支持基于论文内容的多轮问答
- 提供一个轻量 Web UI

README 以“先跑起来，再理解细节”为原则编写。你如果只想开始用，先看“快速开始”。

---

## 这个项目现在能做什么

### 1. 添加单篇论文

输入一个论文链接，自动完成：
1. 拉取元数据（标题、作者、摘要、日期等）
2. 下载 PDF 到本地 `papers/`
3. 在 Notion 数据库里创建页面
4. 检查重复论文；如已存在，可用 `--force` 归档旧页面后重建
5. 用 LLM 生成：
   - 一句话摘要
   - 论文分类（领域 / 类型 / 机构）
   - 结构化中文阅读笔记
6. 若是 Arxiv 论文，额外尝试：
   - 下载 LaTeX 源码
   - 提取论文图片
   - 提取论文表格并渲染为 PNG
   - 翻译图注 / 表注并写入 Notion

### 2. 批量导入论文

从任意文本文件中自动提取支持的论文链接并逐篇处理。

支持的输入文件不限格式，只要里面能匹配出 URL 即可，例如：
- `.txt`
- `.md`
- `.csv`

### 3. 与已下载论文多轮问答

可以按以下方式定位论文：
- Arxiv ID
- 标题关键词
- 本地文件路径

然后进入交互式聊天，针对论文内容连续提问。

### 4. 启动 Web UI

项目内置了一个基于 FastAPI 的轻量 Web 界面，可通过浏览器触发论文处理流程。

---

## 支持的数据源

### Arxiv

支持这些链接形式：
- `https://arxiv.org/abs/xxxx.xxxxx`
- `https://arxiv.org/pdf/xxxx.xxxxx`
- `https://alphaxiv.org/abs/xxxx.xxxxx`
- `https://ar5iv.labs.google.com/html/xxxx.xxxxx`
- `https://huggingface.co/papers/xxxx.xxxxx`
- 纯 ID，例如 `2301.00001`

Arxiv 是当前功能最完整的来源：
- PDF 下载
- LaTeX 源码下载
- 图片提取
- 表格提取与渲染

### OpenReview

支持 OpenReview forum / pdf 链接。

OpenReview 当前主要支持：
- 元数据获取
- PDF 下载
- Notion 写入
- LLM 摘要 / 分类 / 笔记 / chat

一般**不包含 Arxiv 那种基于 LaTeX 的图片 / 表格提取能力**。

---

## 快速开始

## 1. 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

安装依赖：

```bash
uv sync
```

> 本项目约定使用 `uv`，不要混用 `pip` / `poetry`。

## 2. 准备配置文件

项目运行依赖两个配置源：
- `config.yaml`：运行配置
- `.env`：密钥和数据库 ID

最简单的方式是：

```bash
cp config.yaml.example config.yaml
uv run paper-tool config init
```

其中 `config init` 会交互式生成 `.env`。

## 3. 最少需要填写什么

### `.env`

至少需要：
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- 至少一个可用的 LLM API Key

常见可选项：
- `NOTION_PARENT_PAGE_ID`（用于运行建库脚本时指定父页面）
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`（OpenAI 兼容接口，如 DeepSeek / Kimi / 本地 vLLM）
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `OPENREVIEW_USERNAME`
- `OPENREVIEW_PASSWORD`

### `config.yaml`

最重要的是：
- `llm.model`
- `storage.papers_dir`

Notion 数据库 schema 默认放在仓库里的 `notion_schema.yaml`，不是敏感信息；项目默认按这个模板工作。

## 4. 检查配置是否正确

```bash
uv run paper-tool config show
uv run paper-tool config check-db
uv run python scripts/create_notion_db.py
```

- `config show`：展示当前实际生效的配置（密钥会脱敏）
- `config check-db`：检查 Notion 数据库字段是否与 `notion_schema.yaml` 一致
- `scripts/create_notion_db.py`：按 `notion_schema.yaml` 创建一个新的 Notion 数据库

---

## Notion 数据库要求

默认字段映射来自仓库根目录的 `notion_schema.yaml`。

推荐至少有这些字段：

| 字段名（默认） | 类型 | 用途 |
|---|---|---|
| `论文笔记` | `title` | 论文标题 |
| `作者` | `rich_text` | 作者 |
| `一句话摘要` | `rich_text` | 一句话摘要 |
| `来源` | `select` | 来源（Arxiv / OpenReview） |
| `论文链接` | `url` | 原始链接 |
| `发表日期` | `date` | 发表日期 |
| `添加日期` | `date` | 导入日期 |
| `研究领域` | `multi_select` | 研究领域 |
| `论文类型` | `multi_select` | 论文类型 |
| `来源机构` | `multi_select` | 作者机构 |
| `阅读状态` | `checkbox` | 阅读状态 |

说明：
- `Paper Type` 和 `Institution` 不是强制字段；缺失时会跳过对应写入
- 当前默认模板里 `阅读状态` 使用 `checkbox`

记得把你的 Notion Integration 授权到目标数据库。

如果你还没有目标数据库：

```bash
uv run python scripts/create_notion_db.py --parent-page-id <your_notion_page_id>
```

也可以直接运行 `uv run paper-tool add <url>` 或 `batch`。CLI 会先做自检；如果数据库不存在或 schema 不匹配，会要求你先运行这个预置脚本建库。

---

## 常用命令

## 添加单篇论文

```bash
uv run paper-tool add "https://arxiv.org/abs/2301.00001"
```

常用参数：

```bash
# 只写元数据，不跑 LLM
uv run paper-tool add --skip-llm "https://arxiv.org/abs/2301.00001"

# 打印 LLM 原始 prompt / response，便于调试
uv run paper-tool add --debug "https://arxiv.org/abs/2301.00001"

# 在终端小窗口显示流式输出
uv run paper-tool add --stream "https://arxiv.org/abs/2301.00001"

# 如果 Notion 里已存在同 URL 论文，归档旧页面后重建
uv run paper-tool add --force "https://arxiv.org/abs/2301.00001"
```

## 批量导入

```bash
uv run paper-tool batch urls.txt
```

常用参数：

```bash
# 跳过 LLM
uv run paper-tool batch --skip-llm urls.txt

# 遇错继续
uv run paper-tool batch --continue-on-error urls.txt

# 调试 LLM
uv run paper-tool batch --debug urls.txt

# 对重复论文执行覆盖导入
uv run paper-tool batch --force urls.txt
```

## 与论文对话

```bash
uv run paper-tool chat 2301.00001
uv run paper-tool chat "Attention Is All You Need"
uv run paper-tool chat papers/2301.00001_Attention/paper.tex
```

交互命令：
- `/reset`：清空对话历史，但保留论文上下文
- `/exit`：退出

## 配置相关

```bash
uv run paper-tool config init
uv run paper-tool config show
uv run paper-tool config check-db
```

## Web UI

```bash
uv run paper-tool serve --host 127.0.0.1 --port 8000
```

启动后访问：

```text
http://127.0.0.1:8000
```

---

## 表格和图片提取的当前实际行为

这部分是仓库当前状态里最容易误解的地方，单独说明。

## 图片提取

- 主要面向 **Arxiv LaTeX 源码**
- 提取出的图片会写入论文目录下的 `figures/`
- 图注会走 LLM 翻译后写入 Notion

## 表格提取

- 主要面向 **Arxiv LaTeX 源码**
- 提取出的表格 PNG 会写入论文目录下的 `tables/`
- 表注不会画进图片，而是作为 caption 单独保留，便于写入 Notion

## 表格渲染策略

当前顺序是：
1. **优先使用 LaTeX 重绘**（`pdflatex`）
2. 若失败，**回退到 matplotlib**

这样做的目的：
- 尽量保留论文原始表格风格
- 更好地处理公式、multirow、多列布局等结构
- 回退路径仍然存在，不会因为单张表渲染失败导致整个流程中断

## 表格顺序修复

当前版本已经修复一个关键问题：
- 以前 `paper.tex` 是按 arXiv tar 包中文件出现顺序直接拼接
- 这会导致附录里的表跑到正文前面，被错误编号成 Table 1 / 2 / 3

现在改为：
- 优先识别主入口 tex
- 递归展开 `\input{}` / `\include{}`
- 按真实文档顺序生成 merged `paper.tex`
- 找不到入口时再回退到旧方案

所以现在的表格编号更接近论文真实顺序，修复点也只集中在下载合并层，没有把复杂逻辑扩散到下游。

## Notion 中图表标题的当前行为

当前版本已经去掉了每张图 / 表前面自动插入的伪标题（例如 `Figure 2`、`Table 3`）。

现在的 Notion 写入行为是：
- 保留章节级标题（如“论文核心图表”）
- 每张图 / 表直接以 image block 插入
- caption 仍然保留在图片 block 上

这可以避免污染 Notion 大纲。

---

## LaTeX 依赖说明

如果你希望表格尽可能按论文原样渲染，建议安装 LaTeX。

Ubuntu / Debian 参考：

```bash
sudo apt install \
  texlive-latex-base \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-fonts-recommended \
  texlive-science
```

安装后：
- 表格优先走 LaTeX 编译渲染
- 编译失败时自动回退到 matplotlib

如果没装 LaTeX：
- 仍可运行
- 但表格只能依赖 fallback，复杂表格效果可能较差

---

## 调试建议

当你在排查 LLM 或图表问题时，最有用的是下面这几个手段。

## 1. 打开 LLM 调试输出

```bash
uv run paper-tool add --debug "https://arxiv.org/abs/2301.00001"
```

适合查看：
- 分类 prompt
- 摘要 prompt
- 阅读笔记 prompt
- 模型原始返回

## 2. 强制重新渲染图表

在 `config.yaml` 中设置：

```yaml
llm:
  rerender_figures: true
  rerender_tables: true
```

这样可以忽略缓存，重新生成图片 / 表格。

## 3. 查看调试文件

调试文件通常在论文目录下：
- `figures/debug/`
- `tables/debug/`

常见文件：
- `*.status.txt`：本次使用的渲染后端
- `*.latex.tex`：LaTeX 渲染输入
- `*.latex.log`：LaTeX 编译日志
- `*.fallback.txt`：标记已回退到 fallback

---

## 目录结构

当前项目大致结构如下：

```text
paper_list/
├── src/paper_tool/
│   ├── cli.py                  # CLI 入口
│   ├── pipeline.py             # 主流程编排
│   ├── config.py               # config.yaml + .env 加载
│   ├── notion_service.py       # Notion 写入
│   ├── pdf_parser.py           # PDF / LaTeX 文本提取
│   ├── figure_extractor.py     # 图片提取
│   ├── table_extractor.py      # 表格提取与渲染
│   ├── llm_analyzer.py         # 阅读笔记生成
│   ├── llm_classifier.py       # 分类
│   ├── llm_summarizer.py       # 一句话摘要
│   ├── llm_chat.py             # 多轮问答
│   ├── server.py               # Web UI 服务
│   └── downloaders/
│       ├── arxiv.py            # Arxiv 下载器
│       └── openreview.py       # OpenReview 下载器
├── prompts/                    # 可自定义的提示词
├── scripts/                    # 辅助脚本
├── papers/                     # 下载后的论文与渲染产物
├── config.yaml.example         # 配置模板
├── pyproject.toml              # 项目依赖与脚本入口
└── README.md
```

一个 Arxiv 论文目录通常会包含：

```text
papers/<paper_id>_<title>/
├── paper.pdf
├── paper.tex           # 合并后的 tex
├── source/             # 原始 LaTeX 源码树
├── figures/
└── tables/
```

---

## 模型切换

项目通过 `litellm` 做模型抽象，通常只需要改 `config.yaml`：

```yaml
llm:
  model: "openai/gpt-4o"
```

常见示例：

```yaml
llm:
  model: "openai/gpt-4o"
  # model: "openai/deepseek-chat"
  # model: "openai/moonshot-v1-8k"
  # model: "openai/qwen-max"
  # model: "anthropic/claude-3-5-sonnet-20241022"
  # model: "gemini/gemini-2.0-flash"
  # model: "ollama/qwen2.5:72b"
```

如果是 OpenAI 兼容接口，记得在 `.env` 设置：

```bash
OPENAI_BASE_URL=https://your-endpoint/v1
```

---

## 自定义提示词

你可以直接修改 `prompts/` 目录下的提示词文件：
- `prompts/analyzer.md`
- `prompts/classifier.md`
- `prompts/summarizer.md`

对应路径在 `config.yaml` 中配置。

如果你想完全关闭结构化 JSON 笔记，改成自由文本，也可以在 `config.yaml` 中设置：

```yaml
llm:
  note_format: "freeform"
```

默认是：

```yaml
llm:
  note_format: "json"
```

---

## 常见问题

## 1. 为什么论文已存在时没有继续处理？

默认会按 URL 查重。若 Notion 数据库中已有相同 URL 的页面，工具会直接跳过。

如需重新导入：

```bash
uv run paper-tool add --force "<url>"
```

这会先归档旧页面，再创建新页面。

## 2. 为什么有些论文没有图片 / 表格？

常见原因：
- 不是 Arxiv 论文
- Arxiv 没有公开 LaTeX 源码
- 论文源码结构特殊，未提取到目标内容
- 表格 LaTeX 编译失败，且 fallback 也没能成功渲染

## 3. 为什么 `chat` 找不到论文？

`chat` 会在 `papers/` 里按文件名、目录名、ID、关键词做匹配。

如果找不到，直接传完整路径最稳妥：

```bash
uv run paper-tool chat papers/<paper_dir>/paper.tex
```

## 4. 这个项目有自动化测试吗？

目前没有完整测试套件。

当前更实际的验证方式是：
- 对真实论文运行 `add --debug`
- 检查 Notion 页面结果
- 必要时查看 `figures/debug/` 和 `tables/debug/`

---

## 开发说明

代码格式化使用 Ruff：

```bash
uv run ruff format src/
uv run ruff check --fix src/
```

提交信息格式约定：
- `[feat]...`
- `[fix]...`
- `[refactor]...`
- `[doc]...`
- `[chore]...`
- `[perf]...`
- `[style]...`

示例：

```text
[fix]修正LaTeX合并顺序
```

---

## 一句话总结

如果你想把“收集论文 → 下载 → 分析 → 归档到 Notion → 带图表阅读”这条链路尽量自动化，`paper-tool` 就是在做这件事。
