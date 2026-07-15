"""
本文件为杂货铺持久化搜索清单创建数据库表并写入初始分类词。

它属于 Alembic 迁移层，只修改数据库结构和基础配置数据；不执行网页采集或 API 业务。
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0002"
down_revision: str | None = "20260711_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建搜索清单表、索引和默认杂货铺关键词。"""

    op.create_table(
        "catalog_keywords",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("keyword", sa.String(100), nullable=False, unique=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_catalog_keywords_category", "catalog_keywords", ["category"])
    op.create_index("ix_catalog_keywords_keyword", "catalog_keywords", ["keyword"], unique=True)
    op.create_index("ix_catalog_keywords_is_enabled", "catalog_keywords", ["is_enabled"])
    now = datetime.now(UTC)
    table = sa.table(
        "catalog_keywords",
        sa.column("category", sa.String()),
        sa.column("keyword", sa.String()),
        sa.column("note", sa.Text()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    op.bulk_insert(
        table,
        [
            {
                "category": "潮玩收藏", "keyword": "手办", "note": "动漫、模型和小摆件",
                "created_at": now, "updated_at": now,
            },
            {
                "category": "实用小物", "keyword": "遥控器", "note": "家电和设备配件",
                "created_at": now, "updated_at": now,
            },
            {
                "category": "户外渔具", "keyword": "鱼钩", "note": "钓鱼相关小物",
                "created_at": now, "updated_at": now,
            },
            {
                "category": "怀旧收藏", "keyword": "古董", "note": "旧物和收藏品",
                "created_at": now, "updated_at": now,
            },
            {
                "category": "桌面趣物", "keyword": "奇趣摆件", "note": "造型独特的桌面小物",
                "created_at": now, "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    """删除搜索清单表和其索引。"""

    op.drop_index("ix_catalog_keywords_is_enabled", table_name="catalog_keywords")
    op.drop_index("ix_catalog_keywords_keyword", table_name="catalog_keywords")
    op.drop_index("ix_catalog_keywords_category", table_name="catalog_keywords")
    op.drop_table("catalog_keywords")
