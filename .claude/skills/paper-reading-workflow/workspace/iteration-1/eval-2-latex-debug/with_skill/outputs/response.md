# LaTeX 表格渲染调试指南

表格显示为 matplotlib 降级版本，说明 `table_extractor.py` 中的 LaTeX 编译失败了，触发了回退（fallback）逻辑。以下是完整的调试工作流。

## 第 1 步：确认渲染后端

观察终端输出，或者检查 `papers/<paper_dir>/debug/` 目录下的 JSON 状态文件。每个表格会有一个 `table_NN.json`，其中的 `"renderer"` 字段会标明后端：

- `"latex"` — LaTeX 编译成功
- `"matplotlib"` — LaTeX 失败，降级为 matplotlib
- `"cached"` — 命中缓存，直接使用了之前渲染的结果
- `""` (空) — 尚未渲染

## 第 2 步：检查调试产物

进入论文的 debug 目录：

```
papers/<paper_dir>/debug/
├── table_01.latex.tex       # 实际编译的 standalone LaTeX 源码
├── table_01.latex.log        # pdflatex 编译日志（关键！）
├── table_01.latex.stdout.txt # pdflatex stdout
├── table_01.latex.stderr.txt # pdflatex stderr
├── table_01.json             # 渲染状态（renderer, caption, label 等）
└── ...
```

重点关注 `.latex.log` 文件，其中记录了编译失败的根本原因。

## 第 3 步：隔离复现

用 debug 脚本在独立环境中复现该表格的编译过程：

```bash
bash .claude/skills/paper-reading-workflow/scripts/debug-table.sh \
  papers/<paper_dir>/paper.tex <table_index>
```

其中 `<table_index>` 从 0 开始计数（第 0 个表格 = 论文的第 1 个表格）。脚本会：

1. 提取 merged `paper.tex` 中第 N 个 table 环境
2. 注入 preamble 中的 `\newcommand`、`\definecolor`、`\newcolumntype` 等宏
3. 注入 `\begin{document}` 之后、该表格之前的 body 层宏定义（`\def`、`\tikzset`、`\pgfplotsset` 等）
4. 剥离 `\resizebox`/`\scalebox`/`\adjustbox` 包装（`standalone` 文档类不支持这些）
5. 生成 standalone LaTeX 文档并编译
6. 输出编译日志和 PNG

输出文件默认写入 `/tmp/debug-table/`，包含：
- `table.tex` — standalone LaTeX 文档
- `table.stdout` / `table.stderr` — pdflatex 输出
- `table.log` — 编译日志
- `table.png` — 成功编译后的 PNG

脚本会直接输出关键错误摘要（grep `^!|Error|Warning`），可以快速定位问题。

## 第 4 步：分析编译日志中的错误

打开 `debug/<table>.latex.log` 或 debug 脚本生成的 `table.log`，搜索以下关键词：

| 错误模式 | 含义 | 常见原因 |
|---------|------|---------|
| `! Undefined control sequence` | 宏未定义 | `\newcommand` 定义在 body 中（preamble 之前未提取到）或使用了未经 stub 的 `\renewcommand` |
| `! LaTeX Error: File '...' not found` | 文件缺失 | 缺少宏包、字体文件，或 `\pgfplotstableread` 引用的 `.dat`/`.csv` 文件路径未解析 |
| `! Extra alignment tab` | 列数不匹配 | tabular 源码畸形（极少见，属于源论文格式错误） |
| `! Misplaced \noalign` | booktabs 命令位置错误 | `\toprule`/`\midrule`/`\bottomrule` 放错了位置 |
| `Warning: Font shape ... not available` | 字体警告 | 通常无害，但如果导致缺字，需要安装对应 texlive 包 |

## 第 5 步：按根因分类修复

### 情况 A：Body 内宏定义未被注入

**症状**：`.log` 中出现 `! Undefined control sequence` 指向某个自定义命令（如 `\mytablenote`），该命令在论文 body 中用 `\newcommand` 或 `\def` 定义，但 standalone 编译时找不到。

**原因**：`_extract_macros()` 在 body 中扫描了 `\newcommand` / `\def` / `\newcolumntype` / `\pgfplotsset` / `\tikzset`，但可能遗漏了某些宏类型。

**调试方法**：在 `paper.tex` 中搜索 `\begin{document}` 到目标表格之间的自定义宏定义：
```bash
# 搜索 body 中的所有宏定义
awk '/\\begin\{document\}/,/\\begin\{table\}/' papers/<paper_dir>/paper.tex | \
  grep -oP '\\(newcommand|def|newcolumntype|pgfplotsset|tikzset|definecolor)\{[^}]*\}'
```

**修复点**：在 `src/paper_tool/figure_extractor.py` 的 `_extract_preamble_macros()` 或 `src/paper_tool/table_extractor.py` 的 `_extract_macros()` 中添加遗漏的宏类型。

参考 commit `6f7707c`（修复 body 内宏定义未被注入导致表格 LaTeX 编译降级到 matplotlib）。

### 情况 B：`\resizebox` 导致空白输出

**症状**：`.log` 中无明显错误，但编译出的 PDF 是空白的或表格被截断。

**原因**：`standalone` 文档类不支持 `\resizebox` 等缩放包装。`_prepare_table_body` 和 debug 脚本本应剥离这些命令，但如果包装变体未被覆盖，就会失败。

**修复点**：在 `_prepare_table_body()` 或 `_prepare_figure_body()` 中添加遗漏的包装命令变体。

### 情况 C：pgfplotstable 数据文件缺失

**症状**：`.log` 中出现 `! Package pgfplotstable Error: Could not read table file 'data.dat'`。

**原因**：论文使用了 `\pgfplotstableread{data.dat}` 从外部文件读取数据，`_inline_pgf_data()` 未能正确解析文件路径。

**调试方法**：
```bash
# 检查 source 目录中的 .dat / .csv 文件
find papers/<paper_dir>/source/ -name "*.dat" -o -name "*.csv"
```

**修复点**：在 `_inline_pgf_data()` 中改进路径解析逻辑。

### 情况 D：TikZ/PGFPlots 图像触及边框

**症状**：表格渲染出来了但内容被裁剪，边缘被切掉。

**原因**：`_detect_textwidth()` 返回的 `\textwidth` 对当前文档类不准确，导致 standalone 页面对应宽度不正确。

**修复点**：在 `_detect_textwidth()` 的启发式表（NeurIPS / ICLR / ICML / ACL / AAAI 等）中添加新文档类。

## 第 6 步：验证修复

```bash
# 针对修复后的论文，强制重新渲染所有表格
uv run paper-tool add <url> --force --rerender-tables
```

## 第 7 步：回归测试

**任何解析器修改都可能破坏之前正常工作的论文。** 修复后必须验证以下七种典型表格回归：

1. 标准 tabular 表格（最基础，改动后必过）
2. resizebox 包装的表格（最常出问题）
3. pgfplotstable 表格（数据文件路径解析）
4. 多列表格（multirow/multicolumn）
5. NeurIPS 格式表格（textwidth 检测）
6. ICLR/ICML/ACL 格式表格（不同文档类的 textwidth）
7. 带 `\newcommand`/`\def` body 宏的表格

对每个受影响的已知正常 URL 运行：
```bash
uv run paper-tool add <known_good_url> --force --rerender-tables
```

确保所有之前能正常渲染的表格仍然使用 `"latex"` 后端（而非降级为 `"matplotlib"`）。

## 更多参考

完整的历史失败模式目录和修复策略见：
`/home/sparidae/projects/paper-read-workflow/.claude/skills/paper-reading-workflow/reference/latex-failure-patterns.md`
