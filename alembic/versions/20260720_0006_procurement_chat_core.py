"""
本文件为采购聊天第一阶段增加本地执行任务、会话、消息、审计和事务 Outbox 表。

它属于 Alembic 迁移层，只负责 PostgreSQL 数据结构演进；不调用大模型、Playwright 或回调接口。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260720_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建采购执行与聊天领域核心表、唯一约束和查询索引。

    无输入输出；数据库错误向上抛出并回滚；副作用为新增数据库结构。
    """

    op.create_table(
        "procurement_execution_tasks",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column(
            "source_item_id",
            sa.String(64),
            sa.ForeignKey("items.item_id"),
            nullable=False,
        ),
        sa.Column("expected_title", sa.Text(), nullable=False),
        sa.Column("expected_price_cny_minor", sa.Integer(), nullable=False),
        sa.Column("objectives", sa.JSON(), nullable=False),
        sa.Column("max_auto_rounds", sa.Integer(), nullable=False),
        sa.Column("response_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_body_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("next_action", sa.String(32), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.JSON()),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("reason_detail_safe", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "expected_price_cny_minor >= 0",
            name="ck_procurement_execution_tasks_price_nonnegative",
        ),
        sa.CheckConstraint(
            "max_auto_rounds BETWEEN 1 AND 3",
            name="ck_procurement_execution_tasks_max_rounds",
        ),
    )
    op.create_index(
        "ix_procurement_execution_tasks_source_item_id",
        "procurement_execution_tasks",
        ["source_item_id"],
    )
    op.create_index(
        "ix_procurement_execution_tasks_status",
        "procurement_execution_tasks",
        ["status"],
    )

    op.create_table(
        "conversation_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("procurement_execution_tasks.task_id"),
            nullable=False,
        ),
        sa.Column("source_item_id", sa.String(64), nullable=False),
        sa.Column("item_url", sa.Text(), nullable=False),
        sa.Column("expected_seller_id", sa.String(128)),
        sa.Column("observed_seller_id", sa.String(128)),
        sa.Column("account_key", sa.String(64)),
        sa.Column("conversation_key", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("latest_inbound_message_id", sa.String(36)),
        sa.Column("latest_outbound_message_id", sa.String(36)),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail_safe", sa.Text()),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_sessions_task_id",
        "conversation_sessions",
        ["task_id"],
        unique=True,
    )
    op.create_index(
        "ix_conversation_sessions_source_item_id",
        "conversation_sessions",
        ["source_item_id"],
    )
    op.create_index(
        "ix_conversation_sessions_status", "conversation_sessions", ["status"]
    )

    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("conversation_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("sender_role", sa.String(16), nullable=False),
        sa.Column("external_message_id", sa.String(128)),
        sa.Column(
            "reply_to_message_id",
            sa.String(36),
            sa.ForeignKey("conversation_messages.message_id"),
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("llm_model", sa.String(100)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("llm_confidence", sa.Numeric(4, 3)),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("policy_version", sa.String(64)),
        sa.Column("policy_result", sa.String(32), nullable=False),
        sa.Column("policy_reason_codes", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("send_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "seq", name="uq_conversation_messages_session_seq"
        ),
        sa.UniqueConstraint(
            "session_id",
            "external_message_id",
            name="uq_conversation_messages_external_id",
        ),
    )
    op.create_index(
        "ix_conversation_messages_session_id", "conversation_messages", ["session_id"]
    )
    op.create_index("ix_conversation_messages_status", "conversation_messages", ["status"])

    op.create_table(
        "procurement_audit_logs",
        sa.Column("audit_id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("procurement_execution_tasks.task_id"),
            nullable=False,
        ),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("conversation_sessions.session_id")
        ),
        sa.Column(
            "message_id", sa.String(36), sa.ForeignKey("conversation_messages.message_id")
        ),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(64)),
        sa.Column("to_status", sa.String(64)),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("metadata_redacted", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            "action",
            name="uq_procurement_audit_idempotent_action",
        ),
    )
    op.create_index("ix_procurement_audit_logs_task_id", "procurement_audit_logs", ["task_id"])
    op.create_index(
        "ix_procurement_audit_logs_session_id", "procurement_audit_logs", ["session_id"]
    )
    op.create_index("ix_procurement_audit_logs_action", "procurement_audit_logs", ["action"])

    op.create_table(
        "procurement_outbox",
        sa.Column("outbox_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("procurement_execution_tasks.task_id"),
            nullable=False,
        ),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("conversation_sessions.session_id")
        ),
        sa.Column(
            "message_id", sa.String(36), sa.ForeignKey("conversation_messages.message_id")
        ),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String(64)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_error_safe", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "event_seq", name="uq_procurement_outbox_task_seq"),
    )
    op.create_index("ix_procurement_outbox_task_id", "procurement_outbox", ["task_id"])
    op.create_index("ix_procurement_outbox_status", "procurement_outbox", ["status"])
    op.create_index(
        "ix_procurement_outbox_next_attempt_at", "procurement_outbox", ["next_attempt_at"]
    )


def downgrade() -> None:
    """
    按外键依赖逆序删除采购聊天第一阶段数据结构。

    无输入输出；数据库错误向上抛出；副作用为删除本迁移新增的表和索引。
    """

    op.drop_index("ix_procurement_outbox_next_attempt_at", table_name="procurement_outbox")
    op.drop_index("ix_procurement_outbox_status", table_name="procurement_outbox")
    op.drop_index("ix_procurement_outbox_task_id", table_name="procurement_outbox")
    op.drop_table("procurement_outbox")
    op.drop_index("ix_procurement_audit_logs_action", table_name="procurement_audit_logs")
    op.drop_index("ix_procurement_audit_logs_session_id", table_name="procurement_audit_logs")
    op.drop_index("ix_procurement_audit_logs_task_id", table_name="procurement_audit_logs")
    op.drop_table("procurement_audit_logs")
    op.drop_index("ix_conversation_messages_status", table_name="conversation_messages")
    op.drop_index("ix_conversation_messages_session_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversation_sessions_status", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_source_item_id", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_task_id", table_name="conversation_sessions")
    op.drop_table("conversation_sessions")
    op.drop_index(
        "ix_procurement_execution_tasks_status", table_name="procurement_execution_tasks"
    )
    op.drop_index(
        "ix_procurement_execution_tasks_source_item_id",
        table_name="procurement_execution_tasks",
    )
    op.drop_table("procurement_execution_tasks")
