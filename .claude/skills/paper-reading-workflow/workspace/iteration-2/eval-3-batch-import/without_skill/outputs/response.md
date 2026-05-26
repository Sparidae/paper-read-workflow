# 批量导入 20 篇论文，失败不中断

paper-tool 的 `batch` 命令已经内置了你需要的功能。你只需要一个 flag：

```bash
uv run paper-tool batch papers.md --continue-on-error --force
```

关键参数说明：

- `batch papers.md` — 自动从 markdown 文件里提取所有 URL（也支持 txt、csv 等格式）。
- `--continue-on-error` — 遇到某篇失败时不中止，继续处理下一篇。默认行为（`--stop-on-error`）是失败就停。
- `--force` — 如果 Notion 里已经存在同名 URL 的页面，会归档旧的然后重新导入。如果第一次跑中途断掉了，第二次跑不会因为"已存在"而报错。

`papers.md` 里链接的格式无所谓（普通 URL、`[title](url)` markdown 链接、裸链接都可以），`batch` 会自动提取。

跑完之后会打印汇总结果：

```
成功: 18  失败: 2  共: 20
```

注意：即便开了 `--continue-on-error`，最终的 exit code 在有失败的情况下仍然是 1（非零），方便你在脚本里判断。失败的那几篇你看输出的错误信息单独排查即可。
