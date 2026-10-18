"""The chat agent: deterministic filters, an LLM router, and six typed tools.

Spec 13. The LLM routes and explains; it never computes (CLAUDE.md
non-negotiable 1). Dispatch is plain Python -- the model picks a route, and
Python calls the solver. Every number the athlete reads came from the
generator, a solver, the simulator or the database.

The public surface is `handle_message`, which `api/routes/chat.py` calls once
per turn. Everything else is here because something in this package needs it.
"""

from domains.strength.agent.agent import ChatTurn, conversation_messages, handle_message
from domains.strength.agent.schemas import (
    HISTORY_LIMIT,
    ChatModels,
    Route,
    RouterChoice,
    ToolCall,
)

__all__ = [
    "HISTORY_LIMIT",
    "ChatModels",
    "ChatTurn",
    "Route",
    "RouterChoice",
    "ToolCall",
    "conversation_messages",
    "handle_message",
]
