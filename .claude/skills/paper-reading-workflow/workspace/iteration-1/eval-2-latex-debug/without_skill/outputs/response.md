# 论文表格 LaTeX 渲染失败调试指南

## 问题背景

表格渲染有三级降级链：

```
pdflatex 首次编译（原始 body）
  -> 失败 -> pdflatex 重试（剥离 resizebox/scalebox/center）
    -> 失败 -> matplotlib 兜底渲染（解析 tabular 行，booktabs 风格 PNG）
```

当看到 "matplotlib 降级版本" 时，说明前两次 pdflatex 编译都失败了，走到了最后的 matplotlib 兜底路径。matplotlib 版本确实比较粗糙——缺少论文原有的颜色、字体样式、行距等——所以需要修复 LaTeX 编译路径。

## 第一步：定位 debug 产物

每次执行 `uv run paper-tool add <url>` 后，渲染产物和调试文件在：

```
papers/<paper_id>/debug/
```

其中每个表格（和图片）会生成一组文件：

| 文件 | 说明 |
|------|------|
| `table_XX.json` | 渲染结果元数据，`renderer` 字段标记是 `latex` 还是 `matplotlib` |
| `table_XX.latex.tex` | 实际编译的 standalone LaTeX 源码 |
| `table_XX.latex.log` | pdflatex 完整编译日志 |
| `table_XX.latex.stderr.txt` | pdflatex 标准错误输出 |
| `table_XX.latex.stdout.txt` | pdflatex 标准输出 |

首先检查 `table_XX.json` 确认哪些表走了 matplotlib：

```bash
grep -l '"renderer": "matplotlib"' papers/*/debug/table_*.json
```

## 第二步：查看 LaTeX 编译错误

**最直接的方式：看 `.log` 文件末尾的错误。**

```bash
# 查看某个表格的 LaTeX 编译错误
grep -E '^!|Error|Fatal' papers/<paper_id>/debug/table_XX.latex.log
```

例如本项目中的一个真实案例（C0RAL 论文 table_01）：

```
! LaTeX Error: Missing \begin{document}.
...
l.33 \renewcommand{\fnum@figure}
                                {\figurename~\textcolor{violet}{\thefigure}}
```

错误原因：论文 preamble 中的 `\renewcommand{\fnum@figure}{\figurename~\textcolor{violet}{\thefigure}}` 被提取为 preamble 宏并注入了 standalone 模板的 preamble 区域，但 `\textcolor` 只能在 `\begin{document}` 之后使用。这使得 pdflatex 在到达 `\begin{document}` 之前就报错退出。

## 第三步：检查 standalone LaTeX 源码

打开 `table_XX.latex.tex`，检查以下常见问题：

### 3a. Preamble 宏在 begin{document} 之前执行了排版命令

如上面的 `\textcolor{violet}` 案例。`\definecolor` 是安全的，但 `\textcolor`、`\colorbox`、`\makebox` 等排版命令出现在 preamble 中就会导致 "Missing \begin{document}" 错误。

**诊断方法**：检查 `@@PREAMBLE_MACROS@@` 注入的宏定义中是否包含 `\textcolor`、`\colorbox`、`\makebox`、`\framebox` 等排版命令。

### 3b. 表格 body 丢失了 tabular 环境

这是一个更隐晦的问题。看 C0RAL table_01 的 `.latex.tex` 第 71-76 行：

```latex
\begin{document}
\begin{minipage}{\textwidth}
\scriptsize
\setlength{\tabcolsep}{3pt}
\end{minipage}
\end{document}
```

**table body 里没有 tabular！** 只有 `\scriptsize` 和 `\setlength{\tabcolsep}{3pt}`。这说明 `_prepare_table_body_retry` 中剥离 `\resizebox` 时把包裹在 resizebox 里的 tabular 也一起干掉了 —— 当 resizebox 包裹了整个 `{tabular}...` 且参数复杂时，`_remove_command_calls` 的括号平衡解析可能出错。

**诊断方法**：对比原始论文 tex 文件中该 table 的 body 和 debug 产物中的 table body，确认 tabular 环境是否被意外移除。

### 3c. 未定义的命令

论文中自定义的命令（通过 `\newcommand` 定义）如果在 standalone 模板中没有对应的宏包支持，会编译失败。`_extract_preamble_macros` 会自动提取 preamble 中所有 `\newcommand`/`\def`/`\definecolor` 等定义并注入，但如果命令定义在 `\begin{document}` 之后（即 body 中），只有紧邻表格之前的定义会被提取。

**诊断方法**：搜索 log 中的 `Undefined control sequence` 错误：

```bash
grep 'Undefined control sequence' papers/<paper_id>/debug/table_XX.latex.log
```

找到未定义的命令后，检查论文原始 tex 中该命令的定义位置，确认是否在 preamble 中被正确提取。

## 第四步：重跑验证

调试时建议在 `config.yaml` 中确认以下设置：

```yaml
llm:
  rerender_tables: true    # 强制重渲染，不读取缓存
  rerender_figures: true
```

然后用 `--skip-llm` 运行（跳过 LLM 分析和 Notion 上传，只跑提取和渲染）：

```bash
uv run paper-tool add <url> --skip-llm
```

这会重新执行整个提取+渲染流程，生成新的 debug 产物。检查 `papers/<paper_id>/debug/` 下的新文件。

## 第五步：修复方向

根据 root cause 选择修复方向：

1. **Preamble 宏中有排版命令**（如 `\textcolor`）：在 `_extract_preamble_macros` 或 `_extract_macros` 中过滤掉包含排版命令的 `\renewcommand` 定义，或者用 `\AtBeginDocument{...}` 包裹它们。

2. **表格 body 丢失 tabular**：在 `_remove_command_calls` 或 `_prepare_table_body_retry` 中修复 resizebox 剥离逻辑，确保只剥离外层包裹命令而不破坏内部 tabular。

3. **未定义命令**：排查是否有 body 中定义但 preamble 中未定义的宏；检查 `_extract_renewcommand_stubs` 是否为该命令生成了正确的 stub。

## 快速诊断命令汇总

```bash
# 1. 找哪些表用了 matplotlib
grep -l '"renderer": "matplotlib"' papers/*/debug/table_*.json

# 2. 看具体错误
grep -E '^!|Error|Fatal|Undefined' papers/<paper_id>/debug/table_XX.latex.log

# 3. 提取原始 paper.tex 中的表格 body（对比 debug 产物看有没有丢失 tabular）
grep -A 50 '\\begin{table}' papers/<paper_id>/paper.tex | head -80

# 4. 查看 standalone tex 中注入的 preamble 宏
sed -n '/\\providecommand{\\fnum@figure}/,/\\begin{document}/p' \
  papers/<paper_id>/debug/table_XX.latex.tex

# 5. 重跑（强制重渲染，跳过 LLM）
uv run paper-tool add <url> --skip-llm
```

## 注意事项

- CLI 的 `--debug` 标志是给 LLM prompt/response 调试用的，只打印 API 调用的原始 prompt 和回包。它**不**影响 LaTeX 渲染调试输出——debug 产物（`papers/*/debug/`）始终生成。
- `rerender_tables: true` 只影响是否跳过已有 PNG 缓存，不影响 debug 产物的写入（无论缓存命中与否，debug JSON 都会更新）。
- 如果论文使用 pgfplotstable（约 1% 的论文），没有 matplotlib 降级路径，LaTeX 编译是唯一路线，必须修好。
