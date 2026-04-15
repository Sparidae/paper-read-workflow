"""Helpers for validating the target Notion database against the schema."""

from __future__ import annotations

from dataclasses import dataclass

from notion_client import Client

from paper_tool.config import Config, get_config


@dataclass(slots=True)
class DatabaseCheckResult:
    ok: bool
    reason: str
    missing_properties: list[str]
    mismatched_types: list[str]
    database_title: str = ""


def normalize_notion_id(raw: str) -> str:
    return raw.replace("-", "").strip()


def expected_property_types(cfg: Config | None = None) -> dict[str, str]:
    cfg = cfg or get_config()
    type_by_key = {
        "title": "title",
        "authors": "rich_text",
        "abstract": "rich_text",
        "source": "select",
        "url": "url",
        "published_date": "date",
        "added_date": "date",
        "tags": "multi_select",
        "paper_type": "multi_select",
        "institution": "multi_select",
        "status": "checkbox" if cfg.notion_status_type == "checkbox" else "select",
    }

    expected: dict[str, str] = {}
    for key, prop_name in cfg.notion_properties.items():
        if not prop_name:
            continue
        prop_type = type_by_key.get(key)
        if prop_type:
            expected[prop_name] = prop_type
    return expected


def check_database(cfg: Config | None = None) -> DatabaseCheckResult:
    cfg = cfg or get_config()

    try:
        token = cfg.notion_token
        database_id = normalize_notion_id(cfg.notion_database_id)
    except Exception as exc:
        return DatabaseCheckResult(
            ok=False,
            reason=str(exc),
            missing_properties=[],
            mismatched_types=[],
        )

    try:
        client = Client(auth=token)
        db = client.databases.retrieve(database_id=database_id)
    except Exception as exc:
        return DatabaseCheckResult(
            ok=False,
            reason=f"无法访问 Notion 数据库: {exc}",
            missing_properties=[],
            mismatched_types=[],
        )

    actual_props = {
        name: prop.get("type", "unknown")
        for name, prop in db.get("properties", {}).items()
    }
    expected_props = expected_property_types(cfg)
    missing = [name for name in expected_props if name not in actual_props]
    mismatched = [
        f"{name}: 期望 {expected_props[name]}，实际 {actual_props[name]}"
        for name in expected_props
        if name in actual_props and actual_props[name] != expected_props[name]
    ]

    title = "".join(part.get("plain_text", "") for part in db.get("title", []))
    if not missing and not mismatched:
        return DatabaseCheckResult(
            ok=True,
            reason="数据库可访问，且 schema 与 config.yaml 一致。",
            missing_properties=[],
            mismatched_types=[],
            database_title=title,
        )

    detail_parts: list[str] = []
    if missing:
        detail_parts.append("缺少属性: " + ", ".join(missing))
    if mismatched:
        detail_parts.append("类型不匹配: " + "; ".join(mismatched))

    return DatabaseCheckResult(
        ok=False,
        reason="；".join(detail_parts),
        missing_properties=missing,
        mismatched_types=mismatched,
        database_title=title,
    )
