# Batch Import Response

完全可以做到。`batch` 命令自带 `--continue-on-error` 参数，专门用于这个场景——某篇失败不会中断后续处理，全部跑完后会汇总成功/失败数量。

## 推荐命令

```bash
uv run paper-tool batch <你的markdown文件.md> --continue-on-error
```

## 发生了什么

1. **自动提取链接**：`batch` 会从 markdown 文件中自动提取所有 URL（支持 md、txt、csv 等任意文本格式，它只扫描里面出现的 http/https 链接），所以不需要手动转成纯 URL 列表。

2. **逐篇处理**：每篇论文走完整流水线——下载 PDF、提取文本/图表、写入 Notion、LLM 分类、生成阅读笔记。进度条会实时显示当前步骤。

3. **遇错继续**：`--continue-on-error` 的行为是：
   - 如果某篇论文在任意步骤抛异常（下载失败、Notion API 报错、LLM 超时等），`_process_paper` 返回 `False`，`fail_count += 1`，然后 `for` 循环直接进入下一篇 (`continue`)——不会 `break`。
   - 所有论文都处理完后，终端打印汇总 Panel：成功 X 篇，失败 Y 篇，共 Z 篇。
   - 如果有失败，命令以 exit code 1 退出，方便你在脚本里判断。

## 额外可加的参数

| 参数 | 作用 |
|------|------|
| `--skip-llm` | 只存元数据到 Notion，跳过 LLM 分析。如果只是先占坑导入，后续再逐个跑分析，可以用这个大幅加速。 |
| `--force` | 如果 Notion 中已存在同 URL 的论文页面，归档旧页面后重新导入。 |
| `--debug` | 打印 LLM 的原始 prompt 和回包，方便排查分类/笔记质量问题。 |
| `--stream` | 在终端小窗口实时显示 LLM 流式输出，适合观察生成质量但需要互动确认的场景。 |

## 如果你想要更多控制（编程方式）

CLI 的 batch 底层就是 `for url in urls: try...except...continue`。你也可以用 Python 脚本更精细地控制，比如：

```python
from paper_tool.config import PipelineContext
from paper_tool.pipeline import download_paper, extract_paper_text, run_pipeline

ctx = PipelineContext.from_config()

urls = extract_urls_from_markdown("your_file.md")  # 自己实现或用 CLI 的 _extract_urls

success_count = 0
failures = []

for url in urls:
    try:
        run_pipeline(url)
        success_count += 1
    except Exception as e:
        failures.append((url, str(e)))
        continue

print(f"成功: {success_count}, 失败: {len(failures)}")
for url, err in failures:
    print(f"  FAILED: {url} — {err}")
```

但对于你的场景，直接用 `batch --continue-on-error` 就足够了，不需要多写代码。
