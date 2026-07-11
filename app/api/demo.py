"""
本文件提供 POC 内部演示页入口和静态资源挂载。

它属于 api 模块，只返回前端文件，不负责采集、查询或业务状态判断。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/")
def demo_page() -> FileResponse:
    """
    返回 POC internal demo 首页。

    无输入，返回 HTML 文件；文件缺失时由框架抛出异常；只读取静态文件。
    """

    return FileResponse(STATIC_DIR / "index.html")
