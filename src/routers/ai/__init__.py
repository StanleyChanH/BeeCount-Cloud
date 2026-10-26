"""AI 子路由聚合。

- A1: /ask — 文档 Q&A,见 .docs/web-cmdk-ai-doc-search.md
- A2: /agent/chat — 悬浮财务助手(工具调用 Agent),见 agent_chat.py
- B2: /parse-tx-image — 截图记账,见 .docs/web-cmdk-ai-paste-screenshot.md
- B3: /parse-tx-text — 文字记账,见 .docs/web-cmdk-ai-paste-text.md
"""
from fastapi import APIRouter

from . import agent_chat, ask, parse_tx_image, parse_tx_text, test_provider

router = APIRouter()
router.include_router(agent_chat.router)
router.include_router(ask.router)
router.include_router(parse_tx_image.router)
router.include_router(parse_tx_text.router)
router.include_router(test_provider.router)

__all__ = ["router"]
