"""SQLite review storage and optional GoHumanLoop/Feishu integration."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("REVIEW_DB_PATH", str(PROJECT_ROOT / "data" / "reviews.sqlite3")))
GOHUMANLOOP_DB_PATH = PROJECT_ROOT / "deploy" / "gohumanloop-feishu" / "data" / "gohumanloop.db"
GOHUMANLOOP_CONF_PATH = PROJECT_ROOT / "deploy" / "gohumanloop-feishu" / "conf" / "app.conf"
_FEISHU_TOKEN_CACHE = ""
_FEISHU_TOKEN_EXPIRES_AT = 0.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ReviewConflict(RuntimeError):
    pass


def _read_gohumanloop_config() -> dict[str, str]:
    path = Path(os.getenv("GOHUMANLOOP_FEISHU_CONF_PATH", str(GOHUMANLOOP_CONF_PATH)))
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _find_feishu_instance_code(request_id: str) -> str:
    """Resolve the Feishu instance code persisted by GoHumanLoop."""
    path = Path(os.getenv("GOHUMANLOOP_DB_PATH", str(GOHUMANLOOP_DB_PATH)))
    if not path.exists():
        return ""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        row = connection.execute(
            "SELECT sp_no FROM human_loops WHERE request_id = ? AND is_deleted = 0",
            (request_id,),
        ).fetchone()
    return str(row[0] if row else "").strip()


async def query_feishu_approval_fallback(
    client: httpx.AsyncClient, request_id: str
) -> Optional[dict[str, Any]]:
    """Query Feishu directly when GoHumanLoop's event-driven status is stale.

    GoHumanLoop remains the primary integration. This local-demo fallback uses
    the instance code it persisted and never creates or decides an approval.
    """
    instance_code = _find_feishu_instance_code(request_id)
    config = _read_gohumanloop_config()
    app_id = config.get("appid", "")
    app_secret = config.get("appsecret", "")
    if not instance_code or not app_id or not app_secret:
        return None

    global _FEISHU_TOKEN_CACHE, _FEISHU_TOKEN_EXPIRES_AT
    now = asyncio.get_running_loop().time()
    if not _FEISHU_TOKEN_CACHE or now >= _FEISHU_TOKEN_EXPIRES_AT:
        token_response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        if token_data.get("code") != 0 or not token_data.get("tenant_access_token"):
            raise RuntimeError(token_data.get("msg") or "获取飞书 tenant_access_token 失败")
        _FEISHU_TOKEN_CACHE = str(token_data["tenant_access_token"])
        expires_in = max(60, int(token_data.get("expire") or 7200) - 60)
        _FEISHU_TOKEN_EXPIRES_AT = now + expires_in

    instance_response = await client.get(
        f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_code}",
        headers={"Authorization": f"Bearer {_FEISHU_TOKEN_CACHE}"},
    )
    instance_response.raise_for_status()
    instance_data = instance_response.json()
    if instance_data.get("code") != 0:
        raise RuntimeError(instance_data.get("msg") or "查询飞书审批实例失败")

    data = instance_data.get("data") or {}
    remote_status = str(data.get("status") or "PENDING").upper()
    if remote_status == "APPROVED":
        return {"status": "approved", "response": None}
    if remote_status == "REJECTED":
        reason = ""
        for event in reversed(data.get("timeline") or []):
            if str(event.get("type") or "").upper() == "REJECT":
                reason = str(event.get("comment") or "").strip()
                if reason:
                    break
        return {"status": "rejected", "response": {"reason": reason}}
    if remote_status in {"CANCELED", "DELETED"}:
        return {"status": "cancelled", "response": None}
    return {"status": "pending", "response": None}


class ReviewStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_snapshot TEXT NOT NULL,
                    plan_snapshot TEXT NOT NULL,
                    sensitive_reasons TEXT NOT NULL,
                    review_channel TEXT NOT NULL DEFAULT 'pending',
                    reviewer_comment TEXT,
                    external_request_id TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status, created_at)")

    def create(self, session_id: str, request: dict, plan: dict, reasons: list[str]) -> dict[str, Any]:
        review_id = uuid.uuid4().hex
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO reviews VALUES (?, ?, 'pending', ?, ?, ?, 'pending', NULL, NULL, ?, NULL)",
                (review_id, session_id, json.dumps(request, ensure_ascii=False), json.dumps(plan, ensure_ascii=False),
                 json.dumps(reasons, ensure_ascii=False), _now()),
            )
        return self.get(review_id)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("request_snapshot", "plan_snapshot", "sensitive_reasons"):
            data[key] = json.loads(data[key])
        return data

    def get(self, review_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        if not row:
            raise KeyError(review_id)
        return self._decode(row)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM reviews"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def delete(self, review_id: str) -> dict[str, Any]:
        """Permanently delete a completed review; pending reviews are immutable."""
        with closing(self.connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if not row:
                raise KeyError(review_id)
            review = self._decode(row)
            if review["status"] == "pending":
                raise ReviewConflict("待审核记录不能删除")
            cursor = connection.execute(
                "DELETE FROM reviews WHERE review_id = ? AND status != 'pending'",
                (review_id,),
            )
            if cursor.rowcount != 1:
                raise ReviewConflict("审核状态已变化，请刷新后重试")
        return review

    def attach_external(self, review_id: str, request_id: str) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """UPDATE reviews
                   SET external_request_id = ?, review_channel = 'feishu', reviewer_comment = NULL
                   WHERE review_id = ? AND status = 'pending'""",
                (request_id, review_id),
            )

    def mark_error(self, review_id: str, message: str) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "UPDATE reviews SET review_channel = 'review_error', reviewer_comment = ? WHERE review_id = ? AND status = 'pending'",
                (message[:500], review_id),
            )

    def decide(self, review_id: str, decision: str, reason: str | None, channel: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        clean_reason = (reason or "").strip()
        if decision == "rejected" and not clean_reason:
            raise ValueError("拒绝原因不能为空")
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE reviews
                   SET status = ?, reviewer_comment = ?, review_channel = ?, reviewed_at = ?
                   WHERE review_id = ? AND status = 'pending'""",
                (decision, clean_reason or None, channel, _now(), review_id),
            )
            if cursor.rowcount != 1:
                raise ReviewConflict("该审核已由其他渠道处理")
        return self.get(review_id)


async def send_to_gohumanloop(
    review: dict[str, Any],
    store: ReviewStore,
    on_result: Callable[[str, str, str | None, str], Awaitable[None]],
    on_error: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Submit an approval through the GoHumanLoop API service and poll its result."""
    base_url = os.getenv("GOHUMANLOOP_API_URL", "").strip()
    api_key = os.getenv("GOHUMANLOOP_API_KEY", "").strip()
    if not base_url or not api_key:
        return

    def extract_reason(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("reason", "comment", "message", "text"):
                text = str(value.get(key) or "").strip()
                if text:
                    return text
        return ""

    try:
        api_root = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}
        request_id = str(review.get("external_request_id") or "").strip()
        timeout_seconds = max(1, int(os.getenv("GOHUMANLOOP_TIMEOUT", "300")))
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        # GoHumanLoop is a localhost service. Bypass machine-wide HTTP proxy
        # settings, which otherwise turn 127.0.0.1 requests into 502 errors.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            if not request_id:
                request_id = uuid.uuid4().hex
                payload = {
                    "task_id": review["review_id"],
                    "conversation_id": review["session_id"],
                    "request_id": request_id,
                    "loop_type": "approval",
                    "platform": "feishu",
                    "context": {
                        "message": json.dumps(review["plan_snapshot"], ensure_ascii=False)[:3000],
                        "question": "请审核特殊人群旅行方案：" + "；".join(review["sensitive_reasons"]),
                        "additional": json.dumps(review["request_snapshot"], ensure_ascii=False)[:2000],
                    },
                    "metadata": {"reject_requires_reason": True},
                }
                response = await client.post(f"{api_root}/v1/humanloop/request", headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()
                if not result.get("success"):
                    raise RuntimeError(result.get("error") or "GoHumanLoop 未接受审核请求")
                store.attach_external(review["review_id"], request_id)

            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(5)
                response = await client.get(
                    f"{api_root}/v1/humanloop/status",
                    headers=headers,
                    params={
                        "conversation_id": review["session_id"],
                        "request_id": request_id,
                        "platform": "feishu",
                    },
                )
                response.raise_for_status()
                result = response.json()
                if not result.get("success"):
                    raise RuntimeError(result.get("error") or "GoHumanLoop 状态查询失败")
                status = str(result.get("status") or "pending").lower()
                if status == "pending":
                    # Some GoHumanLoop-Feishu deployments successfully create
                    # approvals but miss Feishu's status-change event. Verify
                    # the persisted Feishu instance directly before continuing
                    # to report a stale pending state.
                    fallback = await query_feishu_approval_fallback(client, request_id)
                    if fallback:
                        status = fallback["status"]
                        if fallback.get("response") is not None:
                            result["response"] = fallback["response"]
                if status == "approved":
                    await on_result(review["review_id"], "approved", None, "feishu")
                    return
                if status == "rejected":
                    reason = extract_reason(result.get("response")) or extract_reason(result.get("feedback"))
                    if reason:
                        await on_result(review["review_id"], "rejected", reason, "feishu")
                    else:
                        message = "飞书拒绝结果缺少必填原因，仍保持待审核"
                        store.mark_error(review["review_id"], message)
                        if on_error:
                            await on_error(review["review_id"], message)
                    return
                if status in {"error", "expired", "cancelled"}:
                    raise RuntimeError(extract_reason(result.get("error")) or f"审核状态异常：{status}")

        raise TimeoutError(f"飞书审核超过 {timeout_seconds} 秒，仍保持待审核")
    except ReviewConflict:
        return
    except Exception as exc:
        message = f"GoHumanLoop 调用失败: {exc}"
        store.mark_error(review["review_id"], message)
        if on_error:
            await on_error(review["review_id"], message)
