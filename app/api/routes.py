from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.runtime import AgentRuntimeService
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import create_demo_token, current_user, enforce_demo_rate_limit, require_admin, valid_demo_access_code
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, DemoSessionRequest, DemoSessionResponse, KnowledgeIngestRequest, KnowledgeIngestResponse, ReframeRequest, ReframeResponse, authority
from app.services.chat import ChatService
from app.services.demo_chat import DEMO_CHAT_STORE, demo_client_ip, stream_public_turn
from app.services.knowledge import KnowledgeService
from app.services.model_assets import finetuned_model_status
from app.services.reframing import PhilosophicalReframingService, is_public_demo_text
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.post("/api/demo/session", response_model=DemoSessionResponse)
def demo_session(payload: DemoSessionRequest, request: Request):
    settings = get_settings()
    enforce_demo_rate_limit(request, settings.demo_rate_limit_attempts, settings.demo_rate_limit_window_seconds)
    if not valid_demo_access_code(payload.accessCode, settings.demo_access_code):
        raise HTTPException(401, "体验码无效")
    return DemoSessionResponse(
        token=create_demo_token("student", settings.demo_token_secret, settings.demo_token_ttl_seconds),
        expiresIn=settings.demo_token_ttl_seconds,
    )


@router.get("/api/profile")
def profile(request: Request, user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
        "isDemo": bool(getattr(request.state, "demo_access", False)),
    }


@router.post("/api/reframe", response_model=ReframeResponse)
def reframe(payload: ReframeRequest, request: Request, _: Annotated[UserAccount, Depends(current_user)]):
    if getattr(request.state, "demo_access", False) and not is_public_demo_text(payload.text):
        raise HTTPException(403, "公开演示只运行页面列出的固定场景，不接收真实个人经历。")
    return PhilosophicalReframingService().reframe(payload.text)


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    http_request: Request,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if getattr(http_request.state, "demo_access", False):
        settings = get_settings()
        state = DEMO_CHAT_STORE.reserve(
            http_request.state.demo_key, http_request.state.demo_expires_at,
            demo_client_ip(http_request), request.message, request.sessionId, settings,
        )
        return StreamingResponse(
            stream_public_turn(db, settings, user, state, request.message),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    service = ChatService(db, get_settings())
    return StreamingResponse(service.stream_chat(user, request), media_type="text/event-stream")


@router.delete("/api/demo/conversation")
def reset_demo_conversation(request: Request, _: Annotated[UserAccount, Depends(current_user)]):
    if not getattr(request.state, "demo_access", False):
        raise HTTPException(403, "仅体验会话可以清除临时对话。")
    return DEMO_CHAT_STORE.reset(request.state.demo_key)


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = (settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai"
             else settings.deepseek_model if provider == "deepseek" else "mock")
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai", "deepseek"},
        "modelConfigured": (bool(settings.deepseek_api_key.strip()) if provider == "deepseek" else
                            bool(settings.openai_api_key.strip()) if provider == "openai" else provider == "ollama"),
        "publicDemo": {"maxTurns": settings.demo_chat_max_turns, "maxChars": settings.demo_chat_max_chars,
                       "ttlSeconds": settings.demo_token_ttl_seconds, "storage": "process-memory",
                       "ipRequestsPerHour": settings.demo_chat_ip_requests, "durableWrites": False},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "MemoryAgent", "status": "READY", "description": "短期上下文与长期记忆摘要"},
            {"name": "SupervisorAgent", "status": "READY", "description": "意图识别与路由"},
            {"name": "KnowledgeAgent", "status": "READY", "description": "RAG 检索与证据补充"},
            {"name": "RiskGuardianAgent", "status": "READY", "description": "心理风险识别与分级"},
            {"name": "PhilosophyAgent", "status": "READY", "description": "低中风险情绪整理、视角选择与输出校验"},
            {"name": "CompanionAgent", "status": "READY", "description": "普通陪伴式回复"},
            {"name": "CounselorAgent", "status": "READY", "description": "咨询式支持回复"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "bounded-agent-loop",
            "maxSteps": AgentRuntimeService.max_steps,
            "scheduler": "langgraph-controller" if framework["active"] == "langgraph" else "custom-runtime",
        },
    }


@router.get("/api/reports/me")
def my_reports(request: Request, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    if getattr(request.state, "demo_access", False):
        raise HTTPException(403, "公开体验令牌不允许读取共享账户报告。")
    return ReportService(db).latest_reports(user.id)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/cases")
def admin_cases(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).risk_cases()


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).case_notes(case_id)


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/agent-traces")
def admin_agent_traces(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).agent_run_traces()


@router.get("/api/admin/tool-audits")
def admin_tool_audits(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_audits()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        return ReportService(db).conversation(session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    settings = get_settings()
    filename = file.filename or "uploaded-file"
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix not in {"pdf", "txt", "md"}:
        raise HTTPException(415, "仅支持 PDF、TXT 或 Markdown 文件。")
    data = await file.read(settings.knowledge_upload_max_bytes + 1)
    if len(data) > settings.knowledge_upload_max_bytes:
        raise HTTPException(413, "上传文件超过大小限制。")
    chunks = KnowledgeService(db, settings).ingest_file(filename, data)
    return KnowledgeIngestResponse(source=filename, chunks=chunks)
