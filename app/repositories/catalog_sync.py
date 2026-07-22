"""
本文件封装 Catalog Sync 的采集批次、原子发布和增量读取操作。

它属于 repositories 模块，负责把完整采集结果转换为可同步版本；不访问 Playwright、
不解析页面，也不构造 HTTP 响应。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.catalog_keyword import CatalogKeyword
from app.models.catalog_sync import (
    CatalogAvailability,
    CatalogChange,
    CatalogChangeType,
    CatalogItemState,
    CatalogRevision,
    CrawlRun,
    CrawlRunStatus,
)
from app.models.item import Item
from app.repositories.items import ItemRepository, UpsertStats
from app.schemas.item import ParsedItem


@dataclass(frozen=True)
class PublishedCatalogRun:
    """
    表示一次完整采集发布后的写入统计和 revision。

    由发布仓储返回；没有副作用，不负责 HTTP 序列化。
    """

    stats: UpsertStats
    revision: int | None


@dataclass(frozen=True)
class CatalogChangePage:
    """
    表示按完整 revision 边界截取的增量变更页。

    由同步仓储返回；调用方可把 to_revision 持久化为下次同步游标。
    """

    from_revision: int
    to_revision: int
    has_more: bool
    changes: list[CatalogChange]


@dataclass(frozen=True)
class CatalogSnapshotPage:
    """
    表示由每个商品最新发布快照组成的分页全量重建结果。

    仅在增量游标失效后供 shopping 重建镜像使用；不会写入数据库。
    """

    items: list[CatalogChange]
    page: int
    page_size: int
    total: int
    pages: int


class FullResyncRequiredError(Exception):
    """
    表示消费者游标已早于保留的最小 revision 或超过当前版本。

    API 层将该异常映射为 409；异常本身不写数据库。
    """


class CatalogSyncRepository:
    """
    封装 Catalog Sync 的写入事务与只读增量查询。

    输入 SQLAlchemy 会话；发布方法不自行提交，以便 worker 把任务终态和 revision 放入同一事务。
    """

    def __init__(self, session: Session) -> None:
        """
        保存任务级数据库会话。

        输入有效会话；无返回；副作用仅为保存引用。
        """

        self.session = session

    def begin_run(self, job_id: str, keyword: str, started_at: datetime) -> CrawlRun:
        """
        创建并提交一条运行中采集批次，按关键词关联可选清单项。

        输入任务 ID、关键词和开始时间；返回新批次；数据库错误向上抛出，副作用为短事务写入。
        """

        catalog_keyword = self.session.scalar(
            select(CatalogKeyword).where(CatalogKeyword.keyword == keyword)
        )
        run = CrawlRun(
            job_id=job_id,
            catalog_keyword_id=catalog_keyword.id if catalog_keyword is not None else None,
            status=CrawlRunStatus.RUNNING,
            started_at=started_at,
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def publish_complete_run(
        self,
        run_id: str,
        keyword: str,
        items: list[ParsedItem],
        seen_at: datetime,
        missing_threshold: int,
    ) -> PublishedCatalogRun:
        """
        在调用方事务中写入完整结果、状态变化和单个 revision。

        输入运行、关键词、解析结果、观察时间与缺失阈值；配置关键词不存在时仅写商品并返回
        revision 为 None；调用方必须提交或回滚，异常会向上抛出。
        """

        if missing_threshold < 2:
            raise ValueError("缺失阈值不得小于 2")
        run = self.session.get(CrawlRun, run_id)
        if run is None:
            raise ValueError("采集批次不存在")

        stats = ItemRepository(self.session).upsert_many(keyword, items, seen_at, commit=False)
        if run.catalog_keyword_id is None:
            run.status = CrawlRunStatus.SUCCEEDED
            run.is_comparable = False
            run.finished_at = seen_at
            return PublishedCatalogRun(stats=stats, revision=None)

        catalog_keyword_id = run.catalog_keyword_id
        states = list(
            self.session.scalars(
                select(CatalogItemState).where(
                    CatalogItemState.catalog_keyword_id == catalog_keyword_id
                )
            )
        )
        states_by_item_id = {state.item_id: state for state in states}
        missing_states = [state for state in states if state.item_id not in stats.seen_item_ids]
        affected_item_ids = set(stats.seen_item_ids) | {state.item_id for state in missing_states}
        previous_availability = {
            item_id: self._global_availability(item_id) for item_id in affected_item_ids
        }

        for item_id in stats.seen_item_ids:
            state = states_by_item_id.get(item_id)
            if state is None:
                state = CatalogItemState(
                    item_id=item_id,
                    catalog_keyword_id=catalog_keyword_id,
                    availability=CatalogAvailability.ACTIVE,
                    missing_count=0,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    last_checked_run_id=run_id,
                    status_changed_at=seen_at,
                )
                self.session.add(state)
            else:
                state_was_changed = state.availability is not CatalogAvailability.ACTIVE
                state.availability = CatalogAvailability.ACTIVE
                state.missing_count = 0
                state.last_seen_at = seen_at
                state.last_checked_run_id = run_id
                if state_was_changed:
                    state.status_changed_at = seen_at

        for state in missing_states:
            state.missing_count += 1
            state.last_checked_run_id = run_id
            next_availability = (
                CatalogAvailability.OFF_SHELF
                if state.missing_count >= missing_threshold
                else CatalogAvailability.SUSPECTED_MISSING
            )
            if state.availability is not next_availability:
                state.availability = next_availability
                state.status_changed_at = seen_at

        run.status = CrawlRunStatus.SUCCEEDED
        run.is_comparable = True
        run.finished_at = seen_at
        catalog_keyword = self.session.get(CatalogKeyword, catalog_keyword_id)
        if catalog_keyword is not None:
            catalog_keyword.last_completed_at = seen_at
        self.session.flush()

        revision = CatalogRevision(source_run_id=run_id, published_at=seen_at, status="published")
        self.session.add(revision)
        self.session.flush()

        for item_id in sorted(affected_item_ids):
            item = self.session.get(Item, item_id)
            if item is None:
                raise ValueError("已发布商品不存在")
            availability = self._global_availability(item_id)
            if (
                item_id not in stats.seen_item_ids
                and previous_availability[item_id] is availability
            ):
                continue
            change_type = (
                CatalogChangeType.AVAILABILITY_CHANGED
                if previous_availability[item_id] is not availability
                and item_id not in stats.seen_item_ids
                else CatalogChangeType.UPSERT
            )
            self.session.add(
                CatalogChange(
                    revision=revision.revision,
                    item_id=item_id,
                    change_type=change_type,
                    availability=availability,
                    title=item.title,
                    price=item.price,
                    currency="CNY",
                    image_url=item.image_url,
                    image_urls=list(item.image_urls or []),
                    location=item.location,
                    last_seen_at=item.last_seen_at,
                    status_changed_at=self._latest_status_changed_at(item_id, item.updated_at),
                    occurred_at=seen_at,
                )
            )
        return PublishedCatalogRun(stats=stats, revision=revision.revision)

    def finish_incomplete_run(
        self,
        run_id: str,
        keyword: str,
        items: list[ParsedItem],
        finished_at: datetime,
        error_message: str | None,
    ) -> UpsertStats:
        """
        写入部分成功时实际看到的商品，但不改变缺失状态或发布 revision。

        输入运行、商品、结束时间和错误信息；返回写入统计；调用方必须提交或回滚。
        """

        run = self.session.get(CrawlRun, run_id)
        if run is None:
            raise ValueError("采集批次不存在")
        stats = ItemRepository(self.session).upsert_many(keyword, items, finished_at, commit=False)
        run.status = CrawlRunStatus.PARTIALLY_SUCCEEDED
        run.is_comparable = False
        run.finished_at = finished_at
        run.error_message = error_message
        return stats

    def finish_failed_run(
        self, job_id: str, status: CrawlRunStatus, message: str, finished_at: datetime
    ) -> None:
        """
        将已有采集批次写为失败或风控终态且不发布 revision。

        输入任务、终态、脱敏消息和结束时间；找不到批次时安全返回；调用方必须提交。
        """

        run = self.session.scalar(select(CrawlRun).where(CrawlRun.job_id == job_id))
        if run is None:
            return
        run.status = status
        run.is_comparable = False
        run.finished_at = finished_at
        run.error_message = message[:1000]

    def latest_revision(self) -> CatalogRevision | None:
        """
        返回最近一次已发布 revision，空目录时返回 None。

        无输入；数据库异常向上抛出；无写入副作用。
        """

        return self.session.scalar(
            select(CatalogRevision).order_by(CatalogRevision.revision.desc()).limit(1)
        )

    def list_changes(self, after_revision: int, limit: int) -> CatalogChangePage:
        """
        从指定游标后按完整 revision 边界读取最多 limit 条变更。

        输入游标和上限；游标不再可恢复时抛出 FullResyncRequiredError；无写入副作用。
        """

        latest = self.latest_revision()
        latest_revision = latest.revision if latest is not None else 0
        minimum_revision = self.session.scalar(select(func.min(CatalogRevision.revision)))
        if after_revision > latest_revision or (
            minimum_revision is not None and after_revision < int(minimum_revision) - 1
        ):
            raise FullResyncRequiredError("同步游标已失效，需要全量重建")

        revisions = list(
            self.session.scalars(
                select(CatalogRevision.revision)
                .where(CatalogRevision.revision > after_revision)
                .order_by(CatalogRevision.revision.asc())
            )
        )
        collected: list[CatalogChange] = []
        to_revision = after_revision
        processed_count = 0
        for revision in revisions:
            revision_changes = list(
                self.session.scalars(
                    select(CatalogChange)
                    .where(CatalogChange.revision == revision)
                    .order_by(CatalogChange.item_id.asc())
                )
            )
            if collected and len(collected) + len(revision_changes) > limit:
                break
            collected.extend(revision_changes)
            to_revision = revision
            processed_count += 1
        return CatalogChangePage(
            from_revision=after_revision,
            to_revision=to_revision,
            has_more=processed_count < len(revisions),
            changes=collected,
        )

    def get_latest_item_change(self, item_id: str) -> CatalogChange | None:
        """
        返回某商品最近一次同步快照，不存在时返回 None。

        输入商品 ID；数据库异常向上抛出；无写入副作用。
        """

        return self.session.scalar(
            select(CatalogChange)
            .where(CatalogChange.item_id == item_id)
            .order_by(CatalogChange.revision.desc())
            .limit(1)
        )

    def list_snapshot_page(self, page: int, page_size: int) -> CatalogSnapshotPage:
        """
        返回每个已发布商品的最新快照，用于 409 后的分页全量同步。

        输入页码和页大小；返回稳定排序页面；数据库异常向上抛出，无写入副作用。
        """

        latest_per_item = (
            select(
                CatalogChange.item_id.label("item_id"),
                func.max(CatalogChange.revision).label("revision"),
            )
            .group_by(CatalogChange.item_id)
            .subquery()
        )
        total = int(
            self.session.scalar(select(func.count()).select_from(latest_per_item)) or 0
        )
        rows = list(
            self.session.scalars(
                select(CatalogChange)
                .join(
                    latest_per_item,
                    and_(
                        CatalogChange.item_id == latest_per_item.c.item_id,
                        CatalogChange.revision == latest_per_item.c.revision,
                    ),
                )
                .order_by(CatalogChange.item_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        pages = (total + page_size - 1) // page_size if total else 0
        return CatalogSnapshotPage(
            items=rows,
            page=page,
            page_size=page_size,
            total=total,
            pages=pages,
        )

    def _global_availability(self, item_id: str) -> CatalogAvailability:
        """
        根据一个商品的全部清单关联计算公开可用状态。

        输入商品 ID；返回状态；数据库异常向上抛出，无写入副作用。
        """

        values = set(
            self.session.scalars(
                select(CatalogItemState.availability).where(CatalogItemState.item_id == item_id)
            )
        )
        if CatalogAvailability.ACTIVE in values:
            return CatalogAvailability.ACTIVE
        if CatalogAvailability.SUSPECTED_MISSING in values:
            return CatalogAvailability.SUSPECTED_MISSING
        if CatalogAvailability.SOLD in values:
            return CatalogAvailability.SOLD
        if CatalogAvailability.OFF_SHELF in values:
            return CatalogAvailability.OFF_SHELF
        return CatalogAvailability.UNKNOWN

    def _latest_status_changed_at(self, item_id: str, fallback: datetime) -> datetime:
        """
        返回商品全部清单关联中最近一次状态变化时间。

        输入商品 ID 和无关联时的回退时间；数据库异常向上抛出；无写入副作用。
        """

        value = self.session.scalar(
            select(func.max(CatalogItemState.status_changed_at)).where(
                CatalogItemState.item_id == item_id
            )
        )
        return value or fallback
