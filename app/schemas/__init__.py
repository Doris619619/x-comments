"""
本包集中定义 API 和服务之间传递的 Pydantic 数据结构。

它不访问数据库、页面或网络。
"""

from app.schemas.procurement import (
    ConversationMessageRead,
    ConversationSessionRead,
    ProcurementAutomationPolicy,
    ProcurementEvent,
    ProcurementEventType,
    ProcurementExecutionStatus,
    ProcurementExecutionTaskRead,
    ProcurementExpectedListing,
    ProcurementMessagePage,
    ProcurementObjective,
    ProcurementSource,
    ProcurementTaskAccepted,
    ProcurementTaskCancel,
    ProcurementTaskCreate,
)
from app.schemas.procurement_llm import (
    ProcurementAccessoriesStatus,
    ProcurementAvailability,
    ProcurementDecision,
    ProcurementFacts,
    ProcurementFunctionalStatus,
    ProcurementIntent,
    ProcurementLlmOutput,
    ProcurementRiskFlag,
)

__all__ = [
    "ConversationMessageRead",
    "ConversationSessionRead",
    "ProcurementAccessoriesStatus",
    "ProcurementAutomationPolicy",
    "ProcurementAvailability",
    "ProcurementDecision",
    "ProcurementEvent",
    "ProcurementEventType",
    "ProcurementExecutionTaskRead",
    "ProcurementExecutionStatus",
    "ProcurementExpectedListing",
    "ProcurementFacts",
    "ProcurementFunctionalStatus",
    "ProcurementIntent",
    "ProcurementLlmOutput",
    "ProcurementMessagePage",
    "ProcurementObjective",
    "ProcurementRiskFlag",
    "ProcurementSource",
    "ProcurementTaskAccepted",
    "ProcurementTaskCancel",
    "ProcurementTaskCreate",
]
