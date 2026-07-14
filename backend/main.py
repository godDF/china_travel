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
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from chinatravel.agent.llms import Deepseek
from chinatravel.agent.load_model import init_agent
from chinatravel.environment.world_env import WorldEnv
from backend.safety import IntentDecision, classify_intent, precheck_attack, sensitive_reasons, update_traveler_groups
from backend.rag import RagConfigurationError, RagService
from backend.reviews import ReviewConflict, ReviewStore, send_to_gohumanloop
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

# Shared WorldEnv instance (load DB once)
env = WorldEnv(lang="zh")

# Supported cities
SUPPORTED_CITIES = ["北京", "上海", "南京", "苏州", "杭州", "深圳", "成都", "武汉", "广州", "重庆"]

# ===== Pydantic Models =====
class ChatRequest(BaseModel):
    session_id: str
    message: str


class ReviewDecisionRequest(BaseModel):
    reason: Optional[str] = None


def require_admin_token(authorization: Optional[str] = Header(default=None)) -> None:
    configured = os.getenv("ADMIN_TOKEN", "change-me-for-demo")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != configured:
        raise HTTPException(401, "管理员 Token 无效")

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
    # Match the actual mode-ranking output only. The old broad
    # `innercity_transport` pattern also matched three hard-constraint source
    # lines during every validation, making the UI show misleading counts such
    # as x48 even though modes were not selected 48 times.
    ("inner_transport", r"selected\s+transportranking\s*:", "选择市内交通"),
    ("planning", r"POI planning|add_|backtrack|Plan|searching", "DFS搜索生成行程"),
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
async def create_session() -> dict:
    sid = uuid.uuid4().hex[:12]
    async with sessions_lock:
        sessions[sid] = {
            "session_id": sid,
            "created_at": datetime.now().isoformat(),
            "messages": [],
            "state": "init",
            "extracted": {"optimization_goal": DEFAULT_OPTIMIZATION_GOAL},
            "plan": None,
            "pending_plan": None,
            "progress": [],
            "last_intent": None,
            "traveler_groups": [],
            "sensitive": False,
            "sensitive_reasons": [],
            "review_id": None,
            "review_status": None,
            "review_message": None,
            "rejection_reason": None,
        }
    return sessions[sid]


async def get_session(sid: str) -> Optional[dict]:
    async with sessions_lock:
        return sessions.get(sid)


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
- constraints: 限制条件（如只坐地铁、不要辣等）
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
    "constraints": "只坐地铁",
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
    req = session["extracted"]
    llm = Deepseek()

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
        "backbone_llm": llm,
        "cache_dir": os.path.join(project_root, "cache"),
        "log_dir": os.path.join(project_root, "cache", "web", session["session_id"]),
        "debug": True,  # Enable debug so Logger forwards to our interceptor; spam suppressed by _bt_log
        "time_cut": 20,
        "max_plans": 3,
        # budget_fit needs a wider pool because later, more expensive plans may
        # be closer to the budget. min_total_cost searches cheap-first, so the
        # first three distinct valid plans already satisfy the output objective.
        "max_candidates": 3 if cheapest_branch else 9,
        # Bound POI branching so a complete 3-day path is reached before the
        # DFS spends its entire budget enumerating alternatives for Day 1/2.
        "search_width": 4,
        # The LLM has already ranked metro/taxi/walk from the user's request.
        # Expanding all three again at every DFS edge triples the branch factor.
        # Preselect a diverse preference/price pool before expensive route and
        # constraint evaluation; search_width is applied after this stage.
        "poi_candidate_width": 24,
        # The default keeps the original budget-proximity branch unchanged.
        # min_total_cost activates the isolated low-cost candidate branch in
        # NesyAgent; a wider mode list lets feasibility checks fall back from
        # walking to metro/taxi when walking cannot form a complete itinerary.
        "optimization_goal": req.get("optimization_goal", DEFAULT_OPTIMIZATION_GOAL),
        "inner_transport_width": 3 if cheapest_branch else 1,
        # With no POI preference or extra constraint, LLM recommendations are
        # immediately overwritten by price ordering in the cheapest branch.
        "cost_only_search": cheapest_branch and not req.get("preferences") and not req.get("constraints"),
    }
    agent = init_agent(agent_kwargs)

    # Set up progress interceptor
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
    interceptor = ProgressInterceptor(sys.__stdout__, progress)
    old_stdout = sys.stdout
    sys.stdout = interceptor

    try:
        loop = asyncio.get_event_loop()
        succ, plan = await loop.run_in_executor(None, agent.run, query, True)  # load_cache=True, key is content-based
        plan = _convert_numpy(plan) if isinstance(plan, dict) else plan
    except Exception as e:
        succ = False
        plan = {"error": str(e)}
    finally:
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
            if session.get("sensitive"):
                review = review_store.create(
                    session["session_id"],
                    dict(req),
                    plan,
                    list(session.get("sensitive_reasons", [])),
                )
                session["pending_plan"] = plan
                session["plan"] = None
                session["review_id"] = review["review_id"]
                session["review_status"] = "pending"
                session["review_message"] = "旅行方案已生成，正在等待人工审核"
                session["state"] = "pending_review"
                session["messages"].append({
                    "role": "assistant",
                    "content": "旅行方案已生成。由于包含未成年人、儿童或老人，完整方案需要人工审核通过后才能发布。",
                    "type": "review_pending",
                    "timestamp": datetime.now().isoformat(),
                })
                review_to_submit = review
            else:
                session["plan"] = plan
                session["pending_plan"] = None
                session["state"] = "done"
                session["messages"].append({
                    "role": "assistant",
                    "content": summary,
                    "type": "plan",
                    "plan": plan,
                    "timestamp": datetime.now().isoformat(),
                })
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

            if error_info == "TimeOutError" or stats.get("dfs_timeout"):
                reason = (
                    f"⏱ 搜索超时（20秒内未找到完整方案）。\n\n"
                    f"已尝试 {plan.get('backtrack_count', '?')} 个方案组合。\n"
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

            session["messages"].append({
                "role": "assistant",
                "content": reason,
                "type": "error",
                "timestamp": datetime.now().isoformat(),
            })

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
                del sessions[k]
            if expired:
                print(f"Cleaned up {len(expired)} expired sessions")


async def apply_review_decision(review_id: str, decision: str, reason: Optional[str], channel: str) -> None:
    """Atomically decide a review, then publish or reject its immutable plan snapshot."""
    review = review_store.decide(review_id, decision, reason, channel)
    session = await get_session(review["session_id"])
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
            session["messages"].append({
                "role": "assistant",
                "content": "✅ 人工审核已通过，以下为正式发布的旅行方案。",
                "type": "review_approved",
                "timestamp": datetime.now().isoformat(),
            })
            session["messages"].append({
                "role": "assistant", "content": summary, "type": "plan", "plan": plan,
                "timestamp": datetime.now().isoformat(),
            })
        else:
            clean_reason = (reason or "").strip()
            session["plan"] = None
            session["pending_plan"] = None
            session["state"] = "review_rejected"
            session["review_message"] = "人工审核未通过"
            session["rejection_reason"] = clean_reason
            session["messages"].append({
                "role": "assistant",
                "content": f"人工审核未通过\n拒绝原因：{clean_reason}\n请修改旅行需求后重新生成方案。",
                "type": "review_rejected",
                "timestamp": datetime.now().isoformat(),
            })


async def mark_review_error(review_id: str, message: str) -> None:
    try:
        review = review_store.get(review_id)
    except KeyError:
        return
    session = await get_session(review["session_id"])
    if not session or session.get("review_status") not in {None, "pending"}:
        return
    async with sessions_lock:
        session["state"] = "review_error"
        session["review_status"] = "pending"
        session["review_message"] = "飞书审核服务异常，方案仍保持隐藏，可由本地管理员处理"


# ===== API Endpoints =====
@app.on_event("startup")
async def startup():
    review_store.initialize()
    asyncio.create_task(cleanup_old_sessions())
    # Restore only reviews that were already accepted by GoHumanLoop. This
    # keeps an in-flight approval usable across a Web-service restart without
    # resending historical failed records or creating duplicate approvals.
    for review in review_store.list("pending"):
        if not review.get("external_request_id"):
            continue
        sid = review["session_id"]
        if sid not in sessions:
            request = review["request_snapshot"]
            sessions[sid] = {
                "session_id": sid,
                "created_at": review["created_at"],
                "messages": [{
                    "role": "assistant",
                    "content": "旅行方案已生成。由于包含特殊人群，完整方案需要人工审核通过后才能发布。",
                    "type": "review_pending",
                    "timestamp": review["created_at"],
                }],
                "state": "pending_review",
                "extracted": request,
                "plan": None,
                "pending_plan": review["plan_snapshot"],
                "progress": [],
                "last_intent": "travel_planning",
                "traveler_groups": request.get("traveler_groups", []),
                "sensitive": True,
                "sensitive_reasons": review["sensitive_reasons"],
                "review_id": review["review_id"],
                "review_status": "pending",
                "review_message": "旅行方案已生成，正在等待人工审核",
                "rejection_reason": None,
            }
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


@app.post("/api/sessions/new")
async def api_create_session():
    session = await create_session()
    return {"session_id": session["session_id"], "created_at": session["created_at"]}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session["session_id"],
        "state": session["state"],
        "messages": session["messages"],
        "plan": session["plan"],
        "progress": session.get("progress", []),
        "intent": session.get("last_intent"),
        "sensitive": session.get("sensitive", False),
        "sensitive_reasons": session.get("sensitive_reasons", []),
        "review_status": session.get("review_status"),
        "review_message": session.get("review_message"),
        "rejection_reason": session.get("rejection_reason") if session.get("review_status") == "rejected" else None,
    }


@app.post("/api/sessions/{session_id}/reset")
async def api_reset_session(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    async with sessions_lock:
        reset_session_for_next_input(session)
    return {"ok": True, "state": "init"}


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

    # A terminal response may have been displayed while the explicit reset
    # request was interrupted. Treat the next user message as a fresh request
    # instead of merging it with the completed plan.
    if session.get("state") in {"done", "review_rejected"}:
        reset_session_for_next_input(session)

    # Add and classify only the latest user input. A rejected plan is reset but
    # retained in SQLite as immutable audit history.
    user_message = {
        "role": "user",
        "content": req.message,
        "timestamp": datetime.now().isoformat(),
    }
    session["messages"].append(user_message)

    attack = precheck_attack(req.message)
    if attack:
        user_message["intent"] = "security_attack"
        session["last_intent"] = "security_attack"
        session["state"] = "guardrail_blocked"
        message = "该请求涉及系统安全问题，我无法回答。"
        session["messages"].append({"role": "assistant", "content": message, "type": "guardrail", "timestamp": datetime.now().isoformat()})
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
    except RuntimeError:
        session["state"] = "guardrail_error"
        message = "安全检查暂时不可用，请稍后重试。"
        session["messages"].append({"role": "assistant", "content": message, "type": "error", "timestamp": datetime.now().isoformat()})
        response = {"type": "error", "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    user_message["intent"] = decision.intent
    session["last_intent"] = decision.intent

    if decision.intent == "security_attack":
        session["state"] = "guardrail_blocked"
        message = "该请求涉及系统安全问题，我无法回答。"
        session["messages"].append({"role": "assistant", "content": message, "type": "guardrail", "timestamp": datetime.now().isoformat()})
        response = {"type": "guardrail", "category": decision.intent, "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    if decision.intent == "irrelevant":
        session["state"] = "irrelevant"
        message = "这个问题与旅行规划及旅行规则查询无关，我无法回答。"
        session["messages"].append({"role": "assistant", "content": message, "type": "guardrail", "timestamp": datetime.now().isoformat()})
        response = {"type": "guardrail", "category": decision.intent, "message": message, "reset": True}
        reset_session_for_next_input(session)
        return response

    if decision.mixed_request:
        session["state"] = "awaiting_intent_choice"
        session["pending_mixed_query"] = req.message
        session["pending_mixed_category"] = decision.rag_category
        message = "你的问题同时包含规则查询和旅行方案制定。请先选择一项：回复“规则查询”或“旅行方案制定”。"
        session["messages"].append({"role": "assistant", "content": message, "type": "intent_choice", "timestamp": datetime.now().isoformat()})
        return {"type": "intent_choice", "message": message}

    if decision.intent == "rag_query":
        try:
            result = await rag_service.answer(rag_query_text, decision.rag_category, llm)
            session["state"] = "idle"
            session["messages"].append({
                "role": "assistant",
                "content": result["answer"],
                "type": "rag",
                "sources": result["sources"],
                "timestamp": datetime.now().isoformat(),
            })
            response = {
                "type": "rag", "message": result["answer"], "found": result["found"],
                "sources": result["sources"], "reset": True,
            }
            reset_session_for_next_input(session)
            return response
        except Exception as exc:
            # Do not fall through into planning or fabricate an answer.
            session["state"] = "rag_error"
            message = f"知识库查询暂时不可用：{exc}"
            session["messages"].append({"role": "assistant", "content": message, "type": "error", "timestamp": datetime.now().isoformat()})
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
    conversation = [
        {"role": m["role"], "content": m["content"]}
        for m in session["messages"]
        if m.get("type") not in {"status", "rag", "guardrail", "review_rejected"}
        and (m.get("role") != "user" or m.get("intent") == "travel_planning")
    ]
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

        session["messages"].append({
            "role": "assistant",
            "content": clarification,
            "type": "clarification",
            "timestamp": datetime.now().isoformat(),
        })
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
            session["messages"].append({
                "role": "assistant", "content": msg, "type": "clarification",
                "timestamp": datetime.now().isoformat(),
            })
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
            session["messages"].append({
                "role": "assistant", "content": req_summary, "type": "confirmation",
                "timestamp": datetime.now().isoformat(),
            })
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
        session["messages"].append({
            "role": "assistant", "content": msg, "type": "clarification",
            "timestamp": datetime.now().isoformat(),
        })
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
