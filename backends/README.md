# Output Backends

每个输出后端在 `backends/<name>/` 下有自己独立的配置目录，包含认证、存储位置、知识库和字段映射等设置。

## 目录结构

```text
backends/
├── notion/
│   ├── backend.yaml       # Notion 认证与属性映射
│   └── schema.yaml        # Notion 数据库字段定义
└── lark/
    └── backend.yaml       # 飞书认证与文档创建位置
```

## 初始化方式

### 方式一：让 skill 自动引导（推荐）

直接运行任意后端脚本，例如：

```bash
uv run skill/scripts/notion_check.py <paper-url>
uv run skill/scripts/lark_write.py <paper-dir>
```

如果对应后端的 `backend.yaml` 缺失或必要字段为空，agent 会自动停下来询问你，并把值保存到 `.env` 或 `backends/<name>/backend.yaml`。

### 方式二：手动复制模板

```bash
cp backends/notion/backend.yaml.example backends/notion/backend.yaml
cp backends/lark/backend.yaml.example backends/lark/backend.yaml
```

然后编辑 `backend.yaml`，填写 `auth` 下的 token、database_id 等值。

## 配置优先级

从高到低：

1. 脚本 CLI 参数（如 `--notion-database-id`）
2. `backends/<name>/backend.yaml` 中的直接值
3. `backends/<name>/backend.yaml` 中 `*_env` 字段指向的环境变量 / `.env`
4. `backends/<name>/backend.yaml.example` 中的默认值

## 安全建议

- 敏感信息（token、api key）建议写入 `.env`，由 `*_env` 字段引用。
- `backend.yaml` 可以提交到 git（如果不含直接填写的敏感值）。
- 如果直接在 `backend.yaml` 中填写了 token，请确保它不被意外提交。
