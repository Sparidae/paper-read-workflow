# paper-tool

通过 Arxiv / OpenReview 链接，自动完成：
1. 下载论文 PDF 到本地
2. 在 Notion 数据库中创建论文条目（标题、作者、摘要等）
3. 调用大模型深度分析论文，生成结构化中文笔记写入 Notion

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

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
| `OPENAI_API_KEY` | OpenAI API Key | 使用 GPT-4o 时需要 |
| `ANTHROPIC_API_KEY` | Anthropic API Key | 使用 Claude 时需要 |
| `GEMINI_API_KEY` | Google Gemini API Key | 使用 Gemini 时需要 |

### 4. 配置 Notion 数据库

在你的 Notion 数据库中，确保以下属性存在（名称可在 `config.yaml` 中修改）：

| 属性名 | 类型 |
|--------|------|
| Title | title |
| Authors | rich_text |
| Abstract | rich_text |
| Source | select |
| URL | url |
| Published Date | date |
| Added Date | date |
| Tags | multi_select |
| Status | select |

然后在数据库页面右上角点击「...」→「连接」→ 找到你的 Integration 并授权。

### 5. 验证配置

```bash
uv run paper-tool config show
```

## 使用方法

### 在服务器上直接使用

```bash
# 添加一篇论文
uv run paper-tool add "https://arxiv.org/abs/2301.00001"

# 只保存元数据，跳过 AI 分析
uv run paper-tool add --skip-llm "https://arxiv.org/abs/2301.00001"

# 批量添加（urls.txt 每行一个链接，# 开头为注释）
uv run paper-tool batch urls.txt
```

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
  model: "openai/gpt-4o"           # OpenAI GPT-4o
  # model: "anthropic/claude-3-5-sonnet-20241022"  # Claude
  # model: "gemini/gemini-2.0-flash"               # Gemini（支持超长文档）
  # model: "ollama/qwen2.5:72b"                    # 本地 Ollama
```

## 项目结构

```
paper_list/
  pyproject.toml         # 项目配置与依赖
  config.yaml            # 行为配置（模型、路径、Notion 属性映射）
  .env                   # API Keys（不提交到 git）
  .env.example           # Keys 模板
  papers/                # 下载的 PDF 文件
  scripts/
    add_paper.sh         # 本地 SSH 调用脚本
  src/paper_tool/
    cli.py               # CLI 命令入口
    config.py            # 配置加载
    models.py            # 数据结构定义
    downloaders/         # Arxiv / OpenReview 下载器
    pdf_parser.py        # PDF 文本提取
    llm_analyzer.py      # LLM 分析与笔记生成
    notion_service.py    # Notion API 交互
```
