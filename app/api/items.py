"""
本文件提供商品分页与详情 API。

它属于 api 模块，只做查询参数校验和响应序列化，不写商品或访问闲鱼。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.items import ItemRepository
from app.schemas.item import ItemPage, ItemRead

router = APIRouter(prefix="/api/v1/items", tags=["items"])


@router.get("", response_model=ItemPage)
def list_items(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=100),
    session: Session = Depends(get_db),
) -> ItemPage:
    """
    返回数据库商品分页，可按关键词过滤。

    输入分页参数和会话，返回分页对象；数据库错误向上抛出；无写入副作用。
    """

    rows, total, pages = ItemRepository(session).list_page(page, page_size, keyword)
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
