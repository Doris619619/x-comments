"""
本文件提供仅供 shopping 服务端读取的版本化 Catalog Sync API。

它属于 api 模块，只做令牌校验、参数校验和响应序列化；不写商品、不启动采集，也不暴露
数据库连接、登录态或闲鱼原始链接。
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.catalog_sync import CatalogChange
from app.repositories.catalog_sync import CatalogSyncRepository, FullResyncRequiredError
from app.schemas.catalog_sync import (
    CatalogChangePageRead,
    CatalogChangeRead,
    CatalogRevisionRead,
    CatalogSyncItemRead,
    CatalogSyncSnapshotPageRead,
)

router = APIRouter(prefix="/api/v1/catalog-sync", tags=["catalog-sync"])


def require_catalog_sync_token(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """
    校验 shopping 调用同步接口所需的服务端 Bearer 令牌。

    输入请求和 Authorization；未配置时抛出 503，令牌错误时抛出 401；不写数据库。
    """

    configured_token = getattr(request.app.state, "catalog_sync_token", None)
    if not isinstance(configured_token, str) or not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog Sync 接口未配置",
        )
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    authorized = (
        bool(separator)
        and scheme.casefold() == "bearer"
        and bool(supplied_token.strip())
        and secrets.compare_digest(supplied_token.strip(), configured_token)
    )
    if not authorized:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Catalog Sync 令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def to_change_read(change: CatalogChange) -> CatalogChangeRead:
    """
    将 ORM 增量事件收敛为跨服务稳定响应字段。

    输入已发布变更；返回响应模型；字段校验失败向上抛出，无写入副作用。
    """

    return CatalogChangeRead(
        revision=change.revision,
        change_type=change.change_type,
        item_id=change.item_id,
        availability=change.availability,
        title=change.title,
        price=change.price,
        currency=change.currency,
        image_url=change.image_url,
        image_urls=list(change.image_urls or []),
        location=change.location,
        last_seen_at=change.last_seen_at,
        status_changed_at=change.status_changed_at,
    )


@router.get("/revisions/latest", response_model=CatalogRevisionRead)
def get_latest_revision(
    _authorized: None = Depends(require_catalog_sync_token),
    session: Session = Depends(get_db),
) -> CatalogRevisionRead:
    """
    返回最近可读取 revision，空目录返回稳定的 revision 0。

    输入认证与会话；返回版本；数据库异常向上抛出，无写入副作用。
    """

    revision = CatalogSyncRepository(session).latest_revision()
    if revision is None:
        return CatalogRevisionRead(revision=0, published_at=None, status="empty")
    return CatalogRevisionRead(
        revision=revision.revision,
        published_at=revision.published_at,
        status=revision.status,
    )


@router.get("/changes", response_model=CatalogChangePageRead)
def get_changes(
    after_revision: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=500),
    _authorized: None = Depends(require_catalog_sync_token),
    session: Session = Depends(get_db),
) -> CatalogChangePageRead:
    """
    返回指定游标后的完整 revision 边界变更页。

    输入游标、上限、认证和会话；游标过期时抛出 409；无写入副作用。
    """

    try:
        page = CatalogSyncRepository(session).list_changes(after_revision, limit)
    except FullResyncRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="同步游标已失效，需要全量重建",
        ) from exc
    return CatalogChangePageRead(
        from_revision=page.from_revision,
        to_revision=page.to_revision,
        has_more=page.has_more,
        changes=[to_change_read(change) for change in page.changes],
    )


@router.get("/items", response_model=CatalogSyncSnapshotPageRead)
def list_catalog_sync_snapshot(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _authorized: None = Depends(require_catalog_sync_token),
    session: Session = Depends(get_db),
) -> CatalogSyncSnapshotPageRead:
    """
    返回每个商品最近一次已发布快照的分页全量同步页面。

    输入分页、认证和会话；返回稳定页面；数据库异常向上抛出，无写入副作用。
    """

    snapshot = CatalogSyncRepository(session).list_snapshot_page(page, page_size)
    return CatalogSyncSnapshotPageRead(
        items=[to_change_read(change) for change in snapshot.items],
        page=snapshot.page,
        page_size=snapshot.page_size,
        total=snapshot.total,
        pages=snapshot.pages,
    )


@router.get("/items/{item_id}", response_model=CatalogSyncItemRead)
def get_catalog_sync_item(
    item_id: str,
    _authorized: None = Depends(require_catalog_sync_token),
    session: Session = Depends(get_db),
) -> CatalogSyncItemRead:
    """
    返回某商品最新已发布同步快照。

    输入商品 ID、认证和会话；不存在时抛出 404；无写入副作用。
    """

    change = CatalogSyncRepository(session).get_latest_item_change(item_id)
    if change is None:
        raise HTTPException(status_code=404, detail="同步商品不存在")
    return CatalogSyncItemRead(
        revision=change.revision,
        item_id=change.item_id,
        availability=change.availability,
        title=change.title,
        price=change.price,
        currency=change.currency,
        image_url=change.image_url,
        image_urls=list(change.image_urls or []),
        location=change.location,
        last_seen_at=change.last_seen_at,
        status_changed_at=change.status_changed_at,
    )
