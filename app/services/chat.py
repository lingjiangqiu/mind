from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent, PhilosophyStreamEvent
from app.services.ai import AiClient, split_text


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        outcome = self.agent_harness.run(user, request)
        yield sse("meta", ChatStreamEvent(type="meta", sessionId=outcome.session.public_id).model_dump(by_alias=True))
        assistant = []
        if outcome.philosophy is not None and outcome.prepared_reply:
            # Low/medium-risk consultation: the reply was validated before this point,
            # so stream the checked text rather than generating new free text here.
            yield sse(
                "philosophy",
                PhilosophyStreamEvent(sessionId=outcome.session.public_id, reply=outcome.philosophy.to_payload()).model_dump(),
            )
            for token in split_text(outcome.prepared_reply, 12):
                assistant.append(token)
                yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=token).model_dump())
        else:
            async for token in self.ai.stream(outcome.response_messages):
                assistant.append(token)
                yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=token).model_dump())
        if assistant:
            self.agent_harness.save_assistant_message(user, outcome.session, "".join(assistant))
        try:
            await self.agent_harness.dispatch_tools(outcome.tool_plan)
        except Exception as exc:
            logger.warning(
                "Post-response tool dispatch failed for session=%s report_id=%s: %s",
                outcome.session.public_id,
                outcome.report_id,
                exc,
                exc_info=True,
            )
        yield sse("done", ChatStreamEvent(type="done", sessionId=outcome.session.public_id).model_dump())


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
