# 表格 LaTeX 渲染降级到 matplotlib 的调试方法

## 问题本质

表格显示为 matplotlib 版本，说明 pdflatex 编译失败了。`table_extractor.py` 的流程是：先尝试 pdflatex 编译（两次），如果都失败，且有 `has_matplotlib_fallback` 可用，就回退到 matplotlib 渲染表格。这就是你看到的 "降级版本" 的来源。

## 调试步骤

### Step 1: 定位 debug 产物

进入该论文的 debug 目录，看看渲染状态：

```
papers/<paper_dir>/debug/
```

重点看这几个文件：

| 文件 | 作用 |
|------|------|
| `tab_NNN.json` | 渲染状态，`renderer` 字段如果是 `"matplotlib"` 说明降级了 |
| `tab_NNN.tex` | 为这张表生成的独立 LaTeX 文件 |
| `tab_NNN.log` | pdflatex 编译日志，**这是诊断的核心** |
| `tab_NNN.stdout` / `tab_NNN.stderr` | pdflatex 标准输出和错误 |

### Step 2: 用 debug 脚本快速复现

直接重新跑一次完整的 `paper-tool add` 很慢，用 debug 脚本单独编译一张表：

```bash
bash .claude/skills/paper-reading-workflow/scripts/debug-table.sh \
  papers/<paper_dir>/paper.tex <table_index>
```

这个脚本做了：提取 preamble + body 中在表之前的宏定义 → 构建独立 LaTeX 文档 → pdflatex 编译 → 转 PNG。如果在这步就失败了，你就拿到了最小复现案例。

### Step 3: 读 .log 找根因

打开 `tab_NNN.log`，搜索 `!` 开头的行。常见的失败模式如下：

- **`! Undefined control sequence`** -- 这是最常见的原因。某个自定义宏没有注入到 preamble 里。检查是哪种宏：
  - `\newcommand` 定义在 body 里（`\begin{document}` 之后）但在这张表之前 → body 扫描逻辑可能漏了它
  - `\def`、`\newcolumntype`、`\pgfplotsset`、`\tikzset` 等宏类型可能没有被扫描
  - 修复点：`table_extractor.py` 中注入 preamble 宏的逻辑

- **`! LaTeX Error: File '...' not found`** -- 缺少 LaTeX 包或 pgfplotstable 引用的外部数据文件。如果是数据文件，检查 `source/` 目录下是否存在，路径解析可能需要调整

- **`! Extra alignment tab`** -- tabular 列数与实际数据不匹配，源文件就写错了，一般很少见

- **`\resizebox` wrapping 导致编译异常** -- standalone class 处理 `\resizebox{\textwidth}{!}{...}` 有问题。debug 脚本已经会自动 strip `resizebox`/`scalebox`/`adjustbox`，但如果 `table_extractor.py` 的预处理没有做相同的 strip，就可能导致实际渲染失败

### Step 4: 修代码

定位到具体原因后，在 `src/paper_tool/table_extractor.py` 里改对应的解析逻辑：

- 如果是 **body 里的宏没扫到**：参考 `debug-table.sh` 里 body_macros 的提取逻辑（`\newcommand`、`\def`、`\newcolumntype`、`\pgfplotsset`、`\tikzset`），确保 `table_extractor.py` 在编译前也注入了这些。注意最近 commit `6f7707c` 也是修类似问题（body 内宏定义未被注入），可以参考那个 commit 的改动方式

- 如果是 **preamble 宏不全**：检查 `_expand_tex_includes` 是否正确合并了所有 `\input`/`\include` 文件，以及自定义 `.sty` 文件中的定义是否被纳入

### Step 5: 验证修复

修好后，对目标论文重新跑一次（记得加 `--force --rerender-tables`）：

```bash
uv run paper-tool add <paper_url> --force --rerender-tables --debug
```

然后确认 `debug/tab_NNN.json` 的 `renderer` 字段变成了 `"latex"`。

**回归检查**：修复后还要找一张之前正常渲染的论文，用 `--force --rerender-tables` 重跑，确认不被你的改动搞坏。根据 `reference/latex-failure-patterns.md` 的 checklist，至少检查：标准 tabular、resizebox 包裹、pgfplotstable 这三类表格是否仍能正常渲染。

## 快速检查清单

1. `papers/<paper_dir>/debug/tab_NNN.json` -- 确认 `renderer` 是 `"matplotlib"`
2. `papers/<paper_dir>/debug/tab_NNN.log` -- 搜索 `!` 找具体错误
3. `bash debug-table.sh papers/<paper_dir>/paper.tex N` -- 独立复现
4. 修复 `src/paper_tool/table_extractor.py` 中的解析逻辑
5. `uv run paper-tool add <url> --force --rerender-tables` -- 验证修复
6. 重跑一张之前正常的论文作为回归检查
