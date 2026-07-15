"""
本文件提供杂货铺采集清单的只读 API。

它属于 api 模块，只做响应序列化；不修改配置、不创建采集任务，也不访问闲鱼。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.catalog_keywords import CatalogKeywordRepository
from app.schemas.catalog_keyword import CatalogKeywordRead

router = APIRouter(prefix="/api/v1/catalog-keywords", tags=["catalog-keywords"])


@router.get("", response_model=list[CatalogKeywordRead])
def list_catalog_keywords(session: Session = Depends(get_db)) -> list[CatalogKeywordRead]:
    """
    返回已启用的杂货铺搜索清单。

    参数：session 为请求数据库会话。返回：只读清单。异常：数据库错误向上抛出。副作用：无。
    """

    return [
        CatalogKeywordRead.model_validate(row)
        for row in CatalogKeywordRepository(session).list_enabled()
    ]
