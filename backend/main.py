# -*- coding: utf-8 -*-
"""
ChinaTravel Web 对话应用 - FastAPI 后端
支持对话式旅行规划 + 实时进度跟踪
"""

from __future__ import annotations

# ===== SSL Patch (fix Windows cert store issue) =====
import ssl as _ssl
import certifi
_orig_create_default_context = _ssl.create_default_context
def _patched_create_default_context(*args, **kwargs):
    try:
        return _orig_create_default_context(*args, **kwargs)
    except _ssl.SSLError:
        context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(certifi.where())
        return context
_ssl.create_default_context = _patched_create_default_context

# ===== Path Setup =====
import sys
import os
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Also load .env when the app is started directly with Uvicorn, for example:
# `uvicorn backend.main:app`.
from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

# ===== Imports =====
import json
import uuid
import re
import time
import asyncio
import io
import httpx
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from chinatravel.agent.llms import Deepseek
from chinatravel.agent.load_model import init_agent
from chinatravel.environment.world_env import WorldEnv
from backend.safety import (
    GuardrailClassificationError,
    IntentDecision,
    classify_intent,
    precheck_attack,
    sensitive_reasons,
    update_traveler_groups,
)
from backend.rag import RagConfigurationError, RagService, RagServiceError
from backend.reviews import ReviewConflict, ReviewStore, send_to_gohumanloop
from backend.app_store import (
    AppStore,
    InvalidCredentials,
    StoreConflict,
    StoreNotFound,
)
from backend.memory import (
    ContextBuilder,
    ConversationSummary,
    IncrementalSummaryBatch,
    LongTermMemory,
    MemoryMessage,
    QdrantLongTermMemoryStore,
    complete_incremental_summary,
    estimate_text_tokens,
    plan_incremental_summary,
    stable_memory_id,
)
from chinatravel.optimization import (
    DEFAULT_OPTIMIZATION_GOAL,
    MIN_TOTAL_COST,
    optimization_goal_label,
    resolve_optimization_goal,
)


def _convert_numpy(obj):
    """Recursively convert numpy types to native Python types for JSON serialization."""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert_numpy(v) for v in obj]
    return obj

# ===== FastAPI App =====
app = FastAPI(title="ChinaTravel Chat")

# Mount frontend
frontend_dir = os.path.join(project_root, "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

# ===== In-Memory Session Store =====
sessions: dict[str, dict] = {}
sessions_lock = asyncio.Lock()

# Safety/RAG services. Heavy dependencies and network calls remain lazy.
rag_service = RagService()
review_store = ReviewStore()
app_store = AppStore()
context_builder = ContextBuilder()
long_term_memory_store = QdrantLongTermMemoryStore()

# The authenticated user is kept in request-local state so existing direct
# unit tests can continue calling endpoint functions without fabricating a
# Starlette Request object.
request_user: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "chinatravel_request_user", default=None
)
summary_locks: dict[str, asyncio.Lock] = {}

AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "chinatravel_auth").strip()
AUTH_SESSION_DAYS = max(1, int(os.getenv("AUTH_SESSION_DAYS", "7")))
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
MAX_USER_MESSAGE_CHARS = max(100, int(os.getenv("MAX_USER_MESSAGE_CHARS", "8000")))

# Shared WorldEnv instance (load DB once)
env = WorldEnv(lang="zh")

# Supported cities
SUPPORTED_CITIES = ["北京", "上海", "南京", "苏州", "杭州", "深圳", "成都", "武汉", "广州", "重庆"]
PLANNING_DFS_TIMEOUT_SECONDS = max(
    1, int(os.getenv("PLANNING_DFS_TIMEOUT_SECONDS", "30"))
)

# ===== Pydantic Models =====
class ChatRequest(BaseModel):
    session_id: str
    message: str
    request_id: Optional[str] = None


class AuthRequest(BaseModel):
    username: str
    password: str


class ConversationCreateRequest(BaseModel):
    title: Optional[str] = None


class ReviewDecisionRequest(BaseModel):
    reason: Optional[str] = None


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
    }


def _current_user(*, required: bool = True) -> Optional[dict[str, Any]]:
    user = request_user.get()
    if required and not user:
        raise HTTPException(401, "请先登录")
    return user


def require_admin_token(authorization: Optional[str] = Header(default=None)) -> None:
    user = request_user.get()
    if user and user.get("role") == "admin":
        return
    configured = os.getenv("ADMIN_TOKEN", "change-me-for-demo")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != configured:
        raise HTTPException(401, "管理员 Token 无效")


@app.middleware("http")
async def authenticate_http_request(request: Request, call_next):
    """Authenticate same-origin API calls while leaving static/login pages public."""
    path = request.url.path
    public_api_paths = {
        "/api/auth/login",
        "/api/auth/register",
    }
    user: Optional[dict[str, Any]] = None
    invalid_cookie = False
    raw_token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if raw_token:
        try:
            user = app_store.authenticate_token(raw_token)
        except InvalidCredentials:
            invalid_cookie = True

    authorization = request.headers.get("Authorization", "")
    configured_admin_token = os.getenv("ADMIN_TOKEN", "change-me-for-demo")
    supplied_admin_token = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    legacy_admin = bool(
        supplied_admin_token
        and configured_admin_token
        and supplied_admin_token == configured_admin_token
    )

    if (
        AUTH_REQUIRED
        and path.startswith("/api/")
        and path not in public_api_paths
        and user is None
        and not (path.startswith("/api/admin/") and legacy_admin)
    ):
        response = JSONResponse({"detail": "请先登录"}, status_code=401)
        if invalid_cookie:
            response.delete_cookie(AUTH_COOKIE_NAME, path="/")
        return response

    token = request_user.set(user)
    request.state.user = user
    request.state.legacy_admin = legacy_admin
    try:
        response = await call_next(request)
    finally:
        request_user.reset(token)
    if invalid_cookie:
        response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response

# ===== Progress Tracker =====
# Agent workflow steps mapped from stdout patterns
WORKFLOW_STEPS = [
    ("nl2sl", r"nl2sl|translate|translation", "翻译需求为结构化约束"),
    ("collect_poi", r"collect_poi_info|select.*accommodations|select.*attractions|select.*restaurants", "收集城市POI数据"),
    ("intercity_transport", r"intercity_transport|selected intercity_transports", "选择城际交通"),
    ("thinking", r"thought:|Thought:", "AI分析偏好中"),
    ("hotel", r"ranking_hotel|HotelNameList|selected HotelNameList", "选择酒店"),
    ("rooms", r"room_number|RoomInfo|extracted room", "规划房间"),
    ("budget", r"extracted budget|Budget:", "提取预算限制"),
    ("attractions", r"ranking_attractions|AttractionNameList|selected attractions", "筛选景点"),
    ("restaurants", r"ranking_restaurants|RestaurantNameList|selected restaurants", "筛选餐厅"),
    ("planning", r"DFS_NODE_COUNT:", "DFS搜索生成行程"),
    ("validation", r"valid|constraint|commonsense|check", "验证约束条件"),
]


class ProgressInterceptor(io.StringIO):
    """Intercepts stdout, passes through to original, and extracts progress.
    Deduplicates repeated steps by updating counters in-place rather than
    creating thousands of duplicate entries (e.g. for DFS backtracking)."""
    def __init__(self, original_stdout, progress_list: list):
        super().__init__()
        self.original = original_stdout
        self.progress = progress_list  # list of {step, label, detail, timestamp}
        self.step_index: dict[str, int] = {}  # step_key -> index in progress list
        self.step_counter: dict[str, int] = {}

    def write(self, s):
        if self.original:
            try:
                self.original.write(s)
            except Exception:
                pass

        s_stripped = s.strip()
        s_lower = s_stripped.lower()

        # The planning count is emitted by the search itself.  Previously the
        # UI counted every matching debug line (POI planning/backtrack/etc.),
        # so values such as x379 were log-line counts rather than DFS nodes.
        node_match = re.search(r"DFS_NODE_COUNT:\s*(\d+)", s_stripped)
        if node_match:
            node_count = int(node_match.group(1))
            step_key = "planning"
            detail = f"已搜索 {node_count} 个节点"
            if step_key not in self.step_index:
                self.step_index[step_key] = len(self.progress)
                self.progress.append({
                    "step": step_key,
                    "label": "DFS搜索生成行程",
                    "count": node_count,
                    "detail": detail,
                    "timestamp": datetime.now().isoformat(),
                })
            else:
                idx = self.step_index[step_key]
                self.progress[idx]["count"] = node_count
                self.progress[idx]["detail"] = detail
                self.progress[idx]["timestamp"] = datetime.now().isoformat()
            self.step_counter[step_key] = node_count
            return len(s)

        # Special handling: preserve every Thought instead of updating a single
        # shared progress entry. Numbered output such as "Thought 2:" is also
        # accepted.
        thought_match = re.search(r'thought(?:\s+\d+)?\s*:', s_stripped, re.IGNORECASE)
        if thought_match:
            thought_text = s_stripped[thought_match.start():]
            step_key = "thinking"
            label = "AI分析偏好中"
            thought_number = self.step_counter.get(step_key, 0) + 1
            self.step_counter[step_key] = thought_number
            self.progress.append({
                "step": step_key,
                "label": label,
                "count": thought_number,
                "detail": thought_text,
                "is_thought": True,
                "timestamp": datetime.now().isoformat(),
            })
            return len(s)

        for step_key, pattern, label in WORKFLOW_STEPS:
            if step_key == "thinking":
                continue  # Already handled above
            if re.search(pattern, s_lower):
                # Track counter
                if step_key not in self.step_counter:
                    self.step_counter[step_key] = 0
                    self.step_index[step_key] = len(self.progress)
                    self.progress.append({
                        "step": step_key,
                        "label": label,
                        "count": 1,
                        "detail": s_stripped[:200],
                        "timestamp": datetime.now().isoformat(),
                    })
                else:
                    self.step_counter[step_key] += 1
                    # Update existing entry in-place instead of appending
                    idx = self.step_index[step_key]
                    self.progress[idx]["count"] = self.step_counter[step_key] + 1
                    self.progress[idx]["detail"] = s_stripped[:200]
                    self.progress[idx]["timestamp"] = datetime.now().isoformat()
                break
        return len(s)

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass


# ===== Session Helpers =====
def _base_session(
    sid: str,
    *,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "session_id": sid,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "created_at": datetime.now().isoformat(),
        "messages": [],
        "state": "init",
        "extracted": {"optimization_goal": DEFAULT_OPTIMIZATION_GOAL},
        "plan": None,
        "pending_plan": None,
        "progress": [],
        "last_intent": None,
        "last_rag_category": None,
        "rag_job_id": None,
        "current_request_id": None,
        "traveler_groups": [],
        "sensitive": False,
        "sensitive_reasons": [],
        "review_id": None,
        "review_status": None,
        "review_message": None,
        "rejection_reason": None,
    }


def _checkpoint_session(session: dict[str, Any]) -> None:
    """Persist resumable task state; failures never weaken the safety layer."""
    user_id = session.get("user_id")
    conversation_id = session.get("conversation_id")
    if not user_id or not conversation_id:
        return
    pending_mixed = None
    if session.get("pending_mixed_query"):
        pending_mixed = {
            "query": session.get("pending_mixed_query"),
            "category": session.get("pending_mixed_category"),
        }
    try:
        app_store.save_checkpoint(
            user_id,
            conversation_id,
            runtime_state=session.get("state", "init"),
            extracted=session.get("extracted", {}),
            traveler_groups=session.get("traveler_groups", []),
            pending_mixed=pending_mixed,
            review_id=session.get("review_id"),
            review_status=session.get("review_status"),
            last_rag_category=session.get("last_rag_category"),
            runtime_session_id=session.get("session_id"),
            rag_job_id=session.get("rag_job_id"),
            current_request_id=session.get("current_request_id"),
        )
    except Exception as exc:
        # Runtime output is still kept in memory. The warning is deliberately
        # server-side because internal database details must not reach users.
        print(f"Memory checkpoint warning: {type(exc).__name__}: {exc}")


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    """Persist public rendering data, never raw agent/tool traces or hidden plans."""
    metadata: dict[str, Any] = {}
    if message.get("type") == "rag":
        metadata = {
            "sources": message.get("sources", []),
            "trace_id": message.get("trace_id"),
            "rag_meta": message.get("rag_meta", {}),
        }
    elif message.get("type") == "confirmation":
        metadata = {"current_requirements": message.get("current_requirements", {})}
    return metadata


def _default_context_eligible(message_type: str) -> bool:
    return message_type not in {
        "status",
        "guardrail",
        "error",
        "rag",
        "plan",
        "intent_choice",
        "review_pending",
        "review_approved",
        "review_rejected",
    }


def _persist_runtime_message(
    session: dict[str, Any],
    message: dict[str, Any],
    *,
    request_id: Optional[str] = None,
    context_eligible: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    if message.get("_persisted"):
        return None
    user_id = session.get("user_id")
    conversation_id = session.get("conversation_id")
    if not user_id or not conversation_id:
        return None
    message_type = str(message.get("type") or "chat")
    persisted = app_store.append_message(
        user_id,
        conversation_id,
        role=str(message.get("role") or "assistant"),
        message_type=message_type,
        content=str(message.get("content") or ""),
        request_id=request_id or session.get("current_request_id") or uuid.uuid4().hex,
        intent=message.get("intent"),
        metadata=_message_metadata(message),
        context_eligible=(
            _default_context_eligible(message_type)
            if context_eligible is None
            else context_eligible
        ),
        token_estimate=estimate_text_tokens(str(message.get("content") or "")),
    )
    message["message_id"] = persisted["message_id"]
    message["seq"] = persisted["seq"]
    message["_persisted"] = True

    # Give a new conversation a useful deterministic title without another
    # LLM call. Users may still archive it from the history sidebar.
    if message.get("role") == "user" and persisted["seq"] == 1:
        title = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()
        if title:
            app_store.update_conversation_title(user_id, conversation_id, title[:30])
    return persisted


def _schedule_summary(session: dict[str, Any]) -> None:
    if not session.get("user_id") or not session.get("conversation_id"):
        return
    try:
        asyncio.get_running_loop().create_task(
            _maybe_summarize_conversation(
                str(session["user_id"]), str(session["conversation_id"])
            )
        )
    except RuntimeError:
        pass


def _append_assistant_message(
    session: dict[str, Any],
    content: str,
    message_type: str,
    *,
    request_id: Optional[str] = None,
    context_eligible: Optional[bool] = None,
    schedule_summary: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    message = {
        "role": "assistant",
        "content": content,
        "type": message_type,
        "timestamp": datetime.now().isoformat(),
        **extra,
    }
    session["messages"].append(message)
    _persist_runtime_message(
        session,
        message,
        request_id=request_id,
        context_eligible=context_eligible,
    )
    if schedule_summary and _default_context_eligible(message_type):
        _schedule_summary(session)
    return message


def _memory_messages(rows: list[dict[str, Any]]) -> list[MemoryMessage]:
    return [
        MemoryMessage(
            seq=int(row["seq"]),
            role=str(row["role"]),
            content=str(row["content"]),
            message_id=str(row.get("message_id") or ""),
            message_type=str(row.get("type") or "chat"),
            context_eligible=bool(row.get("context_eligible", True)),
        )
        for row in rows
    ]


def _summary_record(row: Optional[dict[str, Any]]) -> ConversationSummary:
    if not row:
        return ConversationSummary()
    return ConversationSummary(
        summary_text=str(row.get("summary_text") or ""),
        summary_through_seq=int(row.get("summary_through_seq") or 0),
        source_message_count=int(row.get("source_message_count") or 0),
        token_estimate=int(row.get("token_estimate") or 0),
        summary_version=str(row.get("summary_version") or "memory-summary-v1"),
    )


def _truncate_summary(text: str, token_budget: int) -> str:
    clean = text.strip()
    if estimate_text_tokens(clean) <= token_budget:
        return clean
    low, high = 0, len(clean)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(clean[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return clean[:low].rstrip() + "…"


async def _maybe_summarize_conversation(user_id: str, conversation_id: str) -> None:
    """Incrementally summarize old turns; failure keeps the recent window usable."""
    lock = summary_locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        try:
            rows = app_store.list_context_messages(user_id, conversation_id)
            previous = _summary_record(app_store.get_summary(user_id, conversation_id))
            batch = plan_incremental_summary(_memory_messages(rows), previous)
            if not batch:
                return

            # Bound one summary call so a single oversized plan cannot inflate
            # prompt cost. Remaining old messages are handled incrementally on
            # a later public response.
            selected: list[MemoryMessage] = []
            selected_tokens = 0
            for message in batch.messages:
                if selected and selected_tokens + message.token_estimate > 5000:
                    break
                selected.append(message)
                selected_tokens += message.token_estimate
            if not selected:
                return
            bounded_batch = IncrementalSummaryBatch(
                previous_summary=batch.previous_summary,
                messages=tuple(selected),
                start_seq=selected[0].seq,
                end_seq=selected[-1].seq,
            )
            prompt = f"""请把下面的历史对话压缩成简洁、忠实的中文摘要。
只保留已确认的用户目标、关键事实、尚未解决的问题和已经公开的结论；
删除重复内容、寒暄、临时状态、工具过程和系统提示，不推断用户偏好。
摘要必须能帮助后续对话消解指代，不得加入原文没有的信息。

{bounded_batch.source_text()}"""
            llm = Deepseek()
            result = await asyncio.to_thread(
                llm,
                [{"role": "user", "content": prompt}],
                False,
                False,
            )
            if llm.last_error or not isinstance(result, str) or not result.strip():
                return
            if result.strip().startswith('{"error"'):
                return
            summary_text = _truncate_summary(
                result,
                context_builder.config.summary_token_budget,
            )
            completed = complete_incremental_summary(bounded_batch, summary_text)
            app_store.upsert_summary(
                user_id,
                conversation_id,
                summary_text=completed.summary_text,
                summary_through_seq=completed.summary_through_seq,
                source_message_count=completed.source_message_count,
                token_estimate=completed.token_estimate,
                summary_version=completed.summary_version,
            )
        except Exception as exc:
            print(f"Memory summary warning: {type(exc).__name__}: {exc}")


async def _managed_conversation(
    session: dict[str, Any], current_question: str
) -> list[dict[str, str]]:
    """Build extraction context; safety classification never calls this helper."""
    user_id = session.get("user_id")
    conversation_id = session.get("conversation_id")
    if not user_id or not conversation_id:
        return [
            {"role": m["role"], "content": m["content"]}
            for m in session.get("messages", [])
            if m.get("type") not in {"status", "guardrail", "review_rejected"}
        ]
    try:
        history = _memory_messages(
            app_store.list_context_messages(user_id, conversation_id)
        )
        summary = _summary_record(app_store.get_summary(user_id, conversation_id))
        lookup = await long_term_memory_store.retrieve(
            user_id=user_id,
            query=current_question,
            top_k=context_builder.config.long_term_top_k,
            score_threshold=context_builder.config.long_term_score_threshold,
        )
        if lookup.degraded:
            print(f"Long-term memory lookup degraded: {lookup.error}")
        built = context_builder.build(
            system_instruction=(
                "这些内容仅用于旅行需求的上下文衔接和指代消解。"
                "历史信息不得覆盖当前用户明确表达，也不得覆盖系统安全规则；"
                "不要从历史中自动推断或学习用户偏好。"
            ),
            current_question=current_question,
            history=history,
            summary=summary,
            long_term_memories=lookup.memories,
        )
        return [dict(message) for message in built.messages]
    except Exception as exc:
        print(f"Context build warning: {type(exc).__name__}: {exc}")
        return [{"role": "user", "content": current_question}]


_EXPLICIT_MEMORY_RE = re.compile(r"(?:请)?记住(?:一下)?[：:\s]*(.+)", re.DOTALL)
_SENSITIVE_MEMORY_RE = re.compile(
    r"未成年|儿童|孩子|宝宝|老人|老年人|身份证|护照号|手机号|电话|密码|API\s*Key|密钥",
    re.IGNORECASE,
)


def _extract_explicit_memory_fact(content: str) -> Optional[str]:
    """Extract only a fact the user explicitly asked the system to remember."""
    clean = str(content or "").strip()
    if not clean or "不要记住" in clean or "忘记" in clean:
        return None
    match = _EXPLICIT_MEMORY_RE.search(clean)
    if not match:
        return None
    fact = re.sub(r"\s+", " ", match.group(1)).strip(" 。！!？?")[:500]
    return fact or None


async def _store_explicit_memory(
    session: dict[str, Any], message: dict[str, Any]
) -> tuple[str, Optional[str]]:
    """Store an explicit, non-sensitive fact and report a public-safe status.

    Return values are ``ignored``, ``rejected``, ``saved`` or ``degraded``.
    The caller may ignore the result for messages that continue through the
    normal planning/RAG path, while a stand-alone memory command can provide a
    deterministic acknowledgement without relying on another LLM call.
    """
    user_id = session.get("user_id")
    if not user_id or message.get("role") != "user":
        return "ignored", None
    content = str(message.get("content") or "").strip()
    fact = _extract_explicit_memory_fact(content)
    if not fact:
        return "ignored", None
    # The memory-only phase intentionally avoids sensitive traveller identity,
    # credentials and contact data. Those remain request-scoped and are never
    # embedded into the long-term collection.
    if _SENSITIVE_MEMORY_RE.search(fact):
        return "rejected", fact
    memory = LongTermMemory(
        memory_id=stable_memory_id(user_id, "explicit_memory", "user_note", fact),
        user_id=user_id,
        normalized_text=f"用户明确要求记住：{fact}",
        memory_type="explicit_memory",
        canonical_key="user_note",
        canonical_value=fact,
        confidence=1.0,
        source_message_ids=tuple(
            [str(message.get("message_id"))] if message.get("message_id") else []
        ),
    )
    result = await long_term_memory_store.upsert(memory)
    if result.degraded:
        print(f"Long-term memory write degraded: {result.error}")
        return "degraded", fact
    return "saved", fact


def _looks_like_contextual_followup(text: str) -> bool:
    clean = text.strip()
    return len(clean) <= 40 and bool(
        re.search(r"^(那|那么|这个|这种|它|还|另外|然后)|需要什么|怎么办|可以吗|呢[？?]?$", clean)
    )


def _previous_rag_question(session: dict[str, Any], current_question: str) -> str:
    user_id = session.get("user_id")
    conversation_id = session.get("conversation_id")
    if not user_id or not conversation_id:
        return ""
    try:
        rows = app_store.list_messages(
            user_id, conversation_id, limit=50
        )["items"]
        skipped_current = False
        for row in reversed(rows):
            if row.get("role") != "user":
                continue
            content = str(row.get("content") or "")
            if not skipped_current and content.strip() == current_question.strip():
                skipped_current = True
                continue
            if row.get("intent") == "rag_query":
                return content
    except Exception:
        pass
    return ""


def _replay_persisted_request(
    session: dict[str, Any], request_id: str
) -> Optional[dict[str, Any]]:
    """Return the saved public response for an HTTP retry without rerunning LLMs."""
    user_id = session.get("user_id")
    conversation_id = session.get("conversation_id")
    if not user_id or not conversation_id or not request_id:
        return None
    try:
        rows = app_store.list_messages(user_id, conversation_id, limit=200)["items"]
    except Exception:
        return None
    matching = [row for row in rows if row.get("request_id") == request_id]
    if not any(row.get("role") == "user" for row in matching):
        return None
    assistant = next(
        (row for row in reversed(matching) if row.get("role") == "assistant"),
        None,
    )
    if not assistant:
        if session.get("state") == "rag_querying" and session.get("rag_job_id"):
            return {
                "type": "rag_status",
                "status": "running",
                "job_id": session["rag_job_id"],
                "progress": 0,
                "message": "Agentic RAG 正在查询知识库",
            }
        if session.get("state") == "generating":
            return {
                "type": "status",
                "status": "generating",
                "message": "正在生成旅行计划，请稍候...",
                "sensitive": session.get("sensitive", False),
            }
        return None

    message_type = str(assistant.get("type") or "message")
    metadata = assistant.get("metadata") or {}
    response: dict[str, Any] = {
        "type": message_type,
        "message": assistant.get("content", ""),
        "replayed": True,
    }
    if message_type == "rag":
        response.update({
            "sources": metadata.get("sources", []),
            "trace_id": metadata.get("trace_id"),
            "rag_meta": metadata.get("rag_meta", {}),
            "trace": [],
            "reset": True,
        })
    elif message_type in {"guardrail", "error", "plan", "review_rejected"}:
        response["reset"] = True
    elif message_type == "confirmation":
        response["current_requirements"] = metadata.get("current_requirements", {})
    return response


async def create_session(
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> dict:
    checkpoint: Optional[dict[str, Any]] = None
    if user_id and conversation_id:
        app_store.get_conversation(user_id, conversation_id)
        checkpoint = app_store.get_checkpoint(user_id, conversation_id)

        # Reuse the already active runtime for this durable conversation.
        async with sessions_lock:
            for existing in sessions.values():
                if (
                    existing.get("user_id") == user_id
                    and existing.get("conversation_id") == conversation_id
                ):
                    return existing

    preferred_sid = str((checkpoint or {}).get("runtime_session_id") or "")
    sid = preferred_sid if preferred_sid and preferred_sid not in sessions else uuid.uuid4().hex[:12]
    session = _base_session(sid, user_id=user_id, conversation_id=conversation_id)

    if checkpoint:
        checkpoint_state = str(checkpoint.get("runtime_state") or "init")
        if checkpoint_state in {
            "clarifying", "confirmed", "awaiting_intent_choice",
            "pending_review", "review_error", "rag_querying",
        }:
            session["state"] = checkpoint_state
            session["extracted"] = checkpoint.get("extracted") or session["extracted"]
            session["traveler_groups"] = checkpoint.get("traveler_groups") or []
            session["sensitive"] = bool(session["traveler_groups"])
            session["sensitive_reasons"] = sensitive_reasons(session["traveler_groups"])
            pending_mixed = checkpoint.get("pending_mixed") or {}
            session["pending_mixed_query"] = pending_mixed.get("query")
            session["pending_mixed_category"] = pending_mixed.get("category")
            session["review_id"] = checkpoint.get("review_id")
            session["review_status"] = checkpoint.get("review_status")
            session["last_rag_category"] = checkpoint.get("last_rag_category")
            session["rag_job_id"] = checkpoint.get("rag_job_id")
            session["current_request_id"] = checkpoint.get("current_request_id")
            if checkpoint_state in {"pending_review", "review_error"} and session["review_id"]:
                try:
                    review = review_store.get(session["review_id"])
                    session["pending_plan"] = review["plan_snapshot"]
                    session["review_message"] = (
                        "旅行方案已生成，正在等待人工审核"
                        if checkpoint_state == "pending_review"
                        else "飞书审核服务异常，方案仍保持隐藏，可由本地管理员处理"
                    )
                except KeyError:
                    session["state"] = "init"
                    session["review_id"] = None
                    session["review_status"] = None
        else:
            session["last_rag_category"] = checkpoint.get("last_rag_category")

        if checkpoint_state == "generating":
            # Background work cannot be resumed safely after process restart.
            # Record one public interruption and return to a clean input state.
            interruption = {
                "role": "assistant",
                "content": "上次任务因服务重启而中断，请重新发送需求。",
                "type": "error",
                "timestamp": datetime.now().isoformat(),
            }
            session["messages"].append(interruption)
            _persist_runtime_message(
                session,
                interruption,
                request_id=f"recovery:{checkpoint.get('version', 1)}",
                context_eligible=False,
            )
            session["state"] = "init"
            session["extracted"] = {"optimization_goal": DEFAULT_OPTIMIZATION_GOAL}
            session["rag_job_id"] = None
            session["current_request_id"] = None

    async with sessions_lock:
        sessions[sid] = session
    _checkpoint_session(session)
    return session


async def get_session(sid: str) -> Optional[dict]:
    async with sessions_lock:
        existing = sessions.get(sid)
    if existing:
        return existing
    user = request_user.get()
    if not user:
        return None
    try:
        binding = app_store.find_checkpoint_by_runtime_session_id(sid)
        if not binding or binding.get("user_id") != user.get("user_id"):
            return None
        return await create_session(binding["user_id"], binding["conversation_id"])
    except (StoreNotFound, KeyError):
        return None


def reset_session_for_next_input(session: dict, clear_messages: bool = True) -> None:
    """Clear request-scoped state while keeping the same browser session id."""
    if clear_messages:
        session["messages"] = []
    session.update({
        "state": "init",
        "extracted": {"optimization_goal": DEFAULT_OPTIMIZATION_GOAL},
        "plan": None,
        "pending_plan": None,
        "progress": [],
        "last_intent": None,
        "rag_job_id": None,
        "current_request_id": None,
        "traveler_groups": [],
        "sensitive": False,
        "sensitive_reasons": [],
        "review_id": None,
        "review_status": None,
        "review_message": None,
        "rejection_reason": None,
    })
    session.pop("pending_mixed_query", None)
    session.pop("pending_mixed_category", None)
    _checkpoint_session(session)


def _public_session_progress(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Hide raw model thoughts while a sensitive plan is not yet approved."""
    progress = list(session.get("progress", []))
    if not (
        session.get("sensitive")
        and session.get("state") in {"generating", "pending_review", "review_error"}
    ):
        return progress
    return [
        {
            key: value
            for key, value in item.items()
            if key in {"step", "label", "count", "timestamp"}
        }
        for item in progress
        if not item.get("is_thought")
    ]

# ===== Requirement Extraction =====
EXTRACTION_PROMPT = """你是一个旅行需求提取助手。根据用户的对话历史，提取旅行规划所需的结构化信息。

只返回 JSON，不要其他内容。

必填字段：
- target_city: 目的地城市
- start_city: 出发城市
- days: 旅行天数
- people_number: 人数（默认1）

可选字段：
- budget: 总预算（元）
- preferences: 偏好的景点类型、美食类型等
- constraints: 限制条件（如不要辣、指定酒店或必须游览某景点等）。不要提取地铁、打车、步行等市内交通偏好
- optimization_goal: 只能是 budget_fit 或 min_total_cost。用户要求最便宜、最低价、最省钱时为 min_total_cost；否则为 budget_fit

支持的城市：{supported_cities}

对话历史：
{conversation}

当前已提取的信息：
{current}

请分析对话，提取新信息并与已有信息合并。
如果某个必填字段仍然缺失，在 missing_required 中列出。
如果信息不全，在 clarification_question 中写一句友好的追问。

返回格式：
{{
    "target_city": "北京",
    "start_city": "南京",
    "days": 3,
    "people_number": 1,
    "budget": 2000,
    "preferences": "喜欢历史文化景点",
    "constraints": "不要辣",
    "optimization_goal": "budget_fit",
    "missing_required": ["start_city"],
    "clarification_question": "请问您从哪个城市出发呢？"
}}"""


async def extract_requirements(
    llm: Deepseek,
    conversation: list[dict],
    current_extracted: dict,
    latest_message: str = "",
) -> dict:
    prompt = EXTRACTION_PROMPT.format(
        supported_cities=", ".join(SUPPORTED_CITIES),
        conversation=json.dumps(conversation, ensure_ascii=False),
        current=json.dumps(current_extracted, ensure_ascii=False),
    )
    try:
        response = llm([{"role": "user", "content": prompt}], one_line=False, json_mode=True)
        result = json.loads(response)
    except Exception as e:
        print(f"Extraction error: {e}")
        result = {"missing_required": [], "clarification_question": ""}

    merged = {**current_extracted}
    for key in ["target_city", "start_city", "days", "people_number", "budget", "preferences", "constraints"]:
        val = result.get(key)
        if val is not None and val != "":
            merged[key] = val

    merged["optimization_goal"] = resolve_optimization_goal(
        latest_message,
        current_extracted.get("optimization_goal", DEFAULT_OPTIMIZATION_GOAL),
        result.get("optimization_goal"),
    )

    result["merged"] = merged
    return result


def refresh_sensitive_state(session: dict, latest_message: str) -> None:
    """Keep special-traveller state controllable by the user's latest correction."""
    groups = update_traveler_groups(latest_message, session.get("traveler_groups", []))
    session["traveler_groups"] = groups
    session["sensitive"] = bool(groups)
    session["sensitive_reasons"] = sensitive_reasons(groups)
    session["extracted"]["traveler_groups"] = groups


# ===== Plan Generation =====
def _build_nl_from_extracted(req: dict) -> str:
    parts = []
    if req.get("start_city"):
        parts.append(f"当前位置{req['start_city']}")
    if req.get("target_city"):
        parts.append(f"我想去{req['target_city']}")
    if req.get("days"):
        parts.append(f"玩{req['days']}天")
    if req.get("people_number", 1) > 1:
        parts.append(f"{req['people_number']}个人")
    if req.get("budget"):
        parts.append(f"预算{req['budget']}元")
    if req.get("constraints"):
        parts.append(req["constraints"])
    if req.get("preferences"):
        parts.append(req["preferences"])
    if req.get("optimization_goal") == MIN_TOTAL_COST:
        parts.append("在满足全部限制条件和预算上限的前提下，优先选择总花费最低的组合")
    parts.append("请给我一个旅行规划。")
    return "，".join(parts)


async def generate_plan_background(session: dict):
    """Run agent planning in background thread with progress tracking."""
    # Freeze the confirmed request and persistence identity for this task.
    # Later browser input cannot mutate a plan already being generated.
    req = dict(session["extracted"])
    frozen_traveler_groups = list(
        req.get("traveler_groups") or session.get("traveler_groups") or []
    )
    frozen_sensitive = bool(frozen_traveler_groups)
    frozen_sensitive_reasons = sensitive_reasons(frozen_traveler_groups)
    planning_request_id = str(
        session.get("current_request_id") or f"planning:{uuid.uuid4().hex}"
    )
    # Agent initialization is part of the background task and may fail because
    # of local native dependencies. Keep a sentinel so failures are reported
    # back to the session instead of leaving it stuck in `generating`.
    agent = None

    nl_text = _build_nl_from_extracted(req)
    # Unique cache key per request content (not per session)
    import hashlib
    cache_uid = hashlib.md5(nl_text.encode()).hexdigest()[:12]

    query = {
        "uid": cache_uid,
        "nature_language": nl_text,
        "days": req.get("days", 2),
        "target_city": req.get("target_city", "北京"),
        "start_city": req.get("start_city", "深圳"),
        "people_number": req.get("people_number", 1),
        # The web layer already has the structured budget. Passing it through
        # lets the cheapest branch avoid asking the LLM to extract it again.
        "budget": req.get("budget"),
    }

    cheapest_branch = req.get("optimization_goal") == MIN_TOTAL_COST

    agent_kwargs = {
        "method": "LLMNeSy",
        "env": env,
        # 这是 Agent 使用的大语言模型对象 这里是ds模型的实例
        "backbone_llm": None,
        "cache_dir": os.path.join(project_root, "cache"),
        "log_dir": os.path.join(project_root, "cache", "web", session["session_id"]),
        "debug": True,  # Enable debug so Logger forwards to our interceptor; spam suppressed by _bt_log
        "time_cut": PLANNING_DFS_TIMEOUT_SECONDS,
        "max_plans": 3,
        # budget_fit needs a wider pool because later, more expensive plans may
        # be closer to the budget. min_total_cost searches cheap-first, so the
        # first three distinct valid plans already satisfy the output objective.
        # 最便宜的分支只需要3个候选计划，其他分支需要9个候选计划
        "max_candidates": 3 if cheapest_branch else 9,
        # Bound POI branching so a complete 3-day path is reached before the
        # DFS spends its entire budget enumerating alternatives for Day 1/2.
        "search_width": 4,
        # Preselect a diverse preference/price pool before expensive route and
        # constraint evaluation; search_width is applied after this stage.
        "poi_candidate_width": 24,
        # The default keeps the original budget-proximity branch unchanged.
        # min_total_cost activates the isolated low-cost candidate branch in
        # NesyAgent.
        "optimization_goal": req.get("optimization_goal", DEFAULT_OPTIMIZATION_GOAL),
        # Local transport mode is not a product feature. Keep one hidden,
        # route-based duration estimate per transition and never branch on it.
        "enable_innercity_transport": False,
        # With no POI preference or extra constraint, LLM recommendations are
        # immediately overwritten by price ordering in the cheapest branch.
        "cost_only_search": cheapest_branch and not req.get("preferences") and not req.get("constraints"),
    }
    # Set up progress before initialization so even import/DLL failures become
    # visible to the polling frontend.
    progress = []
    async with sessions_lock:
        session["progress"] = progress
    progress.append({
        "step": "start",
        "label": "开始生成旅行计划",
        "detail": f"目的地: {query['target_city']}, {query['days']}天, {query.get('people_number', 1)}人",
        "timestamp": datetime.now().isoformat(),
    })

    # Redirect agent stdout to capture progress. Use sys.__stdout__ as pass-through
    # so terminal shows execution steps. Backtrack spam is suppressed by _bt_log.
    old_stdout = sys.stdout
    stdout_redirected = False

    try:
        llm = Deepseek()
        agent_kwargs["backbone_llm"] = llm
        agent = init_agent(agent_kwargs)

        interceptor = ProgressInterceptor(sys.__stdout__, progress)
        sys.stdout = interceptor
        stdout_redirected = True

        loop = asyncio.get_running_loop()
        succ, plan = await loop.run_in_executor(
            None, agent.run, query, True
        )  # load_cache=True, key is content-based
        plan = _convert_numpy(plan) if isinstance(plan, dict) else plan
    except Exception as exc:
        succ = False
        stage = "agent_initialization" if agent is None else "agent_execution"
        plan = {
            "error": f"{type(exc).__name__}: {exc}",
            "error_info": stage,
        }
        print(
            f"Travel planning background task failed during {stage}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.__stderr__,
        )
    finally:
        if stdout_redirected:
            sys.stdout = old_stdout

    review_to_submit = None
    async with sessions_lock:
        if succ:
            progress.append({
                "step": "done",
                "label": "规划完成",
                "detail": f"搜索节点: {plan.get('search_nodes', '?')}, 回溯: {plan.get('backtrack_count', '?')}",
                "timestamp": datetime.now().isoformat(),
            })
            # Use one stable response layout for every successful result.
            # Multi-plan search normally supplies three entries; a legitimate
            # smaller result is still labelled consistently as 方案A.
            if plan.get("multi"):
                display_plan = plan
            else:
                display_plan = {"plans": [plan], "count": 1, "multi": True}
            summary = _format_multi_plan(display_plan, req)
            if frozen_sensitive:
                review = review_store.create(
                    session["session_id"],
                    dict(req),
                    plan,
                    frozen_sensitive_reasons,
                )
                session["pending_plan"] = plan
                session["plan"] = None
                session["review_id"] = review["review_id"]
                session["review_status"] = "pending"
                session["review_message"] = "旅行方案已生成，正在等待人工审核"
                session["traveler_groups"] = frozen_traveler_groups
                session["sensitive"] = True
                session["sensitive_reasons"] = frozen_sensitive_reasons
                session["state"] = "pending_review"
                _append_assistant_message(
                    session,
                    "旅行方案已生成。由于包含未成年人、儿童或老人，完整方案需要人工审核通过后才能发布。",
                    "review_pending",
                    request_id=planning_request_id,
                    context_eligible=False,
                    schedule_summary=False,
                )
                review_to_submit = review
            else:
                session["plan"] = plan
                session["pending_plan"] = None
                session["state"] = "done"
                _append_assistant_message(
                    session,
                    summary,
                    "plan",
                    request_id=planning_request_id,
                    context_eligible=False,
                    plan=plan,
                )
        else:
            progress.append({
                "step": "error",
                "label": "规划失败",
                "detail": str(plan.get("error", ""))[:200],
                "timestamp": datetime.now().isoformat(),
            })
            session["state"] = "clarifying"

            # Build diagnostic failure message
            stats = getattr(agent, 'failure_stats', {}) or {}
            min_cost = getattr(agent, 'min_intercity_hotel_cost', float('inf'))
            if min_cost == float('inf'):
                min_cost = None

            days = req.get("days", 1)
            people = req.get("people_number", 1)
            budget = session["extracted"].get("budget")
            error_info = plan.get("error_info", "")

            if error_info == "agent_initialization":
                raw_error = str(plan.get("error") or "未知初始化错误")
                reason = (
                    "旅行规划服务初始化失败。\n\n"
                    f"错误：{raw_error}\n\n"
                    "本次任务已安全结束，可以修复依赖后重新点击确认。"
                )
            elif error_info == "agent_execution" and plan.get("error"):
                reason = (
                    "旅行规划执行失败。\n\n"
                    f"错误：{plan.get('error')}\n\n"
                    "请检查后端日志后重试。"
                )
            elif error_info == "TimeOutError" or stats.get("dfs_timeout"):
                search_nodes = plan.get(
                    "search_nodes", getattr(agent, "search_nodes", "?")
                )
                backtracks = plan.get(
                    "backtrack_count", getattr(agent, "backtrack_count", "?")
                )
                reason = (
                    f"⏱ 搜索超时（{PLANNING_DFS_TIMEOUT_SECONDS}秒内未找到完整方案）。\n\n"
                    f"已搜索 {search_nodes} 个 DFS 节点，发生 {backtracks} 次回溯。\n"
                    f"建议：减少天数、增加预算，或换一个城市试试。"
                )
            elif budget is not None and stats.get("budget_blocked", 0) > 0:
                detail = getattr(agent, 'min_cost_detail', {}) or {}
                need_min = (min_cost or 0) + 100 * people * (days - 1)

                # Build detailed cost breakdown
                lines = ["💰 预算不足以覆盖基本开销。\n"]
                lines.append(f"你的预算：¥{budget} | {days}天 | {people}人\n")

                if detail:
                    # Go transport line
                    go_line = (
                        f"  ├─ 去程 {detail.get('go_type','')} {detail.get('go_id','')} "
                        f"{detail.get('go_from','')}→{detail.get('go_to','')} "
                        f"({detail.get('go_time','')}) "
                        f"¥{detail.get('go_cost',0):.0f}"
                    )
                    lines.append(go_line)

                    # Back transport line
                    back_line = (
                        f"  ├─ 回程 {detail.get('back_type','')} {detail.get('back_id','')} "
                        f"{detail.get('back_from','')}→{detail.get('back_to','')} "
                        f"({detail.get('back_time','')}) "
                        f"¥{detail.get('back_cost',0):.0f}"
                    )
                    lines.append(back_line)

                    # Hotel line
                    hotel_name = detail.get('hotel_name', '')
                    hotel_price = detail.get('hotel_price', 0)
                    hotel_rooms = detail.get('hotel_rooms', 0)
                    hotel_nights = detail.get('hotel_nights', 0)
                    hotel_total = detail.get('hotel_total', 0)
                    if hotel_nights > 0:
                        hotel_line = (
                            f"  ├─ 住宿 {hotel_name} "
                            f"¥{hotel_price:.0f}/晚 × {hotel_rooms}间 × {hotel_nights}晚 "
                            f"= ¥{hotel_total:.0f}"
                        )
                    else:
                        hotel_line = f"  ├─ 住宿 无（当日往返）"
                    lines.append(hotel_line)

                    # Subtotal
                    total = detail.get('total', min_cost or 0)
                    lines.append(f"  └─ 交通+住宿小计：¥{total:.0f}")

                lines.append(f"\n还需至少 ¥{100 * people * (days - 1)} 覆盖餐饮和门票")
                lines.append(f"最低预算需 ¥{int(need_min)} 以上")
                lines.append(f"\n建议：增加预算至 ¥{int(need_min) + 200} 以上。")

                reason = "\n".join(lines)
            elif stats.get("back_earlier_than_go", 0) > 0 and stats.get("dfs_no_solution", 0) > 0:
                reason = (
                    f"🚄 交通时间冲突。\n\n"
                    f"  回程早于去程 {stats['back_earlier_than_go']} 次——往返交通时间无法衔接。\n"
                    f"  尝试了 {stats.get('dfs_no_solution', 0)} 个交通组合，无一可行。\n\n"
                    f"建议：选择更早出发或更晚返程的日期。"
                )
            elif stats.get("room_type_mismatch", 0) > 0:
                reason = (
                    f"🏨 房间类型不匹配。\n\n"
                    f"  你要求的房型在酒店中找不到匹配 ({stats['room_type_mismatch']} 次)。\n\n"
                    f"建议：放宽房间类型要求，或不指定床型。"
                )
            elif stats.get("room_number_mismatch", 0) > 0:
                reason = (
                    f"🏨 房间数量不足。\n\n"
                    f"  你要求的房间数无法满足 ({stats['room_number_mismatch']} 次)。\n\n"
                    f"建议：减少每间房的人数限制。"
                )
            else:
                detail = getattr(agent, 'min_cost_detail', {}) or {}
                lines = ["搜索未找到可行方案。\n"]
                lines.append(f"共搜索 {plan.get('search_nodes', '?')} 个节点，回溯 {plan.get('backtrack_count', '?')} 次\n")

                if detail and detail.get('total'):
                    go_line = f"  最便宜去程: {detail.get('go_id','?')} ¥{detail.get('go_cost',0):.0f}"
                    back_line = f"  最便宜回程: {detail.get('back_id','?')} ¥{detail.get('back_cost',0):.0f}"
                    hotel_line = f"  最便宜住宿: {detail.get('hotel_name','?')} ¥{detail.get('hotel_total',0):.0f}"
                    lines.append(go_line)
                    lines.append(back_line)
                    lines.append(hotel_line)
                    lines.append(f"  交通+住宿合计: ¥{detail.get('total',0):.0f}")

                lines.append(f"\n建议：调整出发城市、增加天数，或减少限制条件。")
                reason = "\n".join(lines)

            _append_assistant_message(
                session,
                reason,
                "error",
                request_id=planning_request_id,
                context_eligible=False,
                schedule_summary=False,
            )

        _checkpoint_session(session)

    if review_to_submit:
        asyncio.create_task(send_to_gohumanloop(review_to_submit, review_store, apply_review_decision, mark_review_error))


def _format_multi_plan(plan: dict, req: dict) -> str:
    """Format plans already ranked by the selected optimization branch."""
    plans = plan.get("plans", [])
    budget = req.get("budget")

    goal = req.get("optimization_goal", DEFAULT_OPTIMIZATION_GOAL)
    lines = [
        f"为你找到 {len(plans)} 套旅行方案！",
        f"优化目标：{optimization_goal_label(goal)}",
        "",
    ]

    for idx, p in enumerate(plans):
        total = p.get("_total_cost")
        if total is None:
            total = 0
            for day in p.get("itinerary", []):
                for act in day.get("activities", []):
                    total += act.get("cost", 0) or act.get("price", 0) or 0
                    total += sum(
                        transport.get("cost", 0) or 0
                        for transport in act.get("transports", []) or []
                    )
        budget_str = f"  💰 总花费: ¥{total:.0f}"
        if budget:
            pct = total / budget * 100
            diff = budget - total
            if diff >= 0:
                budget_str += f" | 预算内剩余 ¥{diff:.0f} ({pct:.0f}%)"
            else:
                budget_str += f" | ⚠️ 超预算 ¥{-diff:.0f}"

        labels = ["A", "B", "C"]
        label = labels[idx] if idx < len(labels) else str(idx + 1)
        lines.append(f"方案{label}")
        lines.append(_format_single_plan(p, req))
        lines.append(budget_str)

        # Stats
        stats = p.get("search_time_sec", 0)
        nodes = p.get("search_nodes", 0)
        lines.append(f"  搜索统计: {stats:.1f}秒 | 节点: {nodes}")
        lines.append("")

    return "\n".join(lines)


def _format_single_plan(plan: dict, req: dict) -> str:
    """Format a single plan's itinerary."""
    lines = []
    itinerary = plan.get("itinerary", [])
    for day_data in itinerary:
        raw_day = day_data.get("day", 0)
        day_num = raw_day + 1 if raw_day == 0 else raw_day
        lines.append(f"--- Day {day_num} ---")
        for act in day_data.get("activities", []):
            act_type = act.get("type", "")
            start = act.get("start_time", "")
            end = act.get("end_time", "")
            cost = act.get("cost", 0) or act.get("price", 0) or 0
            if act_type == "train":
                lines.append(f"    [火车] {start}-{end} {act.get('start', '')}→{act.get('end', '')} Y{cost}")
            elif act_type == "airplane":
                lines.append(f"    [飞机] {start}-{end} {act.get('start', '')}→{act.get('end', '')} Y{cost}")
            elif act_type == "accommodation":
                lines.append(f"    [住宿] {act.get('position', '')} Y{cost}")
            elif act_type == "attraction":
                lines.append(f"    [景点] {act.get('position', '')} {start}-{end} Y{cost}")
            elif act_type == "breakfast":
                lines.append(f"    [早餐] {act.get('position', '')} {start}-{end} Y{cost}")
            elif act_type == "lunch":
                lines.append(f"    [午餐] {act.get('position', '')} {start}-{end} Y{cost}")
            elif act_type == "dinner":
                lines.append(f"    [晚餐] {act.get('position', '')} {start}-{end} Y{cost}")
            else:
                lines.append(f"    [{act_type}] {act.get('position', '')} {start}-{end} Y{cost}")
        lines.append("")
    return "\n".join(lines)


def _format_plan_summary(plan: dict, req: dict) -> str:
    lines = [
        f"已为您规划好 {req.get('target_city', '')} {req.get('days', '?')}日游！",
        "",
    ]

    itinerary = plan.get("itinerary", [])
    total_cost = 0

    for day_data in itinerary:
        raw_day = day_data.get("day", 0)
        day_num = raw_day + 1 if raw_day == 0 else raw_day  # handle both 0-indexed and 1-indexed
        lines.append(f"--- Day {day_num} ---")

        for act in day_data.get("activities", []):
            act_type = act.get("type", "")
            start = act.get("start_time", "")
            end = act.get("end_time", "")
            cost = act.get("cost", 0) or act.get("price", 0) or 0
            if isinstance(cost, (int, float)):
                total_cost += cost

            if act_type == "train":
                lines.append(f"  [火车] {start}-{end} {act.get('start', '')} -> {act.get('end', '')}  Y{cost}")
            elif act_type == "airplane":
                lines.append(f"  [飞机] {start}-{end} {act.get('start', '')} -> {act.get('end', '')}  Y{cost}")
            elif act_type == "accommodation":
                lines.append(f"  [住宿] {act.get('position', '')}  Y{cost}")
            elif act_type == "attraction":
                lines.append(f"  [景点] {act.get('position', '')} {start}-{end}  Y{cost}")
            elif act_type == "breakfast":
                lines.append(f"  [早餐] {act.get('position', '')} {start}-{end}  Y{cost}")
            elif act_type == "lunch":
                lines.append(f"  [午餐] {act.get('position', '')} {start}-{end}  Y{cost}")
            elif act_type == "dinner":
                lines.append(f"  [晚餐] {act.get('position', '')} {start}-{end}  Y{cost}")
            else:
                lines.append(f"  [{act_type}] {act.get('position', '')} {start}-{end}  Y{cost}")

        lines.append("")

    stats = plan.get("search_time_sec", 0)
    nodes = plan.get("search_nodes", 0)
    backtracks = plan.get("backtrack_count", 0)

    lines.append(f"预估总花费: Y{total_cost}")
    lines.append(f"搜索统计: {stats:.1f}秒 | 搜索节点: {nodes} | 回溯次数: {backtracks}")

    return "\n".join(lines)


# ===== Background Cleanup =====
async def cleanup_old_sessions():
    while True:
        await asyncio.sleep(3600)
        cutoff = datetime.now() - timedelta(hours=24)
        async with sessions_lock:
            expired = [k for k, v in sessions.items()
                       if datetime.fromisoformat(v["created_at"]) < cutoff]
            for k in expired:
                conversation_id = sessions[k].get("conversation_id")
                del sessions[k]
                if conversation_id:
                    summary_locks.pop(str(conversation_id), None)
            if expired:
                print(f"Cleaned up {len(expired)} expired sessions")
        try:
            app_store.purge_expired_auth_sessions()
        except Exception as exc:
            print(f"Auth cleanup warning: {type(exc).__name__}: {exc}")


async def apply_review_decision(review_id: str, decision: str, reason: Optional[str], channel: str) -> None:
    """Atomically decide a review, then publish or reject its immutable plan snapshot."""
    review = review_store.decide(review_id, decision, reason, channel)
    session = await get_session(review["session_id"])
    if not session:
        async with sessions_lock:
            session = next(
                (item for item in sessions.values() if item.get("review_id") == review_id),
                None,
            )
    if not session:
        try:
            binding = app_store.find_checkpoint_by_review_id(review_id)
            if not binding:
                raise StoreNotFound("review checkpoint not found")
            session = await create_session(
                binding["user_id"], binding["conversation_id"]
            )
        except (StoreNotFound, KeyError):
            session = None
    if not session:
        return
    async with sessions_lock:
        session["review_status"] = decision
        if decision == "approved":
            plan = review["plan_snapshot"]
            request = review["request_snapshot"]
            display_plan = plan if plan.get("multi") else {"plans": [plan], "count": 1, "multi": True}
            summary = _format_multi_plan(display_plan, request)
            session["plan"] = plan
            session["pending_plan"] = None
            session["state"] = "done"
            session["review_message"] = "人工审核已通过"
            session["rejection_reason"] = None
            _append_assistant_message(
                session,
                "✅ 人工审核已通过，以下为正式发布的旅行方案。",
                "review_approved",
                request_id=f"review:{review_id}:approved",
                context_eligible=False,
                schedule_summary=False,
            )
            _append_assistant_message(
                session,
                summary,
                "plan",
                request_id=f"review:{review_id}:plan",
                context_eligible=False,
                plan=plan,
            )
        else:
            clean_reason = (reason or "").strip()
            session["plan"] = None
            session["pending_plan"] = None
            session["state"] = "review_rejected"
            session["review_message"] = "人工审核未通过"
            session["rejection_reason"] = clean_reason
            _append_assistant_message(
                session,
                f"人工审核未通过\n拒绝原因：{clean_reason}\n请修改旅行需求后重新生成方案。",
                "review_rejected",
                request_id=f"review:{review_id}:rejected",
                context_eligible=False,
                schedule_summary=False,
            )
        _checkpoint_session(session)


async def mark_review_error(review_id: str, message: str) -> None:
    try:
        review = review_store.get(review_id)
    except KeyError:
        return
    session = await get_session(review["session_id"])
    if not session:
        async with sessions_lock:
            session = next(
                (item for item in sessions.values() if item.get("review_id") == review_id),
                None,
            )
    if not session or session.get("review_status") not in {None, "pending"}:
        return
    async with sessions_lock:
        session["state"] = "review_error"
        session["review_status"] = "pending"
        session["review_message"] = "飞书审核服务异常，方案仍保持隐藏，可由本地管理员处理"
        _append_assistant_message(
            session,
            "飞书审核服务暂时不可用，方案仍保持隐藏，可由本地管理员继续审核。",
            "error",
            request_id=f"review:{review_id}:delivery-error",
            context_eligible=False,
            schedule_summary=False,
        )
        _checkpoint_session(session)


# ===== API Endpoints =====
async def initialize_long_term_memory() -> None:
    memory_collection = await long_term_memory_store.ensure_collection()
    if memory_collection.degraded:
        print(
            "Long-term memory collection unavailable; continuing without it: "
            f"{memory_collection.error}"
        )


@app.on_event("startup")
async def startup():
    app_store.initialize()
    # Keep the existing local-demo ADMIN_TOKEN usable as the first admin login
    # when no explicit bootstrap password was configured. Existing accounts are
    # never overwritten by bootstrap_user().
    if not os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip():
        legacy_password = os.getenv("ADMIN_TOKEN", "").strip()
        if legacy_password and legacy_password != "change-me-for-demo":
            app_store.bootstrap_user(
                os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin",
                legacy_password,
                role="admin",
            )
    app_store.purge_expired_auth_sessions()
    review_store.initialize()
    asyncio.create_task(cleanup_old_sessions())
    asyncio.create_task(initialize_long_term_memory())
    # Restore only reviews that were already accepted by GoHumanLoop. This
    # keeps an in-flight approval usable across a Web-service restart without
    # resending historical failed records or creating duplicate approvals.
    for review in review_store.list("pending"):
        if not review.get("external_request_id"):
            continue
        sid = review["session_id"]
        if sid not in sessions:
            try:
                binding = app_store.find_checkpoint_by_review_id(review["review_id"])
                if not binding:
                    raise StoreNotFound("review checkpoint not found")
                restored = await create_session(
                    binding["user_id"], binding["conversation_id"]
                )
                restored["state"] = "pending_review"
                restored["pending_plan"] = review["plan_snapshot"]
                restored["review_id"] = review["review_id"]
                restored["review_status"] = "pending"
                restored["review_message"] = "旅行方案已生成，正在等待人工审核"
                restored["sensitive"] = True
                restored["sensitive_reasons"] = review["sensitive_reasons"]
                _checkpoint_session(restored)
            except StoreNotFound:
                request = review["request_snapshot"]
                legacy = _base_session(sid)
                legacy.update({
                    "created_at": review["created_at"],
                    "messages": [{
                        "role": "assistant",
                        "content": "旅行方案已生成。由于包含特殊人群，完整方案需要人工审核通过后才能发布。",
                        "type": "review_pending",
                        "timestamp": review["created_at"],
                    }],
                    "state": "pending_review",
                    "extracted": request,
                    "pending_plan": review["plan_snapshot"],
                    "last_intent": "travel_planning",
                    "traveler_groups": request.get("traveler_groups", []),
                    "sensitive": True,
                    "sensitive_reasons": review["sensitive_reasons"],
                    "review_id": review["review_id"],
                    "review_status": "pending",
                    "review_message": "旅行方案已生成，正在等待人工审核",
                })
                sessions[sid] = legacy
        asyncio.create_task(
            send_to_gohumanloop(review, review_store, apply_review_decision, mark_review_error)
        )
    print("ChinaTravel Chat API ready at http://localhost:8000")


@app.get("/")
async def root():
    return FileResponse(os.path.join(frontend_dir, "index.html"))


@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(frontend_dir, "admin.html"))


def _validate_auth_input(body: AuthRequest) -> tuple[str, str]:
    username = " ".join(body.username.strip().split())
    password = body.password
    if not 2 <= len(username) <= 80:
        raise HTTPException(400, "用户名长度需为 2 到 80 个字符")
    if not 8 <= len(password) <= 128:
        raise HTTPException(400, "密码长度需为 8 到 128 个字符")
    return username, password


def _login_response(user: dict[str, Any]) -> JSONResponse:
    auth = app_store.create_auth_session(
        user["user_id"], session_days=AUTH_SESSION_DAYS
    )
    response = JSONResponse({"user": _public_user(auth["user"])})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        auth["token"],
        max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/register")
async def api_register(body: AuthRequest):
    username, password = _validate_auth_input(body)
    try:
        user = app_store.create_user(username, password, role="user")
    except StoreConflict:
        raise HTTPException(409, "用户名已存在")
    return _login_response(user)


@app.post("/api/auth/login")
async def api_login(body: AuthRequest):
    username, password = _validate_auth_input(body)
    try:
        user = app_store.authenticate_password(username, password)
    except InvalidCredentials:
        raise HTTPException(401, "用户名或密码错误")
    return _login_response(user)


@app.get("/api/auth/me")
async def api_auth_me():
    user = _current_user()
    return {"user": _public_user(user)}


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    raw_token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if raw_token:
        app_store.revoke_auth_session(raw_token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/api/conversations")
async def api_list_conversations(
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    user = _current_user()
    try:
        return app_store.list_conversations(
            user["user_id"], cursor=cursor, limit=limit
        )
    except ValueError:
        raise HTTPException(400, "会话分页游标无效")


@app.post("/api/conversations")
async def api_create_conversation(body: Optional[ConversationCreateRequest] = None):
    user = _current_user()
    title = (body.title if body else None) or "新对话"
    conversation = app_store.create_conversation(user["user_id"], title=title)
    session = await create_session(user["user_id"], conversation["conversation_id"])
    return {
        "conversation_id": conversation["conversation_id"],
        "session_id": session["session_id"],
        "state": session["state"],
    }


@app.post("/api/conversations/{conversation_id}/activate")
async def api_activate_conversation(conversation_id: str):
    user = _current_user()
    try:
        conversation = app_store.get_conversation(user["user_id"], conversation_id)
        if conversation.get("archived_at"):
            raise HTTPException(409, "该会话已归档")
        session = await create_session(user["user_id"], conversation_id)
    except StoreNotFound:
        raise HTTPException(404, "会话不存在")
    return {
        "conversation_id": conversation_id,
        "session_id": session["session_id"],
        "state": session["state"],
        "rag_job_id": session.get("rag_job_id"),
        "sensitive": session.get("sensitive", False),
        "review_message": session.get("review_message"),
    }


@app.get("/api/conversations/{conversation_id}/messages")
async def api_conversation_messages(
    conversation_id: str,
    before_seq: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    user = _current_user()
    try:
        return app_store.list_messages(
            user["user_id"],
            conversation_id,
            before_seq=before_seq,
            limit=limit,
        )
    except StoreNotFound:
        raise HTTPException(404, "会话不存在")


@app.post("/api/conversations/{conversation_id}/archive")
async def api_archive_conversation(conversation_id: str):
    user = _current_user()
    async with sessions_lock:
        active = next(
            (
                item for item in sessions.values()
                if item.get("user_id") == user["user_id"]
                and item.get("conversation_id") == conversation_id
            ),
            None,
        )
        if active and active.get("state") in {
            "generating", "rag_querying", "pending_review", "review_error"
        }:
            raise HTTPException(409, "当前任务尚未结束，暂时不能归档")
        if active:
            sessions.pop(active["session_id"], None)
    try:
        app_store.archive_conversation(user["user_id"], conversation_id)
    except StoreNotFound:
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


def _public_memory(memory: LongTermMemory) -> dict[str, Any]:
    return {
        "memory_id": memory.memory_id,
        "text": memory.normalized_text,
        "memory_type": memory.memory_type,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


@app.get("/api/memories")
async def api_list_memories(limit: int = Query(default=100, ge=1, le=200)):
    user = _current_user()
    result = await long_term_memory_store.list_for_user(
        user_id=user["user_id"], limit=limit
    )
    if result.degraded:
        raise HTTPException(503, "长期记忆服务暂时不可用")
    return {"items": [_public_memory(item) for item in result.memories]}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: str):
    user = _current_user()
    result = await long_term_memory_store.delete(
        user_id=user["user_id"], memory_id=memory_id
    )
    if result.degraded:
        raise HTTPException(503, "长期记忆服务暂时不可用")
    if not result.success:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True, "memory_id": memory_id}


@app.post("/api/sessions/new")
async def api_create_session():
    user = _current_user(required=False)
    if not user:
        session = await create_session()
        return {"session_id": session["session_id"], "created_at": session["created_at"]}
    conversation = app_store.create_conversation(user["user_id"])
    session = await create_session(user["user_id"], conversation["conversation_id"])
    return {
        "session_id": session["session_id"],
        "conversation_id": conversation["conversation_id"],
        "created_at": session["created_at"],
    }


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    user = _current_user(required=False)
    if user and session.get("user_id") != user["user_id"]:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session["session_id"],
        "state": session["state"],
        "messages": session["messages"],
        "plan": session["plan"],
        "progress": _public_session_progress(session),
        "intent": session.get("last_intent"),
        "sensitive": session.get("sensitive", False),
        "sensitive_reasons": session.get("sensitive_reasons", []),
        "review_status": session.get("review_status"),
        "review_message": session.get("review_message"),
        "rag_job_id": session.get("rag_job_id"),
        "rejection_reason": session.get("rejection_reason") if session.get("review_status") == "rejected" else None,
    }


@app.post("/api/sessions/{session_id}/reset")
async def api_reset_session(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    user = _current_user(required=False)
    if user and session.get("user_id") != user["user_id"]:
        raise HTTPException(404, "Session not found")
    if session.get("state") in {
        "generating", "rag_querying", "pending_review", "review_error"
    }:
        raise HTTPException(409, "当前任务尚未结束，不能重置会话")
    async with sessions_lock:
        reset_session_for_next_input(session)
    return {"ok": True, "state": "init"}


@app.get("/api/rag-jobs/{job_id}")
async def api_get_rag_job(job_id: str, session_id: str = Query(...)):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    user = _current_user(required=False)
    if user and session.get("user_id") != user["user_id"]:
        raise HTTPException(404, "Session not found")
    if session.get("rag_job_id") != job_id:
        raise HTTPException(404, "RAG 查询任务不属于当前会话或已结束")
    rag_request_id = str(session.get("current_request_id") or f"rag:{job_id}")

    try:
        job = await rag_service.get_job(job_id)
    except (RagConfigurationError, RagServiceError) as exc:
        message = f"知识库查询暂时不可用：{exc}"
        _append_assistant_message(
            session,
            message,
            "error",
            request_id=rag_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        response = {"type": "error", "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response
    except Exception:
        message = "知识库查询暂时不可用：Agentic RAG 服务发生内部错误"
        _append_assistant_message(
            session,
            message,
            "error",
            request_id=rag_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        response = {"type": "error", "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    if job["status"] in {"queued", "running"}:
        _checkpoint_session(session)
        return {
            "type": "rag_status",
            "status": job["status"],
            "job_id": job_id,
            "progress": job["progress"],
            "current_stage": job["current_stage"],
            "events": job["events"],
        }

    if job["status"] == "failed":
        reason = (job.get("error") or {}).get("message") or "Agentic RAG 查询失败"
        message = f"知识库查询暂时不可用：{reason}"
        response = {
            "type": "error",
            "message": message,
            "trace": job.get("events", []),
            "reset": True,
        }
        _append_assistant_message(
            session,
            message,
            "error",
            request_id=rag_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        reset_session_for_next_input(session)
        return response

    result = job["result"]
    _append_assistant_message(
        session,
        result["answer"],
        "rag",
        request_id=rag_request_id,
        context_eligible=False,
        sources=result["sources"],
        trace_id=result["trace_id"],
        rag_meta=result["meta"],
        trace=result["trace"],
    )
    response = {
        "type": "rag",
        "message": result["answer"],
        "found": result["found"],
        "sources": result["sources"],
        "trace_id": result["trace_id"],
        "rag_meta": result["meta"],
        "trace": result["trace"],
        "reset": True,
    }
    reset_session_for_next_input(session)
    return response


@app.get("/api/admin/reviews")
async def admin_reviews(status: Optional[str] = Query(default=None), authorization: Optional[str] = Header(default=None)):
    require_admin_token(authorization)
    return {"reviews": review_store.list(status)}


@app.get("/api/admin/reviews/{review_id}")
async def admin_review_detail(review_id: str, authorization: Optional[str] = Header(default=None)):
    require_admin_token(authorization)
    try:
        return review_store.get(review_id)
    except KeyError:
        raise HTTPException(404, "审核记录不存在")


@app.delete("/api/admin/reviews/{review_id}")
async def admin_delete_review(review_id: str, authorization: Optional[str] = Header(default=None)):
    require_admin_token(authorization)
    try:
        review = review_store.delete(review_id)
    except KeyError:
        raise HTTPException(404, "审核记录不存在")
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc))

    session = await get_session(review["session_id"])
    if not session:
        binding = app_store.find_checkpoint_by_review_id(review_id)
        if binding:
            session = await create_session(
                binding["user_id"], binding["conversation_id"]
            )
    if session and session.get("review_id") == review_id:
        async with sessions_lock:
            reset_session_for_next_input(session)
    return {"ok": True, "deleted_review_id": review_id}


@app.post("/api/admin/reviews/{review_id}/approve")
async def admin_approve(review_id: str, body: ReviewDecisionRequest, authorization: Optional[str] = Header(default=None)):
    require_admin_token(authorization)
    try:
        await apply_review_decision(review_id, "approved", body.reason, "admin")
        return {"ok": True, "review": review_store.get(review_id)}
    except KeyError:
        raise HTTPException(404, "审核记录不存在")
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/admin/reviews/{review_id}/reject")
async def admin_reject(review_id: str, body: ReviewDecisionRequest, authorization: Optional[str] = Header(default=None)):
    require_admin_token(authorization)
    if not (body.reason or "").strip():
        raise HTTPException(400, "拒绝原因不能为空")
    try:
        await apply_review_decision(review_id, "rejected", body.reason, "admin")
        return {"ok": True, "review": review_store.get(review_id)}
    except KeyError:
        raise HTTPException(404, "审核记录不存在")
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    session = await get_session(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    user = _current_user(required=False)
    if user and session.get("user_id") != user["user_id"]:
        raise HTTPException(404, "Session not found")
    if not req.message.strip():
        raise HTTPException(400, "消息不能为空")
    if len(req.message) > MAX_USER_MESSAGE_CHARS:
        raise HTTPException(
            400, f"单条消息不能超过 {MAX_USER_MESSAGE_CHARS} 个字符"
        )

    current_request_id = (req.request_id or uuid.uuid4().hex).strip()
    replayed = _replay_persisted_request(session, current_request_id)
    if replayed:
        return replayed

    if session.get("state") == "rag_querying" and session.get("rag_job_id"):
        return {
            "type": "rag_status",
            "status": "running",
            "job_id": session["rag_job_id"],
            "progress": 0,
            "message": "Agentic RAG 正在查询知识库",
        }
    if session.get("state") == "generating":
        return {
            "type": "status",
            "status": "generating",
            "message": "正在生成旅行计划，请稍候...",
            "sensitive": session.get("sensitive", False),
        }
    if session.get("state") in {"pending_review", "review_error"}:
        return {
            "type": "review_pending",
            "state": session["state"],
            "review_status": session.get("review_status") or "pending",
            "message": session.get("review_message")
            or "旅行方案已生成，正在等待人工审核",
        }

    # A terminal response may have been displayed while the explicit reset
    # request was interrupted. Treat the next user message as a fresh request
    # instead of merging it with the completed plan.
    if session.get("state") in {"done", "review_rejected"}:
        reset_session_for_next_input(session)

    session["current_request_id"] = current_request_id

    # Add and classify only the latest user input. A rejected plan is reset but
    # retained in SQLite as immutable audit history.
    user_message = {
        "role": "user",
        "content": req.message,
        "type": "chat",
        "request_id": current_request_id,
        "timestamp": datetime.now().isoformat(),
    }
    session["messages"].append(user_message)

    attack = precheck_attack(req.message)
    if attack:
        user_message["intent"] = "security_attack"
        _persist_runtime_message(
            session,
            user_message,
            request_id=current_request_id,
            context_eligible=False,
        )
        session["last_intent"] = "security_attack"
        session["state"] = "guardrail_blocked"
        message = "该请求涉及系统安全问题，我无法回答。"
        _append_assistant_message(
            session,
            message,
            "guardrail",
            request_id=current_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        response = {"type": "guardrail", "category": "security_attack", "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    llm = Deepseek()
    rag_query_text = req.message
    choice = req.message.strip()
    pending_mixed = session.get("pending_mixed_query")
    try:
        if session.get("state") == "awaiting_intent_choice" and pending_mixed and choice == "规则查询":
            decision = IntentDecision("rag_query", session.get("pending_mixed_category"), reason="用户选择规则查询")
            rag_query_text = pending_mixed
            session["pending_mixed_query"] = None
        elif session.get("state") == "awaiting_intent_choice" and pending_mixed and choice == "旅行方案制定":
            decision = IntentDecision("travel_planning", reason="用户选择旅行方案制定")
            session["pending_mixed_query"] = None
        else:
            decision = classify_intent(req.message, llm, session.get("state", "init"))
    except GuardrailClassificationError as exc:
        user_message["intent"] = "guardrail_error"
        _persist_runtime_message(
            session,
            user_message,
            request_id=current_request_id,
            context_eligible=False,
        )
        session["state"] = "guardrail_error"
        message = f"安全检查暂时不可用，请稍后重试。\n原因：{exc.public_reason}。"
        _append_assistant_message(
            session,
            message,
            "error",
            request_id=current_request_id,
            context_eligible=False,
            schedule_summary=False,
            error_code=exc.code,
        )
        response = {
            "type": "error",
            "state": "guardrail_error",
            "message": message,
            "error_code": exc.code,
            "error_reason": exc.public_reason,
            "reset": True,
        }
        reset_session_for_next_input(session)
        return response

    # A short benign follow-up may rely on the last trusted RAG category. The
    # safety classifier above still saw only the latest message.
    if (
        decision.intent == "irrelevant"
        and session.get("last_rag_category")
        and _looks_like_contextual_followup(req.message)
    ):
        decision = IntentDecision(
            "rag_query",
            str(session["last_rag_category"]),
            reason="基于上一轮规则查询识别为上下文追问",
        )

    user_message["intent"] = decision.intent
    _persist_runtime_message(
        session,
        user_message,
        request_id=current_request_id,
        context_eligible=decision.intent == "travel_planning",
    )
    session["last_intent"] = decision.intent

    if decision.intent == "security_attack":
        session["state"] = "guardrail_blocked"
        message = "该请求涉及系统安全问题，我无法回答。"
        _append_assistant_message(
            session, message, "guardrail", request_id=current_request_id,
            context_eligible=False, schedule_summary=False,
        )
        response = {"type": "guardrail", "category": decision.intent, "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    # A stand-alone "请记住……" command may legitimately be classified as
    # irrelevant to travel planning.  It is still a memory-system operation,
    # but only after both deterministic and LLM safety checks have passed.
    explicit_fact = _extract_explicit_memory_fact(req.message)
    if decision.intent == "irrelevant" and explicit_fact:
        memory_status, fact = await _store_explicit_memory(session, user_message)
        if memory_status == "saved":
            message = f"已记住：{fact}。之后会在相关问题中按需检索这条信息。"
        elif memory_status == "rejected":
            message = "这条信息涉及敏感个人信息或特殊人群信息，未写入长期记忆。"
        elif memory_status == "degraded":
            message = "长期记忆服务暂时不可用，本次信息未保存；当前对话仍可继续。"
        else:
            message = "没有识别到可保存的明确事实，本次未写入长期记忆。"
        _append_assistant_message(
            session,
            message,
            "memory",
            request_id=current_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        response = {
            "type": "memory",
            "message": message,
            "stored": memory_status == "saved",
            "reset": True,
        }
        reset_session_for_next_input(session)
        return response

    if decision.intent in {"rag_query", "travel_planning"}:
        asyncio.create_task(_store_explicit_memory(session, user_message))

    if decision.intent == "irrelevant":
        session["state"] = "irrelevant"
        message = "这个问题与旅行规划及旅行规则查询无关，我无法回答。"
        _append_assistant_message(
            session, message, "guardrail", request_id=current_request_id,
            context_eligible=False, schedule_summary=False,
        )
        response = {"type": "guardrail", "category": decision.intent, "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    if decision.mixed_request:
        session["state"] = "awaiting_intent_choice"
        session["pending_mixed_query"] = req.message
        session["pending_mixed_category"] = decision.rag_category
        message = "你的问题同时包含规则查询和旅行方案制定。请先选择一项：回复“规则查询”或“旅行方案制定”。"
        _append_assistant_message(
            session,
            message,
            "intent_choice",
            request_id=current_request_id,
            context_eligible=False,
            schedule_summary=False,
        )
        _checkpoint_session(session)
        return {"type": "intent_choice", "message": message}

    if decision.intent == "rag_query":
        previous_rag = _previous_rag_question(session, req.message)
        if previous_rag and _looks_like_contextual_followup(req.message):
            rag_query_text = (
                f"上一轮旅行规则问题：{previous_rag}\n"
                f"当前追问：{req.message}\n"
                "请结合上一轮主题，把当前追问作为独立问题回答。"
            )
        rag_category = decision.rag_category or session.get("last_rag_category")
        if not rag_category:
            session["state"] = "rag_error"
            message = "暂时无法确定要查询的规则类别，请明确说明儿童票、老人票、学生票、航班、高铁或景点注意事项。"
            _append_assistant_message(
                session,
                message,
                "error",
                request_id=current_request_id,
                context_eligible=False,
                schedule_summary=False,
            )
            response = {"type": "error", "message": message, "reset": True}
            reset_session_for_next_input(session)
            return response
        try:
            job = await rag_service.start_job(
                rag_query_text,
                str(rag_category),
                session_id=req.session_id,
                request_id=current_request_id,
            )
            session["state"] = "rag_querying"
            session["rag_job_id"] = job["job_id"]
            session["last_rag_category"] = str(rag_category)
            _checkpoint_session(session)
            return {
                "type": "rag_status",
                "status": job["status"],
                "job_id": job["job_id"],
                "progress": job["progress"],
                "message": "Agentic RAG 正在查询知识库",
            }
        except (RagConfigurationError, RagServiceError) as exc:
            # Do not fall through into planning or fabricate an answer.
            session["state"] = "rag_error"
            message = f"知识库查询暂时不可用：{exc}"
            _append_assistant_message(
                session, message, "error", request_id=current_request_id,
                context_eligible=False, schedule_summary=False,
            )
            response = {"type": "error", "message": message, "reset": True}
            reset_session_for_next_input(session)
            return response
        except Exception:
            # Unexpected implementation details are not exposed to the browser.
            session["state"] = "rag_error"
            message = "知识库查询暂时不可用：Agentic RAG 服务发生内部错误"
            _append_assistant_message(
                session, message, "error", request_id=current_request_id,
                context_eligible=False, schedule_summary=False,
            )
            response = {"type": "error", "message": message, "reset": True}
            reset_session_for_next_input(session)
            return response

    if session.get("state") == "review_rejected":
        session["pending_plan"] = None
        session["plan"] = None
        session["review_id"] = None
        session["review_status"] = None
        session["review_message"] = None
        session["rejection_reason"] = None

    refresh_sensitive_state(session, req.message)

    # Step 1: Extract requirements
    conversation = await _managed_conversation(session, req.message)
    extraction = await extract_requirements(llm, conversation, session["extracted"], req.message)
    session["extracted"] = extraction.get("merged", session["extracted"])

    # Step 2: Check required fields
    REQUIRED = ["target_city", "days"]
    missing = [f for f in REQUIRED if not session["extracted"].get(f)]

    if missing:
        clarification = extraction.get("clarification_question",
            f"还需要以下信息：{'、'.join(missing)}，请告诉我~")
        session["state"] = "clarifying"

        city = session["extracted"].get("target_city", "")
        if city and city not in SUPPORTED_CITIES:
            clarification = f"抱歉，目前只支持以下城市：{' / '.join(SUPPORTED_CITIES)}。请选择一个目的地城市~"
            session["extracted"]["target_city"] = None

        _append_assistant_message(
            session,
            clarification,
            "clarification",
            request_id=current_request_id,
            context_eligible=True,
        )
        _checkpoint_session(session)
        return {
            "type": "clarification",
            "message": clarification,
            "missing_fields": missing,
            "current_requirements": {k: v for k, v in session["extracted"].items() if v},
        }

    # Validate cities
    for field in ["target_city", "start_city"]:
        city = session["extracted"].get(field, "")
        if city and city not in SUPPORTED_CITIES:
            msg = f"抱歉，{city} 暂不支持。支持的城市：{' / '.join(SUPPORTED_CITIES)}。请换个城市~"
            session["state"] = "clarifying"
            session["extracted"][field] = None
            _append_assistant_message(
                session,
                msg,
                "clarification",
                request_id=current_request_id,
                context_eligible=True,
            )
            _checkpoint_session(session)
            return {"type": "clarification", "message": msg, "missing_fields": [field],
                    "current_requirements": {k: v for k, v in session["extracted"].items() if v}}

    # Step 3: Confirm with user
    if session["state"] != "done":
        confirm_keywords = ["确认", "是", "对", "可以", "行", "好", "ok", "yes", "开始", "生成"]
        is_confirm = any(kw in req.message.lower() for kw in confirm_keywords)
        is_confirm = is_confirm or req.message.strip().lower() in {"确认并开始规划", "确认", "ok", "yes"}

        if not is_confirm and session["state"] != "generating":
            req_summary = f"""请确认以下信息：
  目的地: {session['extracted'].get('target_city', '?')}
  出发地: {session['extracted'].get('start_city', '未指定')}
  天数: {session['extracted'].get('days', '?')} 天
  人数: {session['extracted'].get('people_number', 1)} 人
  预算: {session['extracted'].get('budget', '不限')} 元
  偏好: {session['extracted'].get('preferences', '无特殊偏好')}
  方案目标: {optimization_goal_label(session['extracted'].get('optimization_goal'))}

回复"确认"开始生成旅行计划，或者继续补充需求~"""
            if session.get("sensitive"):
                people_text = "、".join(session.get("sensitive_reasons", []))
                req_summary += f"\n\n⚠️ 敏感方案：{people_text}\n方案生成后须经人工审核，通过后才会发布。"
            session["state"] = "confirmed"
            _append_assistant_message(
                session,
                req_summary,
                "confirmation",
                request_id=current_request_id,
                context_eligible=True,
                current_requirements={
                    key: value for key, value in session["extracted"].items() if value
                },
            )
            _checkpoint_session(session)
            return {"type": "confirmation", "message": req_summary, "missing_fields": [],
                    "current_requirements": {k: v for k, v in session["extracted"].items() if v}}

    # Pre-check: warn if budget is unrealistically low
    budget = session["extracted"].get("budget")
    if budget is not None and isinstance(budget, (int, float)) and budget < 500:
        days = session["extracted"].get("days", 1)
        msg = (
            f"预算 {budget} 元可能不足以完成 {days} 天旅行。\n"
            f"即使是最节省的情况下，{days} 天至少也需要约 "
            f"{500 * days} 元（往返交通 + 住宿 + 餐饮）。\n"
            f"建议把预算调到 {800 * days} 元以上再试试~"
        )
        session["state"] = "clarifying"
        session["extracted"]["budget"] = None
        _append_assistant_message(
            session,
            msg,
            "clarification",
            request_id=current_request_id,
            context_eligible=True,
        )
        _checkpoint_session(session)
        return {"type": "clarification", "message": msg, "missing_fields": [],
                "current_requirements": {k: v for k, v in session["extracted"].items() if v}}

    # Step 4: Generate plan
    session["state"] = "generating"
    session["messages"].append({
        "role": "assistant",
        "content": "开始生成旅行计划...",
        "type": "status",
        "timestamp": datetime.now().isoformat(),
    })

    _checkpoint_session(session)

    asyncio.create_task(generate_plan_background(session))

    return {
        "type": "status",
        "status": "generating",
        "message": "正在生成旅行计划，请稍候...",
        "sensitive": session.get("sensitive", False),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
