# Response to: 帮我把这篇论文加到Notion里 https://arxiv.org/abs/2301.12345

好的，我来帮你把这篇文章加到Notion中。使用这个命令：

```bash
uv run paper-tool add https://arxiv.org/abs/2301.12345
```

这个命令会做以下几件事：

1. **下载 PDF** - 从 arxiv 下载论文的 PDF 文件
2. **LLM 分析** - 使用配置的 LLM 模型对论文内容进行分析和总结
3. **写入 Notion** - 将论文元数据和 AI 生成的笔记写入你配置的 Notion 数据库

如果你只想导入论文元数据而不进行 LLM 分析，可以加上 `--skip-llm` 参数：

```bash
uv run paper-tool add --skip-llm https://arxiv.org/abs/2301.12345
```

如果你想查看详细的调试信息（包括 LLM 的原始 prompt 和响应），可以加上 `--debug` 参数：

```bash
uv run paper-tool add --debug https://arxiv.org/abs/2301.12345
```

需要我帮你执行这个命令吗？
