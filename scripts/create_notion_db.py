"""
Create a Notion database using the committed schema template.

Usage:
    uv run python scripts/create_notion_db.py
"""

import os
import sys
from pathlib import Path

import typer
from notion_client import Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_tool.config import PROJECT_ROOT, get_config
from paper_tool.notion_setup import check_database, normalize_notion_id

_SOURCE_OPTIONS = [
    {"name": "Arxiv", "color": "blue"},
    {"name": "OpenReview", "color": "green"},
]
_DEFAULT_STATUS_OPTIONS = [
    ("Unread", "gray"),
    ("Reading", "yellow"),
    ("Read", "green"),
]

app = typer.Typer(add_completion=False)


def _upsert_env_value(key: str, value: str) -> Path:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["# paper-tool 环境配置"]

    prefix = f"{key}="
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            lines[idx] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return env_path


def _build_database_properties() -> dict[str, dict]:
    cfg = get_config()
    mapping = cfg.notion_properties
    properties: dict[str, dict] = {}

    def add(prop_name: str, schema: dict) -> None:
        if prop_name and prop_name not in properties:
            properties[prop_name] = schema

    add(mapping.get("title", ""), {"title": {}})
    add(mapping.get("authors", ""), {"rich_text": {}})
    add(mapping.get("abstract", ""), {"rich_text": {}})
    add(mapping.get("source", ""), {"select": {"options": list(_SOURCE_OPTIONS)}})
    add(mapping.get("url", ""), {"url": {}})
    add(mapping.get("published_date", ""), {"date": {}})
    add(mapping.get("added_date", ""), {"date": {}})
    add(mapping.get("tags", ""), {"multi_select": {"options": []}})
    add(mapping.get("paper_type", ""), {"multi_select": {"options": []}})
    add(mapping.get("institution", ""), {"multi_select": {"options": []}})

    status_name = mapping.get("status", "")
    if cfg.notion_status_type == "checkbox":
        add(status_name, {"checkbox": {}})
    else:
        status_options: list[dict[str, str]] = []
        seen: set[str] = set()
        for name, color in [
            (cfg.notion_default_status.strip() or "Unread", "gray"),
            *_DEFAULT_STATUS_OPTIONS,
        ]:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            status_options.append({"name": name, "color": color})
        add(status_name, {"select": {"options": status_options}})

    return properties


@app.command()
def main(
    parent_page_id: str = typer.Option(
        "",
        "--parent-page-id",
        help="数据库父页面 ID；留空则读取 .env 中的 NOTION_PARENT_PAGE_ID",
    ),
    title: str = typer.Option(
        "",
        "--title",
        help="数据库标题；留空则读取 notion_schema.yaml 里的 database_title",
    ),
    write_env: bool = typer.Option(
        True,
        "--write-env/--no-write-env",
        help="是否把新数据库 ID 写回 .env",
    ),
) -> None:
    cfg = get_config()
    resolved_parent = normalize_notion_id(parent_page_id or cfg.notion_parent_page_id)
    if not resolved_parent:
        typer.echo(
            "缺少父页面 ID。请在 .env 中设置 NOTION_PARENT_PAGE_ID，"
            "或传入 --parent-page-id。",
            err=True,
        )
        raise typer.Exit(code=1)

    resolved_title = title.strip() or cfg.notion_database_title
    client = Client(auth=cfg.notion_token)
    response = client.databases.create(
        parent={"type": "page_id", "page_id": resolved_parent},
        title=[{"type": "text", "text": {"content": resolved_title}}],
        properties=_build_database_properties(),
    )

    database_id = normalize_notion_id(response["id"])
    database_url = response.get("url", f"https://www.notion.so/{database_id}")

    typer.echo(f"✓ 新数据库已创建: {database_url}")
    typer.echo(f"数据库 ID: {database_id}")

    if write_env:
        env_path = _upsert_env_value("NOTION_DATABASE_ID", database_id)
        _upsert_env_value("NOTION_PARENT_PAGE_ID", resolved_parent)
        typer.echo(f"✓ 已写回 .env: {env_path}")

    verify = check_database()
    if verify.ok:
        typer.echo("✓ 新数据库自检通过。")
        return

    typer.echo(f"新数据库自检失败: {verify.reason}", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
