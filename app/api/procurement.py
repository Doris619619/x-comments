"""
本文件提供仅供 shopping 服务端调用的本地采购任务创建、查询和取消 API。

它属于 api 模块，只负责 Bearer 鉴权、请求校验、服务装配和错误映射；不调用大模型、
不操作 Playwright，也不执行购买、付款或真实消息发送。
"""

import secrets
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.procurement import ProcurementRepository
from app.schemas.procurement import (
    ProcurementExecutionTaskRead,
    ProcurementTaskAccepted,
    ProcurementTaskCancel,
    ProcurementTaskCreate,
)
from app.services.procurement import (
    ProcurementDataIntegrityError,
    ProcurementExecutionService,
    ProcurementIdempotencyConflictError,
    ProcurementInvalidStateError,
    ProcurementServiceError,
    ProcurementSourceItemNotFoundError,
    ProcurementSourcePriceChangedError,
    ProcurementSourceUnavailableError,
    ProcurementTaskConflictError,
    ProcurementTaskNotFoundError,
    ProcurementTaskResult,
)

router = APIRouter(prefix="/api/v1/procurement-tasks", tags=["procurement"])


def require_procurement_api_token(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """
    使用恒定时间比较校验独立的采购服务端 Bearer 令牌。

    输入请求和 Authorization；令牌未配置抛出 503，缺失或错误抛出 401；无写入副作用。
    """

    configured_token = getattr(request.app.state, "procurement_api_token", None)
    if not isinstance(configured_token, str) or not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="采购任务接口未配置",
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
            detail="采购任务令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_procurement_service(
    session: Session = Depends(get_db),
) -> ProcurementExecutionService:
    """
    为一次请求装配采购仓储和业务服务。

    输入请求级数据库会话，返回服务对象；数据库配置错误向上抛出，无立即写入副作用。
    """

    return ProcurementExecutionService(ProcurementRepository(session))


def to_task_read(result: ProcurementTaskResult) -> ProcurementExecutionTaskRead:
    """
    将执行任务和会话转换为不暴露幂等键、哈希、租约或 URL 的内部响应。

    输入服务结果，返回严格响应模型；字段不一致时抛出 ValidationError，无写入副作用。
    """

    task = result.task
    return ProcurementExecutionTaskRead(
        task_id=task.task_id,
        session_id=result.conversation.session_id,
        source_item_id=task.source_item_id,
        expected_title=task.expected_title,
        expected_price_cny_minor=task.expected_price_cny_minor,
        objectives=task.objectives,
        max_auto_rounds=task.max_auto_rounds,
        response_deadline_at=task.response_deadline_at,
        status=task.status,
        next_action=task.next_action,
        session_status=result.conversation.status,
        summary=task.summary,
        reason_code=task.reason_code,
        reason_detail_safe=task.reason_detail_safe,
        created_at=task.created_at,
        updated_at=task.updated_at,
        cancelled_at=task.cancelled_at,
        completed_at=task.completed_at,
    )


def raise_procurement_http_error(error: ProcurementServiceError) -> NoReturn:
    """
    将稳定采购业务异常映射为 HTTP 状态码和机器可读 code。

    输入业务异常并始终抛出 HTTPException；不写数据库，也不记录请求敏感内容。
    """

    if isinstance(error, (ProcurementSourceItemNotFoundError, ProcurementTaskNotFoundError)):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(
        error,
        (
            ProcurementIdempotencyConflictError,
            ProcurementTaskConflictError,
            ProcurementSourceUnavailableError,
            ProcurementSourcePriceChangedError,
            ProcurementInvalidStateError,
        ),
    ):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(error, ProcurementDataIntegrityError):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    raise HTTPException(
        status_code=http_status,
        detail={"code": error.code, "message": str(error)},
    ) from error


@router.post("", response_model=ProcurementTaskAccepted, status_code=status.HTTP_202_ACCEPTED)
def create_procurement_task(
    payload: ProcurementTaskCreate,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    _authorized: None = Depends(require_procurement_api_token),
    service: ProcurementExecutionService = Depends(get_procurement_service),
) -> ProcurementTaskAccepted:
    """
    幂等创建经本地 active/CNY/价格校验的采购执行任务与聊天会话。

    输入严格请求、幂等键和认证；成功返回 202；冲突、缺失或价格变化映射为明确错误。
    """

    try:
        result = service.create(payload, idempotency_key)
    except ProcurementServiceError as exc:
        raise_procurement_http_error(exc)
    return ProcurementTaskAccepted(
        task_id=result.task.task_id,
        session_id=result.conversation.session_id,
        status=result.task.status,
        next_action=result.task.next_action,
        created_at=result.task.created_at,
    )


@router.get("/{task_id}", response_model=ProcurementExecutionTaskRead)
def get_procurement_task(
    task_id: UUID,
    _authorized: None = Depends(require_procurement_api_token),
    service: ProcurementExecutionService = Depends(get_procurement_service),
) -> ProcurementExecutionTaskRead:
    """
    返回指定本地采购执行任务和会话的当前状态。

    输入任务 UUID 和认证；不存在返回 404；不执行外部访问或写入。
    """

    try:
        result = service.get(str(task_id))
    except ProcurementServiceError as exc:
        raise_procurement_http_error(exc)
    return to_task_read(result)


@router.post("/{task_id}/cancel", response_model=ProcurementExecutionTaskRead)
def cancel_procurement_task(
    task_id: UUID,
    payload: ProcurementTaskCancel,
    _authorized: None = Depends(require_procurement_api_token),
    service: ProcurementExecutionService = Depends(get_procurement_service),
) -> ProcurementExecutionTaskRead:
    """
    幂等取消尚未进入其他终态的本地采购执行任务和会话。

    输入任务 UUID、稳定原因码和认证；返回取消后详情；不会购买、付款或操作页面。
    """

    try:
        result = service.cancel(str(task_id), payload.reason_code)
    except ProcurementServiceError as exc:
        raise_procurement_http_error(exc)
    return to_task_read(result)
