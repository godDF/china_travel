"""Knowledge-base helpers and the Agentic RAG runtime client.

The Markdown, embedding and Qdrant helpers remain available for the legacy
indexing script.  User-facing queries are delegated to the standalone Agentic
RAG service so ChinaTravel does not repeat retrieval or answer generation.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml
from qdrant_client import QdrantClient, models


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_ROOT = PROJECT_ROOT / "kb"
COLLECTION = os.getenv("QDRANT_COLLECTION", "chinatravel_safety_knowledge")
VECTOR_SIZE = int(os.getenv("BGE_M3_VECTOR_SIZE", "1024"))
TOP_K = int(os.getenv("RAG_TOP_K", "4"))
SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.55"))
AGENTIC_RAG_API_URL = os.getenv("AGENTIC_RAG_API_URL", "http://127.0.0.1:8100").rstrip("/")
AGENTIC_RAG_TIMEOUT_SECONDS = float(os.getenv("AGENTIC_RAG_TIMEOUT_SECONDS", "30"))
AGENTIC_RAG_API_KEY = os.getenv("AGENTIC_RAG_API_KEY", "").strip()


class RagConfigurationError(RuntimeError):
    pass


class RagServiceError(RuntimeError):
    """A browser-safe failure raised by the remote Agentic RAG client."""


def _query_endpoint(base_url: str) -> str:
    if not base_url:
        raise RagConfigurationError("未配置 AGENTIC_RAG_API_URL")
    if base_url.endswith("/api/v1/query"):
        return base_url
    return f"{base_url}/api/v1/query"


def _service_root(base_url: str) -> str:
    if not base_url:
        raise RagConfigurationError("未配置 AGENTIC_RAG_API_URL")
    for suffix in ("/api/v1/query-jobs", "/api/v1/query"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    return base_url


def _validate_non_negative_int(meta: dict[str, Any], field: str) -> None:
    value = meta.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RagServiceError(f"Agentic RAG 响应字段 meta.{field} 无效")


def _validate_trace_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RagServiceError("Agentic RAG 响应缺少有效的 trace 字段")
    events: list[dict[str, Any]] = []
    allowed_types = {"agent", "tool", "llm", "system"}
    allowed_statuses = {"running", "completed", "failed", "fallback"}
    for event in value:
        if not isinstance(event, dict):
            raise RagServiceError("Agentic RAG Trace 事件格式无效")
        if not isinstance(event.get("event_id"), str) or not event["event_id"]:
            raise RagServiceError("Agentic RAG Trace 缺少 event_id")
        if event.get("event_type") not in allowed_types:
            raise RagServiceError("Agentic RAG Trace 的 event_type 无效")
        if not isinstance(event.get("stage"), str) or not event["stage"]:
            raise RagServiceError("Agentic RAG Trace 缺少 stage")
        if event.get("status") not in allowed_statuses:
            raise RagServiceError("Agentic RAG Trace 的 status 无效")
        progress = event.get("progress")
        latency_ms = event.get("latency_ms")
        if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
            raise RagServiceError("Agentic RAG Trace 的 progress 无效")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise RagServiceError("Agentic RAG Trace 的 latency_ms 无效")
        if not isinstance(event.get("details"), dict):
            raise RagServiceError("Agentic RAG Trace 的 details 无效")
        events.append(event)
    return events


def _validate_agentic_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RagServiceError("Agentic RAG 返回的数据不是 JSON 对象")

    found = payload.get("found")
    answer = payload.get("answer")
    sources = payload.get("sources")
    trace_id = payload.get("trace_id")
    meta = payload.get("meta")
    trace = _validate_trace_events(payload.get("trace", []))
    if not isinstance(found, bool):
        raise RagServiceError("Agentic RAG 响应缺少有效的 found 字段")
    if not isinstance(answer, str) or not answer.strip():
        raise RagServiceError("Agentic RAG 响应缺少有效的 answer 字段")
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise RagServiceError("Agentic RAG 响应缺少有效的 sources 字段")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise RagServiceError("Agentic RAG 响应缺少有效的 trace_id 字段")
    if not isinstance(meta, dict):
        raise RagServiceError("Agentic RAG 响应缺少有效的 meta 字段")

    integer_fields = (
        "input_tokens",
        "output_tokens",
        "cache_hit_input_tokens",
        "cache_miss_input_tokens",
        "retrieval_rounds",
        "max_retrieval_rounds",
        "latency_ms",
    )
    for field in integer_fields:
        _validate_non_negative_int(meta, field)
    if meta["retrieval_rounds"] > meta["max_retrieval_rounds"]:
        raise RagServiceError("Agentic RAG 响应中的检索轮次超过硬上限")
    if not isinstance(meta.get("cache_usage_reported"), bool):
        raise RagServiceError("Agentic RAG 响应字段 meta.cache_usage_reported 无效")
    cost = meta.get("estimated_cost_cny")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise RagServiceError("Agentic RAG 响应字段 meta.estimated_cost_cny 无效")
    if not isinstance(meta.get("pricing_model"), str):
        raise RagServiceError("Agentic RAG 响应字段 meta.pricing_model 无效")
    if not isinstance(meta.get("pricing_currency"), str):
        raise RagServiceError("Agentic RAG 响应字段 meta.pricing_currency 无效")

    return {
        "found": found,
        "answer": answer,
        "sources": sources,
        "trace_id": trace_id,
        "meta": meta,
        "trace": trace,
    }


def _validate_job_created(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RagServiceError("Agentic RAG 创建任务响应格式无效")
    job_id = payload.get("job_id")
    progress = payload.get("progress")
    if not isinstance(job_id, str) or not job_id:
        raise RagServiceError("Agentic RAG 创建任务响应缺少 job_id")
    if payload.get("status") != "queued":
        raise RagServiceError("Agentic RAG 创建任务响应状态无效")
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise RagServiceError("Agentic RAG 创建任务响应进度无效")
    return {
        "job_id": job_id,
        "status": "queued",
        "progress": progress,
        "poll_url": payload.get("poll_url", f"/api/v1/query-jobs/{job_id}"),
    }


def _validate_query_job(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RagServiceError("Agentic RAG 查询任务响应格式无效")
    job_id = payload.get("job_id")
    status = payload.get("status")
    progress = payload.get("progress")
    if not isinstance(job_id, str) or not job_id:
        raise RagServiceError("Agentic RAG 查询任务缺少 job_id")
    if status not in {"queued", "running", "completed", "failed"}:
        raise RagServiceError("Agentic RAG 查询任务状态无效")
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise RagServiceError("Agentic RAG 查询任务进度无效")
    events = _validate_trace_events(payload.get("events", []))
    result = payload.get("result")
    error = payload.get("error")
    if status == "completed":
        result = _validate_agentic_response(result)
    elif result is not None:
        raise RagServiceError("未完成的 Agentic RAG 任务不应包含结果")
    if status == "failed":
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise RagServiceError("Agentic RAG 失败任务缺少错误信息")
        error = {
            "type": str(error.get("type", "query_failed"))[:100],
            "message": error["message"][:500],
        }
    else:
        error = None
    return {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "current_stage": str(payload.get("current_stage", "")),
        "events": events,
        "result": result,
        "error": error,
    }


class BgeM3ApiClient:
    def __init__(self) -> None:
        self.api_url = os.getenv("BGE_M3_API_URL", "").rstrip("/")
        self.api_key = os.getenv("BGE_M3_API_KEY", "")
        self.model = os.getenv("BGE_M3_MODEL", "BAAI/bge-m3")

    def _endpoint(self) -> str:
        if not self.api_url or not self.api_key:
            raise RagConfigurationError("未配置 BGE_M3_API_URL 或 BGE_M3_API_KEY")
        return self.api_url if self.api_url.endswith("/embeddings") else f"{self.api_url}/v1/embeddings"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # Avoid intermittent TLS/proxy failures caused by machine-wide proxy
        # settings; the configured embedding endpoint is accessed directly.
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.post(self._endpoint(), headers=headers, json={"model": self.model, "input": texts})
            response.raise_for_status()
            data = response.json().get("data", [])
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        if len(vectors) != len(texts):
            raise RuntimeError("BGE-M3 API 返回的向量数量不匹配")
        if vectors and len(vectors[0]) != VECTOR_SIZE:
            raise RuntimeError(f"BGE-M3 向量维度为 {len(vectors[0])}，配置期望 {VECTOR_SIZE}")
        return vectors


def qdrant_client() -> QdrantClient:
    url = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
    key = os.getenv("QDRANT_API_KEY") or None
    # Local Qdrant traffic must not be routed through a Windows/system HTTP
    # proxy, otherwise httpx may return a proxy-generated 502 for localhost.
    return QdrantClient(url=url, api_key=key, timeout=10, trust_env=False)


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path} 缺少 YAML 元数据")
    _, frontmatter, body = raw.split("---", 2)
    metadata = yaml.safe_load(frontmatter) or {}
    required = {"title", "category", "source_name", "source_url", "updated_at"}
    missing = required - set(metadata)
    if missing:
        raise ValueError(f"{path} 缺少元数据: {', '.join(sorted(missing))}")
    metadata["updated_at"] = str(metadata["updated_at"])
    return metadata, body.strip()


def chunk_markdown(text: str, max_chars: int = 400, overlap: int = 80) -> list[str]:
    sections: list[str] = []
    current = ""
    for paragraph in [part.strip() for part in text.split("\n\n") if part.strip()]:
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            sections.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            sections.append(paragraph[start : start + max_chars])
            start += max_chars - overlap
        current = ""
    if current:
        sections.append(current)
    return sections


class RagService:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_url = (api_url if api_url is not None else AGENTIC_RAG_API_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else AGENTIC_RAG_API_KEY).strip()
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else AGENTIC_RAG_TIMEOUT_SECONDS
        )
        if self.timeout_seconds <= 0:
            raise RagConfigurationError("AGENTIC_RAG_TIMEOUT_SECONDS 必须大于 0")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _job_request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                if method == "POST":
                    response = await client.post(endpoint, headers=self._headers(), json=payload)
                else:
                    response = await client.get(endpoint, headers=self._headers())
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RagServiceError(
                f"Agentic RAG 请求超时（{self.timeout_seconds:g} 秒）"
            ) from exc
        except httpx.ConnectError as exc:
            raise RagServiceError(
                "无法连接 Agentic RAG 服务，请先启动 127.0.0.1:8100 服务"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                detail = "Agentic RAG 服务鉴权失败，请检查 AGENTIC_RAG_API_KEY"
            elif status == 404:
                detail = "Agentic RAG 查询任务不存在或已过期"
            else:
                detail = f"Agentic RAG 服务返回 HTTP {status}"
            raise RagServiceError(detail) from exc
        except httpx.HTTPError as exc:
            raise RagServiceError("Agentic RAG 网络请求失败") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise RagServiceError("Agentic RAG 服务返回了非法 JSON") from exc

    async def start_job(
        self,
        query: str,
        category: str,
        *,
        session_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        endpoint = f"{_service_root(self.api_url)}/api/v1/query-jobs"
        payload = await self._job_request(
            "POST",
            endpoint,
            {
                "session_id": session_id,
                "request_id": request_id or uuid.uuid4().hex,
                "query": query,
                "category": category,
            },
        )
        return _validate_job_created(payload)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            raise RagServiceError("Agentic RAG job_id 不能为空")
        endpoint = f"{_service_root(self.api_url)}/api/v1/query-jobs/{job_id}"
        payload = await self._job_request("GET", endpoint)
        return _validate_query_job(payload)

    async def answer(
        self,
        query: str,
        category: str,
        llm: Any = None,
        *,
        session_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        del llm  # Kept only for backward-compatible callers; generation is remote.
        endpoint = _query_endpoint(self.api_url)
        headers = self._headers()
        request_payload = {
            "session_id": session_id,
            "request_id": request_id or uuid.uuid4().hex,
            "query": query,
            "category": category,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                response = await client.post(endpoint, headers=headers, json=request_payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RagServiceError(
                f"Agentic RAG 查询超时（{self.timeout_seconds:g} 秒），请确认 8100 服务及下游模型服务正常"
            ) from exc
        except httpx.ConnectError as exc:
            raise RagServiceError(
                "无法连接 Agentic RAG 服务，请先启动 127.0.0.1:8100 服务"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                detail = "Agentic RAG 服务鉴权失败，请检查 AGENTIC_RAG_API_KEY"
            elif status == 404:
                detail = "Agentic RAG 查询接口不存在，请检查 AGENTIC_RAG_API_URL"
            else:
                detail = f"Agentic RAG 服务返回 HTTP {status}"
            raise RagServiceError(detail) from exc
        except httpx.HTTPError as exc:
            raise RagServiceError("Agentic RAG 网络请求失败") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise RagServiceError("Agentic RAG 服务返回了非法 JSON") from exc
        return _validate_agentic_response(payload)


def stable_point_id(file_path: str, title: str, chunk_index: int) -> str:
    digest = hashlib.sha256(f"{file_path}|{title}|{chunk_index}".encode("utf-8")).hexdigest()
    return str(__import__("uuid").UUID(digest[:32]))
