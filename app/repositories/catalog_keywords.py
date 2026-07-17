"""
本文件封装杂货铺采集清单的数据库读取与初始化。

它属于 repositories 模块，供调度器和只读 API 使用；不创建采集任务或访问闲鱼页面。
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog_keyword import CatalogKeyword

DEFAULT_CATALOG_KEYWORDS: tuple[tuple[str, str, str], ...] = (
    ("潮玩手办", "手办", "动漫、模型和小摆件"),
    ("潮玩手办", "盲盒", "未拆和二手盲盒"),
    ("潮玩手办", "动漫周边", "徽章、挂件和角色周边"),
    ("潮玩手办", "游戏周边", "游戏角色、卡带和周边"),
    ("潮玩手办", "拼装模型", "拼装和收藏模型"),
    ("潮玩手办", "奇趣摆件", "造型独特的桌面小物"),
    ("实用小物", "遥控器", "家电和设备配件"),
    ("实用小物", "鱼钩", "钓鱼相关小物"),
    ("实用小物", "五金工具", "家庭维修和工具配件"),
    ("实用小物", "露营装备", "户外和露营小物"),
    ("实用小物", "桌面收纳", "桌面和房间收纳小物"),
    ("实用小物", "创意小家电", "造型或用途独特的小电器"),
    ("怀旧收藏", "古董", "旧物和收藏品"),
    ("怀旧收藏", "老相机", "胶片与早期数码相机"),
    ("怀旧收藏", "磁带", "录音带、卡带和播放器"),
    ("怀旧收藏", "老游戏机", "掌机、主机和怀旧游戏"),
    ("怀旧收藏", "旧物件", "有年代感的生活小物"),
    ("怀旧收藏", "邮票", "邮品和纸质收藏"),
)


class CatalogKeywordRepository:
    """
    提供采集清单的查询、种子初始化与调度时间更新。

    输入 SQLAlchemy 会话；写方法提交事务；数据库异常会向上抛出。
    """

    def __init__(self, session: Session) -> None:
        """
        保存请求或调度周期使用的数据库会话。

        参数：session 为有效 SQLAlchemy 会话。返回：无。副作用：仅保存引用。
        """

        self.session = session

    def ensure_defaults(self) -> None:
        """
        写入缺失的默认杂货铺搜索清单，且不覆盖已有配置。

        返回：无。异常：数据库错误向上抛出。副作用：可能插入默认清单并提交事务。
        """

        existing = set(self.session.scalars(select(CatalogKeyword.keyword)))
        for category, keyword, note in DEFAULT_CATALOG_KEYWORDS:
            if keyword not in existing:
                self.session.add(CatalogKeyword(category=category, keyword=keyword, note=note))
        self.session.commit()

    def list_enabled(self) -> list[CatalogKeyword]:
        """
        返回当前全部启用的清单项。

        返回：按分类和关键词稳定排序的配置行。异常：数据库错误向上抛出。副作用：无。
        """

        return list(
            self.session.scalars(
                select(CatalogKeyword)
                .where(CatalogKeyword.is_enabled.is_(True))
                .order_by(CatalogKeyword.category.asc(), CatalogKeyword.keyword.asc())
            )
        )

    def get_next_due(
        self, now: datetime, excluded_keywords: set[str] | None = None
    ) -> CatalogKeyword | None:
        """
        返回一个已到期且最久未调度的搜索词。

        参数：now 为当前 UTC 时间、可选排除关键词。返回配置行或 None；数据库错误向上抛出。
        无写入副作用。
        """

        excluded = excluded_keywords or set()
        rows = [row for row in self.list_enabled() if row.keyword not in excluded]
        due_rows = [
            row
            for row in rows
            if row.next_due_at is None
            and (
                row.last_scheduled_at is None
                or row.last_scheduled_at + timedelta(minutes=row.interval_minutes) <= now
            )
            or row.next_due_at is not None
            and row.next_due_at <= now
        ]
        oldest = datetime.min.replace(tzinfo=now.tzinfo)
        return min(due_rows, key=lambda row: row.last_scheduled_at or oldest, default=None)

    def mark_scheduled(self, keyword_id: int, now: datetime, *, commit: bool = True) -> None:
        """
        标记一个搜索词已进入采集队列。

        参数：keyword_id 为配置主键，now 为调度时间。
        返回：无。异常：不存在时抛出 ValueError。副作用：更新并可选择提交。
        """

        row = self.session.get(CatalogKeyword, keyword_id)
        if row is None:
            raise ValueError("采集清单不存在")
        row.last_scheduled_at = now
        row.next_due_at = now + timedelta(minutes=row.interval_minutes)
        if commit:
            self.session.commit()
