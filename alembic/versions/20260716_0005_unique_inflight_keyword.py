"""
本文件为 PostgreSQL 调度器增加同关键词未完成任务的唯一约束。

它属于 Alembic 迁移层，只增加数据库级并发保护；不启动调度器或执行采集任务。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260716_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建只约束 pending/running 状态的 PostgreSQL 部分唯一索引。

    无输入输出；若已有重复未完成任务则迁移失败并停止；副作用为新增并发安全索引。
    """

    op.create_index(
        "uq_crawl_jobs_inflight_keyword",
        "crawl_jobs",
        ["keyword"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    """
    删除本迁移新增的同关键词未完成任务唯一索引。

    无输入输出；数据库错误向上抛出；副作用为删除索引。
    """

    op.drop_index("uq_crawl_jobs_inflight_keyword", table_name="crawl_jobs")
