---
name: commit
description: 提交当前未 commit 的修改。自动分析变更内容，按主题分组，生成符合项目规范的 commit message。
---

# Git Commit 技能

## 步骤一：查看未提交修改

```bash
git status --short
```

## 步骤二：分析变更类型

根据文件路径判断变更归属：

| 路径 | commit 类型 |
|------|-------------|
| `src/` | `[feat]` / `[fix]` / `[refactor]` / `[perf]` |
| `prompts/` | `[chore]` |
| `scripts/` | `[chore]` / `[feat]` |
| `CLAUDE.md`、`README.md` | `[doc]` |
| `pyproject.toml`、`uv.lock` | `[chore]` |
| `.claude/` | `[chore]` |

## 步骤三：提交前格式检查

如果变更包含 `.py` 文件，先执行：

```bash
uv run ruff format src/ && uv run ruff check --fix src/
```

## 步骤四：决定提交策略

- **单一主题**：一次性提交所有文件
- **多主题**：按以上分组分批提交，每批一个 commit

## 步骤五：生成 Commit Message

格式：`[type]中文描述`，不超过 50 字。

参考 CLAUDE.md 中的类型定义：
- `[feat]` 新功能
- `[fix]` 问题修复
- `[refactor]` 重构
- `[doc]` 文档
- `[chore]` 构建/依赖/配置等杂项
- `[perf]` 性能优化
- `[style]` 格式调整（不影响逻辑）

## 步骤六：执行提交

明确指定文件，不用 `git add .`：

```bash
git add <具体文件>
git commit -m "[type]描述"
```

排除不提交的文件：`.env`、`config.yaml`、`papers/`、`__pycache__/`、`*.pyc`

## 步骤七：确认结果

```bash
git log --oneline -3
```
