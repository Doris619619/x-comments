"""
本文件提供商品分页与详情 API。

它属于 api 模块，只做查询参数校验和响应序列化，不写商品或访问闲鱼。
"""

import secrets
from typing import cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.items import ItemRepository
from app.schemas.item import ItemPage, ItemRead
from app.schemas.item_verification import ItemVerifyRequest, ItemVerifyResponse
from app.services.item_verification import (
    ItemVerificationService,
    LiveItemVerifier,
)

router = APIRouter(prefix="/api/v1/items", tags=["items"])


def require_item_verification_token(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """
    校验核验接口的服务端 Bearer 令牌。

    输入请求与 Authorization；配置缺失时抛出 503，令牌缺失或错误时抛出 401；不写数据。
    """

    configured_token = getattr(request.app.state, "item_verification_token", None)
    if not isinstance(configured_token, str) or not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="商品核验接口未配置",
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
            detail="商品核验令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_item_verification_service(
    request: Request, session: Session = Depends(get_db)
) -> ItemVerificationService:
    """
    为一次核验请求装配只读商品仓储和应用级可注入核验器。

    输入请求与数据库会话；返回核验服务；依赖缺失或数据库异常向上抛出，无立即网络访问。
    """

    verifier = cast(LiveItemVerifier, request.app.state.item_verifier)
    return ItemVerificationService(ItemRepository(session), verifier)


@router.get("", response_model=ItemPage)
def list_items(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=64),
    session: Session = Depends(get_db),
) -> ItemPage:
    """
    返回数据库商品分页，可按关键词或杂货铺分类过滤。

    输入分页参数和会话，返回分页对象；数据库错误向上抛出；无写入副作用。
    """

    rows, total, pages = ItemRepository(session).list_page(page, page_size, keyword, category)
    return ItemPage(
        items=[ItemRead.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.get("/{item_id}", response_model=ItemRead)
def get_item(item_id: str, session: Session = Depends(get_db)) -> object:
    """
    返回单个数据库商品。

    输入商品 ID；不存在时抛出 404；无写入副作用。
    """

    item = ItemRepository(session).get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return item


@router.post("/{item_id}/verify", response_model=ItemVerifyResponse)
async def verify_item(
    item_id: str,
    payload: ItemVerifyRequest,
    _authorized: None = Depends(require_item_verification_token),
    service: ItemVerificationService = Depends(get_item_verification_service),
) -> ItemVerifyResponse:
    """
    为商城结算执行一次单商品实时详情核验。

    输入商品 ID 与来源价格快照；返回五状态响应；本地商品不存在时抛出 404；会访问一次详情页。
    """

    result = await service.verify(item_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return result
