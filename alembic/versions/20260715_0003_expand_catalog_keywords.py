"""
本文件扩充杂货铺搜索清单，并将旧的细分类归并为首页使用的三个分类。

它属于 Alembic 迁移层，只调整持久化运营配置；不执行采集、访问网页或删除已有商品。
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0003"
down_revision: str | None = "20260715_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """归并旧分类，并插入新增的杂货铺搜索词。"""

    catalog_keywords = sa.table(
        "catalog_keywords",
        sa.column("category", sa.String()),
        sa.column("keyword", sa.String()),
        sa.column("note", sa.Text()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    op.execute(
        "UPDATE catalog_keywords SET category = '潮玩手办' WHERE keyword IN ('手办', '奇趣摆件')"
    )
    op.execute(
        "UPDATE catalog_keywords SET category = '实用小物' WHERE keyword IN ('遥控器', '鱼钩')"
    )
    op.execute("UPDATE catalog_keywords SET category = '怀旧收藏' WHERE keyword = '古董'")
    now = datetime.now(UTC)
    op.bulk_insert(
        catalog_keywords,
        [
            {
                "category": "潮玩手办",
                "keyword": "盲盒",
                "note": "未拆和二手盲盒",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "潮玩手办",
                "keyword": "动漫周边",
                "note": "徽章、挂件和角色周边",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "潮玩手办",
                "keyword": "游戏周边",
                "note": "游戏角色、卡带和周边",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "潮玩手办",
                "keyword": "拼装模型",
                "note": "拼装和收藏模型",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "实用小物",
                "keyword": "五金工具",
                "note": "家庭维修和工具配件",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "实用小物",
                "keyword": "露营装备",
                "note": "户外和露营小物",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "实用小物",
                "keyword": "桌面收纳",
                "note": "桌面和房间收纳小物",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "实用小物",
                "keyword": "创意小家电",
                "note": "造型或用途独特的小电器",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "怀旧收藏",
                "keyword": "老相机",
                "note": "胶片与早期数码相机",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "怀旧收藏",
                "keyword": "磁带",
                "note": "录音带、卡带和播放器",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "怀旧收藏",
                "keyword": "老游戏机",
                "note": "掌机、主机和怀旧游戏",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "怀旧收藏",
                "keyword": "旧物件",
                "note": "有年代感的生活小物",
                "created_at": now,
                "updated_at": now,
            },
            {
                "category": "怀旧收藏",
                "keyword": "邮票",
                "note": "邮品和纸质收藏",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    """删除新增词并恢复旧的五个分类标签。"""

    op.execute(
        "DELETE FROM catalog_keywords WHERE keyword IN "
        "('盲盒', '动漫周边', '游戏周边', '拼装模型', '五金工具', '露营装备', '桌面收纳', "
        "'创意小家电', '老相机', '磁带', '老游戏机', '旧物件', '邮票')"
    )
    op.execute("UPDATE catalog_keywords SET category = '潮玩收藏' WHERE keyword = '手办'")
    op.execute("UPDATE catalog_keywords SET category = '桌面趣物' WHERE keyword = '奇趣摆件'")
    op.execute("UPDATE catalog_keywords SET category = '实用小物' WHERE keyword = '遥控器'")
    op.execute("UPDATE catalog_keywords SET category = '户外渔具' WHERE keyword = '鱼钩'")
    op.execute("UPDATE catalog_keywords SET category = '怀旧收藏' WHERE keyword = '古董'")
