# 表格 LaTeX 渲染降级到 matplotlib 的调试方法

## 渲染降级链

表格渲染走三级降级链（见 `src/paper_tool/table_extractor.py`）：

```
pdflatex 编译（原始 table body）
  -> 失败 -> pdflatex 重试（剥离 \resizebox/\scalebox/\adjustbox/center）
    -> 失败 -> matplotlib 兜底渲染（解析 tabular 行，booktabs 风格 PNG）
```

当看到 matplotlib 版本时，说明前两次 pdflatex 都失败了。matplotlib 版本丢失了原论文的列颜色、字体样式、行距等排版效果，需要让 LaTeX 编译路径成功。

## 第一步：定位问题是哪个表

debug 产物在 `papers/<paper_dir>/debug/` 下。每个表有一组文件：

| 文件 | 说明 |
|------|------|
| `table_XX.json` | 渲染结果元数据，`renderer` 字段告诉你实际走的是 `latex` 还是 `matplotlib` |
| `table_XX.latex.tex` | 实际编译的 standalone LaTeX 源码，这是最重要的文件 |
| `table_XX.latex.log` | pdflatex 完整编译日志 |
| `table_XX.latex.stderr.txt` | pdflatex 标准错误输出 |

先确认哪些表走了 matplotlib：

```bash
grep -l '"renderer": "matplotlib"' papers/*/debug/table_*.json
```

## 第二步：看编译错误

用 `.log` 文件查具体失败原因：

```bash
# 看所有错误
grep -E '^!|Error|Fatal|Undefined' papers/<paper_id>/debug/table_XX.latex.log
```

常见错误类型及其原因：

### `! Undefined control sequence`
论文用了自定义命令，但 standalone 模板中没有定义它。查找是哪个命令：
```bash
grep 'Undefined control sequence' papers/<paper_id>/debug/table_XX.latex.log
```
然后去 `table_XX.latex.tex` 里找到该命令，再到原始 `paper.tex` 里查它的定义来源。如果定义在 preamble 中，`_extract_preamble_macros` 应该已经提取了；如果定义在 body 中，只有紧邻表格之前的定义才会被抓取（`parse_tables` 第 901-907 行）。如果命令由某个未加载的 sty/宏包定义，需要加 `\providecommand` stub。

### `! LaTeX Error: Missing \begin{document}`
某个被注入 preamble 的宏定义中含有必须在 `\begin{document}` 之后才能使用的排版命令。例如 `\renewcommand{\fnum@figure}{\figurename~\textcolor{violet}{\thefigure}}` ——`\textcolor` 是排版命令，不能出现在 preamble 里。

**定位方法**：检查 `table_XX.latex.tex` 中 `@@PREAMBLE_MACROS@@` 区域（即 `\providecommand{\parencite}...` 之前的自定义宏区域），看有没有 `\textcolor`、`\colorbox`、`\makebox` 等排版命令。

### `! Extra alignment tab` 或 `! Misplaced \noalign`
tabular 语法错误，通常是论文原文就有问题，或者我们的 body 清理逻辑破坏了 tabular 结构。

## 第三步：检查 standalone LaTeX 源码

打开 `table_XX.latex.tex`，重点检查：

### 3a. tabular 环境是否还在

看 `\begin{document}...\end{document}` 之间的 body 里是否还能找到 `\begin{tabular}`。如果 tabular 被 `_remove_command_calls` 意外吃掉了（比如 resizebox 参数解析出错时连同内部 tabular 一起删除），body 就会只剩 `\scriptsize`、`\setlength` 这类定位命令但没有实际的表格。

对比验证：
```bash
# 看 debug 产物中的 table body
sed -n '/\\begin{document}/,/\\end{document}/p' papers/<paper_id>/debug/table_XX.latex.tex

# 看原始 paper.tex 中对应的 table 环境原始内容
grep -A 60 '\\begin{table}' papers/<paper_id>/paper.tex | head -80
```

### 3b. preamble 宏是否正确注入

看 `\providecommand{\parencite}[1]{[#1]}` 之前的区域。`@@RENEW_STUBS@@` 和 `@@PREAMBLE_MACROS@@` 会在这里展开。如果论文的 `\newcommand` 或 `\definecolor` 没出现在这里，说明提取逻辑漏了。

### 3c. textwidth 是否正确

看 `\setlength{\textwidth}{...}` 的值。如果偏差太大会导致表格超出裁剪区域。这个值由 `_detect_textwidth` 确定（先 probe pdflatex 实测，失败则用启发式规则匹配已知文档类/会议模板）。

## 第四步：重跑验证

修改代码后重跑，记得强制重渲染以跳过缓存：

```yaml
# config.yaml
llm:
  rerender_tables: true
```

```bash
# 跳过 LLM 分析和 Notion 上传，只重新跑提取+渲染
uv run paper-tool add <url> --skip-llm

# 或带 debug 标志看完整 prompt（这是给 LLM 调用的 debug，与 LaTeX 渲染无关）
uv run paper-tool add <url> --skip-llm --debug
```

注意：`--debug` 标志控制的是 LLM prompt/response 打印，不影响 debug 产物写入——`papers/<paper_id>/debug/` 下的文件始终在生成。

## 第五步：按根因分类修复

根据诊断结果，问题落在下面几类之一：

### 类型 A：未定义命令

论文的 table 中用了某个命令但 standalone 模板不知道它。修复路径：
- 去 `_LATEX_TEMPLATE` 里加对应的 `\usepackage`
- 或者在模板的 `\providecommand` fallback 区域加 stub
- 如果命令来自 body 中而非 preamble 中的定义，检查 `parse_tables` 第 901-907 行的 body-before-table 宏扫描逻辑是否覆盖了这种情况

### 类型 B：preamble 宏中有排版命令

`_extract_macros` / `_extract_preamble_macros` 无差别地提取所有 `\newcommand`/`\renewcommand`，但有些重定义里包含必须在 document body 内使用的命令。修复思路：
- 在提取时过滤掉包含排版命令的宏定义
- 或者把这类宏定义移到 `\begin{document}` 之后

### 类型 C：body 清理逻辑破坏了表格结构

`_prepare_table_body` 或 `_prepare_table_body_retry` 中剥离 caption/label/resizebox 等命令时，括号平衡解析失效导致连同内部 tabular 一起删除。修复思路：
- 在 `_remove_command_calls` 中添加更严格的参数边界检查
- 确保 `_consume_balanced` 正确处理了嵌套 `{}` 的情况（当前实现是逐字符计数括号，应该能处理，但可能遇到 `{]}` 之类的特殊组合）
- 加一个安全阀：如果 body 中找不到任何 tabular 环境，不要进 pdflatex，直接降级到 matplotlib（当前已经有这个逻辑，但重试变体也可能丢失 tabular）

### 类型 D：textwidth 估算错误

表格宽度与 standalone 页面不匹配导致裁剪或溢出。修复思路：
- 在 `_estimate_textwidth` 的 `style_widths` dict 中添加对应的会议/期刊样式包
- 或者在 `_probe_textwidth` 中让探测文档加载更多 preamble 元素来提高实测成功率

## 快速命令速查

```bash
# 找所有 matplotlib 降级的表
grep -l '"renderer": "matplotlib"' papers/*/debug/table_*.json

# 看具体错误
grep -E '^!|Error|Fatal|Undefined' papers/<paper_id>/debug/table_XX.latex.log

# 看 standalone tex 的 body 部分（确认 tabular 是否还在）
sed -n '/\\begin{document}/,/\\end{document}/p' papers/<paper_id>/debug/table_XX.latex.tex

# 提取原始论文中该 table 的源码
grep -A 60 '\\begin{table}' papers/<paper_id>/paper.tex | head -80

# 看注入的 preamble 宏
sed -n '/@@PREAMBLE_MACROS@@占位被替换后实际内容/,/\\providecommand{\\parencite}/p' \
  papers/<paper_id>/debug/table_XX.latex.tex
# （更简单的：直接看 \providecommand{\parencite} 之前的全部 preamble 内容）
head -40 papers/<paper_id>/debug/table_XX.latex.tex

# 强制重渲染
uv run paper-tool add <url> --skip-llm
```
