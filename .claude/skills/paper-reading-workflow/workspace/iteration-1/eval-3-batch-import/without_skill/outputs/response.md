# Batch Import 20 Papers into Notion Without Stopping on Failures

## Answer

`paper-tool` 的 `batch` 命令正好支持你的需求。它内置了 `--continue-on-error` 选项，可以让你在单篇论文失败后自动跳过、继续处理下一篇，不会半路停下。

## Commands and Explanation

```bash
uv run paper-tool batch --continue-on-error <your_markdown.md>
```

### Key Details

1. **文件格式支持**：`batch` 命令支持 `txt`、`csv`、`md` 等任意文本格式，会自动从文件内容中提取论文链接（URL），所以你不需要对 markdown 文件做任何预处理。

2. **`--continue-on-error`**：默认行为是 `--stop-on-error`（遇到任何错误立即停止）。加上 `--continue-on-error` 后，即使某篇论文添加失败（例如下载 PDF 超时、Notion API 写入失败），工具会记录该篇失败，然后自动继续处理下一篇。

3. **完成后会打印汇总面板**（Panel），展示：
   - 成功：N 篇
   - 失败：N 篇
   - 共：N 篇

   失败的论文不会阻断整体流程，你可以在最终汇总中看到哪些处理失败，再单独排查或重试。

### 其他可选参数

根据你的实际需求，可以组合以下选项：

| 参数 | 作用 |
|------|------|
| `--skip-llm` | 跳过 LLM 分析，仅下载 PDF 和写入 Notion，速度更快 |
| `--force` | 若 Notion 中已存在同 URL 论文，先归档旧页面再重新导入 |
| `--debug` | 打印 LLM 原始 prompt 和回包，用于排查具体失败原因 |

### 示例完整命令

```bash
uv run paper-tool batch --continue-on-error --skip-llm papers.md
```

如果你想让每个失败的论文有详细日志方便事后排查：

```bash
uv run paper-tool batch --continue-on-error --debug papers.md
```

### 批量处理完成后的注意事项

`batch` 命令结束后，如果有失败项，进程会返回退出码 `1`。你可以根据终端输出的失败论文 URL 列表，单独用 `uv run paper-tool add <url>` 再次尝试。
