# paper-read-workflow — Agent Instructions

本文件分两部分：
- **开发指南**：你在这个仓库里写代码时要遵守的约定。
- **运行指南**：当用户给出论文相关指令时，你应该去哪里找 skill 的运行方式。

---

## 一、开发指南

### 核心原则：面向 Agent 设计，而非人工手动运行

本仓库的所有脚本、输入输出格式和运行流程都是**为 agent 消费而设计**，不是为人类在终端里手动敲命令而设计。新增或修改代码时遵循以下约定：

- **脚本必须是可编排的单元**：每个脚本只做一件事，参数通过 CLI 显式传入，不依赖交互式 `input()` 或终端提示。
- **输出必须是结构化 JSON**：所有脚本通过 stdout 输出 `{"status": "ok"|"error", ...}`，便于 agent 解析并决定下一步动作。
- **错误必须可程序化处理**：错误输出中应包含机器可读的字段（如 `backend`、`missing`、`hint`），agent 能据此自动修复或向用户提问。
- **不要假设有 TTY**：脚本可能在非交互环境中运行，避免依赖 `isatty()` 控制核心逻辑；交互式引导只作为缺失配置时的最后兜底。
- **配置应可被 agent 自动写入**：当配置缺失时，脚本要给出明确的字段清单，由 agent 调用 `AskUserQuestion` 收集并持久化，而不是让用户手动编辑文件。
- **状态变更必须显式**：任何写入文件、数据库、第三方服务的操作，都应在输出中明确报告（如 `images_uploaded`、`doc_id`、`pages`）。

### 包管理器

- 统一使用 `uv`。
- 禁止混用 `pip` / `poetry`。
- 安装依赖：`uv sync`（如果存在 `pyproject.toml`）或直接依赖脚本顶部的 PEP 723 inline metadata。
- 当前仓库的脚本使用 PEP 723 inline metadata 声明依赖，**不要**为了引入依赖而创建 `pyproject.toml`。

### Python 版本

- Python 3.12+（见 `.python-version`）。

### 代码风格

- 使用 Ruff 进行格式化和检查。
- 行长度：88。
- 提交前必须运行：
  ```bash
  uv run ruff check skill/scripts/
  uv run ruff format skill/scripts/
  ```

### 提交约定

- 提交信息格式：
  ```text
  [feat]...
  [fix]...
  [refactor]...
  [doc]...
  [chore]...
  [perf]...
  [style]...
  ```
  示例：`[fix]修复arxiv ID解析错误`
- **完成一个完整功能更改后必须提交**。不要在一个任务中积累大量未提交的修改；每个可独立工作的改动点都应单独提交。
- 提交前确保代码可运行，并且相关脚本已通过 ruff 检查。

### 项目结构

```text
paper-read-workflow/          # 用户工作区
├── papers/                   # 论文数据（不要移动）
├── config.yaml               # 运行时配置
├── config.yaml.example       # 配置模板
├── .env                      # 密钥
├── .env.example              # 密钥模板
├── backends/                 # 输出后端配置
│   ├── notion/
│   │   ├── backend.yaml      # Notion 认证与属性映射
│   │   └── schema.yaml       # Notion 数据库字段定义
│   └── lark/
│       └── backend.yaml      # 飞书认证与文档创建位置
├── README.md                 # 人类使用说明
├── CLAUDE.md                 # 旧版 agent 约定（保留参考）
├── AGENTS.md                 # 本文件
└── skill/                    # Agent Skill 本体
    ├── SKILL.md              # skill 元数据 + 完整运行指令
    ├── scripts/              # 可执行脚本
    ├── references/           # 参考文档
    └── assets/               # 模板/资源
        └── prompts/          # LLM 提示词
```

### 测试

- 当前没有测试套件。
- 验证方式：对真实论文运行 `uv run skill/scripts/extract_visuals.py <paper-dir> --repair`。
- 修改表格/图片渲染相关代码后，必须在至少一个已知成功和一个已知失败的论文上验证，避免回归。

### 兼容性要求

- 表格渲染修复必须保持向后兼容，不能破坏之前能成功渲染的论文。
- 任何对 `_figure_extractor.py` / `_table_extractor.py` / `_render_repair.py` 的修改，都要确认 fallback 路径仍然可用。
- 不要改变 `visuals.json` 的输出格式，下游 `notion_write.py` / `lark_write.py` 依赖它。

### 不要做的事情

- 不要创建 `pyproject.toml`。
- 不要把 `papers/` 移进 `skill/`。
- 不要把用户的 `config.yaml` / `.env` 移进 `skill/`。
- 不要修改 `SKILL.md` 的顶层元数据块（`---` 之间的 `name` / `description`），除非明确要求。

---

## 二、运行指南

当用户给出任何与论文相关的指令时，**第一步读取 `skill/SKILL.md`**。所有具体决策流程、脚本调用顺序和参数说明都在其中。

本文件不再重复 SKILL.md 中的内容，只保留执行前的快速检查清单。

---

## 三、快速检查清单

执行任何论文任务前，确认：

- [ ] 已读 `skill/SKILL.md`
- [ ] `config.yaml` 存在且 `llm.model` / `storage.papers_dir` / `output.backends` 已配置
- [ ] 对应输出后端的 `backends/<name>/backend.yaml` 已配置，或准备让 skill 自动引导用户填写
- [ ] `.env` 中存在 LLM API Key（`OPENAI_API_KEY`）
- [ ] 脚本路径使用 `skill/scripts/xxx.py`，不是根目录的 `scripts/xxx.py`

### 后端配置自动引导

当 `notion_check.py` / `notion_write.py` / `lark_write.py` 因为缺少配置而失败时：

1. 读取错误输出中的 `backend` 和 `missing` 字段。
2. 使用 `AskUserQuestion` 向用户逐项询问缺失的字段。
3. 将用户回答写入 `.env`（推荐）或 `backends/<name>/backend.yaml`。
4. 重新运行失败的脚本。

如果当前处于无法交互的模式，至少输出清晰错误信息并停止，不要猜测或编造 token。
