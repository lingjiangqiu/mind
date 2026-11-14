from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sessionId: Optional[str] = Field(default=None, max_length=64)


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    type: str


class PhilosophyReplyPayload(BaseModel):
    """Six-part philosophy translation shown to the student as cards."""

    lens: str
    empathy: str
    plainExplanation: str
    perspective: str
    controllable: str
    smallStep: str
    reflectionQuestion: str
    supportNote: str = ""
    source: str
    policyVersion: str


class PhilosophyStreamEvent(BaseModel):
    type: str = "philosophy"
    sessionId: Optional[str] = None
    reply: PhilosophyReplyPayload


class DemoSessionRequest(BaseModel):
    accessCode: str = Field(min_length=1, max_length=32)


class DemoSessionResponse(BaseModel):
    token: str
    tokenType: str = "Bearer"
    expiresIn: int


class ReframeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ReframingCard(BaseModel):
    id: str
    title: str
    lens: str
    content: str
    nextStep: str


class ReframeDecisionTrace(BaseModel):
    policyVersion: str
    privacyStep: str
    hardRiskDecision: str
    routeDecision: str
    outputContract: str


class ReframeResponse(BaseModel):
    riskLevel: str
    safetyOverride: bool
    sanitizedText: str
    cards: list[ReframingCard]
    safetyTitle: Optional[str] = None
    safetyMessage: Optional[str] = None
    safetyActions: list[str] = Field(default_factory=list)
    decisionTrace: ReframeDecisionTrace
    disclaimer: str


class KnowledgeIngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=1_000_000)


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: str
    emotionScore: float
    riskLevel: str
    confidence: float
    summary: str
    createdAt: datetime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: datetime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    messages: list[ConversationMessageResponse]


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: datetime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class RiskCaseResponse(BaseModel):
    id: int
    reportId: int
    riskLevel: str
    status: str
    owner: str
    summary: str
    handoffSummary: str
    acknowledgedBy: Optional[str] = None
    acknowledgedAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime


class CaseNoteResponse(BaseModel):
    id: int
    caseId: int
    actor: str
    note: str
    createdAt: datetime


class ToolJobResponse(BaseModel):
    id: int
    reportId: int
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: datetime
    lastError: str
    createdAt: datetime
    updatedAt: datetime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: datetime


class AgentRunTraceResponse(BaseModel):
    id: int
    sessionId: str
    reportId: Optional[int] = None
    username: str
    intent: str
    riskLevel: str
    originalInput: str
    sanitizedInput: str
    memoryBrief: str
    agentSteps: list[dict[str, Any]]
    retrievedKnowledge: list[dict[str, Any]]
    responseMessages: list[dict[str, Any]]
    assessment: dict[str, Any]
    createdAt: datetime


class ToolAuditResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    toolName: str
    policy: str
    allowed: bool
    status: str
    reason: str
    payload: dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


class AiMessage(BaseModel):
    role: str
    content: str


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}
