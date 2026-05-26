# Response

你可以运行以下命令将这篇论文添加到 Notion：

```bash
uv run paper-tool add https://arxiv.org/abs/2301.12345
```

这个命令会触发完整的自动化流水线：

1. **PDF/LaTeX 下载** — 从 arXiv 拉取论文源文件。
2. **文本提取** — 解析 PDF 或 LaTeX 源码，提取正文、图表等结构化内容。
3. **图表渲染** — 将论文中的 figures 和 tables 渲染为图片。
4. **LLM 分析** — 调用大模型生成 AI 阅读笔记（摘要、方法、贡献、优缺点等）。
5. **写入 Notion** — 将解析结果和 LLM 笔记写入你的 Notion 数据库，生成一页阅读笔记。

**常用 flag：**

- 如果遇到问题想看原始 LLM 输入输出，加上 `--debug`：
  ```bash
  uv run paper-tool add --debug https://arxiv.org/abs/2301.12345
  ```

- 如果你只想手动记笔记、不需要 AI 生成，可以跳过 LLM：
  ```bash
  uv run paper-tool add --skip-llm https://arxiv.org/abs/2301.12345
  ```

**如果 LaTeX 渲染失败：**

终端输出会显示渲染后端是 `latex`、`matplotlib` 还是 `cached`。失败时去 `papers/<paper_dir>/debug/` 目录查看 `.tex`、`.log`、`.stdout`、`.stderr` 文件定位问题。常见原因包括缺失宏定义、resizebox 包裹问题、字体缺失、pgfplotstable 找不到数据、TikZ 边框碰触等。修复后用 `--force` 重新导入：

```bash
uv run paper-tool add https://arxiv.org/abs/2301.12345 --force
```

**成功之后**，Notion 页面 URL 会打印在终端输出中，直接分享即可。
