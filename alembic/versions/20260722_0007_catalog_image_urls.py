"""
本文件为商品和 Catalog Sync 快照增加多图字段。

它属于 Alembic 迁移层，只负责将既有首图回填为单元素图库；不访问闲鱼页面，
也不修改业务状态。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260722_0007"
down_revision: str | None = "20260720_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    为商品和发布快照增加非空图库列并回填旧首图。

    返回：无。
    异常：数据库结构或回填失败时向上抛出。
    副作用：新增两列并更新已有行。
    """

    op.add_column(
        "items",
        sa.Column("image_urls", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "catalog_changes",
        sa.Column("image_urls", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.execute(
        "UPDATE items SET image_urls = "
        "CASE WHEN image_url IS NULL THEN '[]'::json "
        "ELSE json_build_array(image_url) END"
    )
    op.execute(
        "UPDATE catalog_changes SET image_urls = "
        "CASE WHEN image_url IS NULL THEN '[]'::json "
        "ELSE json_build_array(image_url) END"
    )
    op.alter_column("items", "image_urls", server_default=None)
    op.alter_column("catalog_changes", "image_urls", server_default=None)


def downgrade() -> None:
    """
    删除多图列，保留兼容首图列。

    返回：无。
    异常：数据库结构变更失败时向上抛出。
    副作用：删除多图列及其中数据。
    """

    op.drop_column("catalog_changes", "image_urls")
    op.drop_column("items", "image_urls")
