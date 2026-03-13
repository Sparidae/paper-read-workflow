"""
临时调试脚本：通过 Notion API 创建一个带完整属性的测试数据库。

用法：
    uv run python scripts/create_test_db.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_tool.config import get_config
from notion_client import Client


def main() -> None:
    cfg = get_config()
    client = Client(auth=cfg.notion_token)

    # 在同一父页面下创建新数据库
    existing_db = client.databases.retrieve(database_id=cfg.notion_database_id)
    parent_page_id = existing_db["parent"]["page_id"]
    print(f"父页面 ID: {parent_page_id}")

    new_db = client.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "paper-tool 测试数据库"}}],
        properties={
            "Title": {"title": {}},
            "Authors": {"rich_text": {}},
            "Abstract": {"rich_text": {}},
            "Source": {
                "select": {
                    "options": [
                        {"name": "Arxiv", "color": "blue"},
                        {"name": "OpenReview", "color": "green"},
                    ]
                }
            },
            "URL": {"url": {}},
            "Published Date": {"date": {}},
            "Added Date": {"date": {}},
            "Tags": {"multi_select": {"options": []}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Unread", "color": "gray"},
                        {"name": "Reading", "color": "yellow"},
                        {"name": "Read", "color": "green"},
                    ]
                }
            },
        },
    )

    db_id = new_db["id"].replace("-", "")
    db_url = new_db.get("url", f"https://www.notion.so/{db_id}")

    print(f"\n✓ 测试数据库创建成功！")
    print(f"  数据库 ID : {db_id}")
    print(f"  链接      : {db_url}")
    print(f"\n如果确认要用这个新数据库，把 .env 里的 NOTION_DATABASE_ID 改为:")
    print(f"  NOTION_DATABASE_ID={db_id}")


if __name__ == "__main__":
    main()
