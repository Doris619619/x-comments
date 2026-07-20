"""
本文件声明采购 AI 提示词包及其稳定版本入口。

它只导出提示词构造器，不调用模型、不写日志，也不执行聊天发送或资金动作。
"""

from app.ai.prompts.procurement_v1 import PROMPT_VERSION, build_procurement_messages

__all__ = ["PROMPT_VERSION", "build_procurement_messages"]
