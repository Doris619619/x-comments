"""
本文件把采购聊天任务升级为兼容 v1 的 v2 授权与 Canary 数据结构。

它只演进 PostgreSQL 表和索引；不读取登录态、不访问闲鱼，也不会开启聊天或自动发送。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0008"
down_revision: str | None = "20260722_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    增加任务授权快照、执行模式、回复退避计数和来源商品活动任务唯一索引。

    无输入输出；数据库失败向上抛出并由 Alembic 回滚。
    """

    op.add_column(
        "procurement_execution_tasks",
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "procurement_execution_tasks",
        sa.Column("execution_mode", sa.String(32), nullable=False, server_default="paid_order"),
    )
    op.add_column(
        "procurement_execution_tasks",
        sa.Column("auto_send_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "procurement_execution_tasks",
        sa.Column("authorized_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "procurement_execution_tasks",
        sa.Column("authorization_source", sa.String(40)),
    )
    op.create_index(
        "ix_procurement_execution_tasks_execution_mode",
        "procurement_execution_tasks",
        ["execution_mode"],
    )
    op.create_index(
        "uq_procurement_active_source_item",
        "procurement_execution_tasks",
        ["source_item_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending_source_verification', 'contacting_seller', "
            "'awaiting_seller_reply', 'awaiting_procurement_review')"
        ),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("seller_poll_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """
    删除 v2 授权字段和索引，恢复到 v1 数据结构。

    无输入输出；该操作会丢弃 v2 授权快照，只用于受控回滚。
    """

    op.drop_column("conversation_sessions", "seller_poll_attempt_count")
    op.drop_index(
        "uq_procurement_active_source_item",
        table_name="procurement_execution_tasks",
    )
    op.drop_index(
        "ix_procurement_execution_tasks_execution_mode",
        table_name="procurement_execution_tasks",
    )
    op.drop_column("procurement_execution_tasks", "authorization_source")
    op.drop_column("procurement_execution_tasks", "authorized_at")
    op.drop_column("procurement_execution_tasks", "auto_send_authorized")
    op.drop_column("procurement_execution_tasks", "execution_mode")
    op.drop_column("procurement_execution_tasks", "contract_version")
