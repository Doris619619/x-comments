"""
本文件为 PostgreSQL Catalog Sync 增加采集批次、状态和版本化变更表。

它属于 Alembic 迁移层，只负责数据库结构演进；不执行采集、状态判断或 HTTP 调用。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0004"
down_revision: str | None = "20260715_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建可原子发布 Catalog revision 的最小持久化结构。

    无输入输出；数据库错误向上抛出；副作用为新增表、列和索引。
    """

    op.add_column("catalog_keywords", sa.Column("last_completed_at", sa.DateTime(timezone=True)))
    op.add_column("catalog_keywords", sa.Column("next_due_at", sa.DateTime(timezone=True)))
    op.create_index("ix_catalog_keywords_next_due_at", "catalog_keywords", ["next_due_at"])

    op.create_table(
        "crawl_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("crawl_jobs.job_id"), nullable=False),
        sa.Column("catalog_keyword_id", sa.Integer(), sa.ForeignKey("catalog_keywords.id")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("is_comparable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_crawl_runs_job_id", "crawl_runs", ["job_id"])
    op.create_index("ix_crawl_runs_catalog_keyword_id", "crawl_runs", ["catalog_keyword_id"])
    op.create_index("ix_crawl_runs_status", "crawl_runs", ["status"])

    op.create_table(
        "catalog_item_states",
        sa.Column("item_id", sa.String(64), sa.ForeignKey("items.item_id"), primary_key=True),
        sa.Column(
            "catalog_keyword_id",
            sa.Integer(),
            sa.ForeignKey("catalog_keywords.id"),
            primary_key=True,
        ),
        sa.Column("availability", sa.String(32), nullable=False, server_default="active"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_run_id", sa.String(36), sa.ForeignKey("crawl_runs.run_id")),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("item_id", "catalog_keyword_id"),
    )
    op.create_index(
        "ix_catalog_item_states_availability", "catalog_item_states", ["availability"]
    )

    op.create_table(
        "catalog_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_run_id", sa.String(36), sa.ForeignKey("crawl_runs.run_id"), nullable=False
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="published"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_run_id"),
    )

    op.create_table(
        "catalog_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "revision", sa.Integer(), sa.ForeignKey("catalog_revisions.revision"), nullable=False
        ),
        sa.Column("item_id", sa.String(64), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("availability", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        sa.Column("image_url", sa.Text()),
        sa.Column("location", sa.String(100)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("revision", "item_id"),
    )
    op.create_index("ix_catalog_changes_revision", "catalog_changes", ["revision"])
    op.create_index("ix_catalog_changes_item_id", "catalog_changes", ["item_id"])
    op.create_index("ix_catalog_changes_availability", "catalog_changes", ["availability"])


def downgrade() -> None:
    """
    按依赖逆序删除 Catalog Sync 数据结构。

    无输入输出；数据库错误向上抛出；副作用为删除本迁移新增的结构。
    """

    op.drop_index("ix_catalog_changes_availability", table_name="catalog_changes")
    op.drop_index("ix_catalog_changes_item_id", table_name="catalog_changes")
    op.drop_index("ix_catalog_changes_revision", table_name="catalog_changes")
    op.drop_table("catalog_changes")
    op.drop_table("catalog_revisions")
    op.drop_index("ix_catalog_item_states_availability", table_name="catalog_item_states")
    op.drop_table("catalog_item_states")
    op.drop_index("ix_crawl_runs_status", table_name="crawl_runs")
    op.drop_index("ix_crawl_runs_catalog_keyword_id", table_name="crawl_runs")
    op.drop_index("ix_crawl_runs_job_id", table_name="crawl_runs")
    op.drop_table("crawl_runs")
    op.drop_index("ix_catalog_keywords_next_due_at", table_name="catalog_keywords")
    op.drop_column("catalog_keywords", "next_due_at")
    op.drop_column("catalog_keywords", "last_completed_at")
