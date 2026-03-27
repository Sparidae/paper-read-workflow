# paper-tool

通过 Arxiv / OpenReview 链接，自动完成：

1. 下载论文 PDF（Arxiv 同时下载 LaTeX 源码）到本地
2. 在 Notion 数据库中创建论文条目（标题、作者、摘要等）
3. LLM 自动分类（论文类型、研究领域、来源机构）
4. LLM 生成一句话摘要，写入 Notion Abstract 字段
5. 提取论文图片并翻译图注（仅 Arxiv，需 LaTeX 源码）
6. 调用大模型深度分析全文，生成结构化中文笔记写入 Notion
7. 支持基于论文全文的多轮问答（`chat` 命令）

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 可选：LaTeX（用于表格精确渲染）

表格默认使用 matplotlib 渲染。若要切换为方案 B（直接编译 LaTeX 源码，公式/合并单元格效果与原论文一致），需在系统中安装 LaTeX：

**Ubuntu / Debian：**

```bash
sudo apt install \
  texlive-latex-base \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-fonts-recommended \
  texlive-science
```

> 约 700 MB，覆盖 ML 论文常用宏包（`booktabs`、`amsmath`、`multirow`、`array`、`xcolor` 等）。  
> 已安装 `texlive-full` 的环境无需再单独安装。
>
> 安装 LaTeX 后，表格渲染会优先走 `pdflatex`。若某张表编译失败，会自动回退到 matplotlib，并把调试文件写到对应论文目录下的 `tables/debug/`。

## 服务器部署

### 1. 克隆项目

```bash
git clone <your-repo> ~/paper_list
cd ~/paper_list
```

### 2. 安装依赖

```bash
uv sync
```

### 3. 初始化配置文件

```bash
# 从模板复制本地配置
cp config.yaml.example config.yaml
cp .env.example .env
```

然后编辑 `config.yaml` 按需修改模型和 Notion 属性名称，编辑 `.env` 填入 API Keys（或使用交互式引导）：

```bash
uv run paper-tool config init
```

需要填写的配置：

| 配置项 | 说明 | 获取方式 |
|--------|------|----------|
| `NOTION_TOKEN` | Notion Integration Token | [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `NOTION_DATABASE_ID` | Notion 数据库 ID | 从数据库页面 URL 中提取 |
| `OPENAI_API_KEY` | OpenAI API Key | 使用 GPT-4o 等 OpenAI 模型时需要 |
| `OPENAI_BASE_URL` | OpenAI 兼容端点（可选） | DeepSeek / Kimi / 本地 vLLM 等 |
| `ANTHROPIC_API_KEY` | Anthropic API Key | 使用 Claude 时需要 |
| `GEMINI_API_KEY` | Google Gemini API Key | 使用 Gemini 时需要 |
| `OPENREVIEW_USERNAME` | OpenReview 账号（可选） | 需要登录才能下载的论文 |
| `OPENREVIEW_PASSWORD` | OpenReview 密码（可选） | 同上 |

### 4. 配置 Notion 数据库

在你的 Notion 数据库中，确保以下属性存在（名称可在 `config.yaml` 中修改）：

| 属性名（默认值） | 类型 | 说明 |
|--------|------|------|
| `Title` | title | 论文标题 |
| `Authors` | rich_text | 作者列表 |
| `Abstract` | rich_text | LLM 一句话摘要（自动覆盖原摘要） |
| `Source` | select | 来源平台（Arxiv / OpenReview） |
| `URL` | url | 论文链接 |
| `Published Date` | date | 发表日期 |
| `Added Date` | date | 添加日期 |
| `Tags` | multi_select | 研究领域标签（LLM 自动分类） |
| `Paper Type` | multi_select | 论文类型（Method / Benchmark 等，LLM 自动分类） |
| `Institution` | multi_select | 来源机构（LLM 自动分类） |
| `Status` | select 或 checkbox | 阅读状态（新添加默认为 Unread） |

> `Paper Type` 和 `Institution` 为可选字段；如果数据库中不存在，分类结果会静默跳过。

然后在数据库页面右上角点击「...」→「连接」→ 找到你的 Integration 并授权。

### 5. 验证配置

```bash
# 查看当前配置（API key 脱敏显示）
uv run paper-tool config show

# 检查 Notion 数据库属性是否与 config.yaml 匹配
uv run paper-tool config check-db
```

## 使用方法

### 在服务器上直接使用

```bash
# 添加一篇论文（完整流程：下载 + 分析 + 写入 Notion）
uv run paper-tool add "https://arxiv.org/abs/2301.00001"

# 只保存元数据，跳过 LLM 分析
uv run paper-tool add --skip-llm "https://arxiv.org/abs/2301.00001"

# 批量添加（urls.txt 每行一个链接，# 开头为注释）
uv run paper-tool batch urls.txt

# 调试模式：打印 LLM 原始 prompt 和返回内容
uv run paper-tool add --debug "https://arxiv.org/abs/2301.00001"

# 默认输出单行阶段进度条（无心跳刷屏）
# 如需看 LLM token 流式输出，再显式开启 --stream
uv run paper-tool add --stream "https://arxiv.org/abs/2301.00001"
# 显式关闭 LLM token 流式输出（默认即关闭）
uv run paper-tool add --no-stream "https://arxiv.org/abs/2301.00001"
```

### 与论文多轮问答

```bash
# 通过 Arxiv ID、关键词或文件路径指定论文
uv run paper-tool chat 2301.00001
uv run paper-tool chat "Attention Is All You Need"
uv run paper-tool chat papers/2301.00001_Attention/paper.tex
uv run paper-tool chat --stream 2301.00001
```

进入交互式会话后：
- 直接输入问题即可提问
- `/reset` — 清空对话历史（保留论文上下文）
- `/exit` — 退出

### 从本地电脑远程调用（SSH）

**方法一：使用脚本**

把 `scripts/add_paper.sh` 复制到本地电脑，修改其中的 `SERVER` 和 `PROJECT_DIR`：

```bash
# 复制到本地
scp your-server:~/paper_list/scripts/add_paper.sh ~/scripts/
chmod +x ~/scripts/add_paper.sh

# 使用
~/scripts/add_paper.sh "https://arxiv.org/abs/2301.00001"
```

**方法二：shell alias（更简洁）**

在本地 `~/.bashrc` 或 `~/.zshrc` 中添加：

```bash
alias add-paper='ssh your-server "cd ~/paper_list && uv run paper-tool add"'
```

然后：

```bash
source ~/.bashrc   # 或 ~/.zshrc
add-paper "https://arxiv.org/abs/2301.00001"
```

> 建议提前配置 SSH 密钥免密登录：`ssh-copy-id your-server`

## 切换 LLM 模型

编辑 `config.yaml`：

```yaml
llm:
  model: "openai/gpt-4o"                              # OpenAI GPT-4o（官方）
  stream_window: false                                # 开启后显示 LLM token 流式输出（与阶段进度条独立）
  stream_window_height: 8                             # 兼容旧版本保留字段
  # model: "openai/deepseek-chat"                     # DeepSeek（配合 OPENAI_BASE_URL）
  # model: "openai/moonshot-v1-8k"                    # Kimi（配合 OPENAI_BASE_URL）
  # model: "openai/qwen-max"                          # 通义千问（配合 OPENAI_BASE_URL）
  # model: "anthropic/claude-3-5-sonnet-20241022"     # Claude
  # model: "gemini/gemini-2.0-flash"                  # Gemini（支持超长文档）
  # model: "ollama/qwen2.5:72b"                       # 本地 Ollama
```

使用 OpenAI 兼容端点时，在 `.env` 中同时设置：

```bash
OPENAI_BASE_URL=https://api.deepseek.com/v1   # 以 DeepSeek 为例
```

## 自定义提示词

`prompts/` 目录下可覆盖内置的 LLM 提示词（`config.yaml` 中已默认配置）：

| 文件 | 用途 |
|------|------|
| `prompts/analyzer.md` | 全文笔记生成提示词 |
| `prompts/classifier.md` | 分类（类型 / 领域 / 机构）提示词 |
| `prompts/summarizer.md` | 一句话摘要提示词 |

修改后直接生效，无需重启。

### 表格渲染调试

如果你在检查 LaTeX 图表渲染是否生效，建议在 `config.yaml` 中开启：

```yaml
llm:
  rerender_figures: true
  rerender_tables: true
```

这样每次都会忽略已有的渲染缓存，重新渲染。调试文件会写到对应论文目录下的 `figures/debug/` 或 `tables/debug/`：

- `*.status.txt`：记录本次使用的是 `latex`、`file`、`matplotlib` 还是失败
- `*.latex.tex` / `*.latex.log`：LaTeX 失败时的编译输入和日志
- `*.fallback.txt`：标记该图/表已从 LaTeX 回退到 fallback 渲染

## 项目结构

```
paper_list/
  pyproject.toml              # 项目配置与依赖
  config.yaml                 # 行为配置（模型、路径、Notion 属性映射）
  .env                        # API Keys（不提交到 git）
  .env.example                # Keys 模板
  papers/                     # 下载的 PDF / LaTeX 文件（按论文子目录存放）
  prompts/
    analyzer.md               # 笔记生成提示词（可自定义）
    classifier.md             # 分类提示词（可自定义）
    summarizer.md             # 一句话摘要提示词（可自定义）
  scripts/
    add_paper.sh              # 本地 SSH 调用脚本
    create_test_db.py         # 创建测试用 Notion 数据库
    migrate_papers.py         # 旧版 flat 目录结构迁移工具
  src/paper_tool/
    cli.py                    # CLI 命令入口（add / batch / chat / config）
    config.py                 # 配置加载（config.yaml + .env）
    models.py                 # 数据结构定义（PaperMetadata / PaperNote 等）
    retry.py                  # API 调用自动重试装饰器
    downloaders/
      arxiv.py                # Arxiv 下载器（含 LaTeX 源码下载）
      openreview.py           # OpenReview 下载器
    pdf_parser.py             # PDF / LaTeX 文本提取
    figure_extractor.py       # LaTeX 图片提取与 PDF 转 PNG（仅 Arxiv）
    table_extractor.py        # LaTeX 表格提取与渲染为 PNG（仅 Arxiv）
    llm_analyzer.py           # LLM 全文笔记生成
    llm_classifier.py         # LLM 论文分类（类型 / 领域 / 机构）
    llm_summarizer.py         # LLM 一句话摘要生成
    llm_chat.py               # LLM 多轮问答会话
    notion_service.py         # Notion API 交互（建页 / 写笔记 / 上传图片）
```
