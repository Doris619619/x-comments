"""
本文件创建 Goal 1 的任务、商品、关键词及关联表。

它属于 Alembic 迁移层，只修改数据库结构，不执行应用业务或页面操作。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260711_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建 POC 初始数据表和索引。

    无输入输出；数据库错误会抛出并回滚；副作用为新增数据库结构。
    """

    op.create_table(
        "crawl_jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("keyword", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text()),
    )
    op.create_index("ix_crawl_jobs_keyword", "crawl_jobs", ["keyword"])
    op.create_index("ix_crawl_jobs_status", "crawl_jobs", ["status"])
    op.create_table(
        "items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("image_url", sa.Text()),
        sa.Column("item_url", sa.Text(), nullable=False),
        sa.Column("location", sa.String(100)),
        sa.Column("source", sa.String(32), nullable=False, server_default="xianyu"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_items_last_seen_at", "items", ["last_seen_at"])
    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("normalized_value", sa.String(100), nullable=False, unique=True),
        sa.Column("display_value", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_keywords_normalized_value", "keywords", ["normalized_value"], unique=True)
    op.create_table(
        "item_keywords",
        sa.Column("item_id", sa.String(64), sa.ForeignKey("items.item_id"), primary_key=True),
        sa.Column("keyword_id", sa.Integer(), sa.ForeignKey("keywords.id"), primary_key=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("item_id", "keyword_id"),
    )


def downgrade() -> None:
    """
    按依赖逆序删除初始表和索引。

    无输入输出；数据库错误会抛出并回滚；副作用为删除数据库结构。
    """

    op.drop_table("item_keywords")
    op.drop_index("ix_keywords_normalized_value", table_name="keywords")
    op.drop_table("keywords")
    op.drop_index("ix_items_last_seen_at", table_name="items")
    op.drop_table("items")
    op.drop_index("ix_crawl_jobs_status", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_keyword", table_name="crawl_jobs")
    op.drop_table("crawl_jobs")
