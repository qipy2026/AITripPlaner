"""兼容层：请使用 app.core.llm。"""

from ..core.llm import get_chat_model, invoke_chat

__all__ = ["get_chat_model", "invoke_chat"]
