"""
本文件提供健康检查 API。

它属于 api 模块，只验证应用和数据库连通性，不执行采集任务。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db

router = APIRouter()


@router.get("/health", tags=["system"])
def health(session: Session = Depends(get_db)) -> dict[str, str]:
    """
    返回应用和数据库健康状态。

    输入请求级会话；数据库失败时由 FastAPI 返回错误；只执行只读查询。
    """

    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
