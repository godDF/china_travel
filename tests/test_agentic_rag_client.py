import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.rag import RagService, RagServiceError


def response_payload(*, found=True):
    return {
        "found": found,
        "answer": "学生票规则回答" if found else "知识库中暂未查询到可靠信息。",
        "sources": [
            {
                "document_id": "doc-1",
                "title": "学生票规则",
                "source_name": "中国铁路",
                "source_url": "https://example.com/student",
                "updated_at": "2026-07-16",
                "score": 0.86,
            }
        ] if found else [],
        "trace_id": "trace-123",
        "meta": {
            "retrieval_rounds": 1,
            "max_retrieval_rounds": 2,
            "retrieved_chunks": 4,
            "accepted_chunks": 2,
            "rewritten": False,
            "verified": found,
            "latency_ms": 12540,
            "input_tokens": 2215,
            "output_tokens": 986,
            "cache_hit_input_tokens": 0,
            "cache_miss_input_tokens": 2215,
            "cache_usage_reported": True,
            "estimated_cost_cny": 0.004187,
            "cost_configured": True,
            "pricing_model": "deepseek-v4-flash",
            "pricing_currency": "CNY",
        },
        "trace": [],
    }


def trace_event(*, status="completed"):
    return {
        "event_id": "event-1",
        "event_type": "tool",
        "stage": "retrieval",
        "label": "第1轮向量检索",
        "status": status,
        "progress": 20,
        "started_at": "2026-07-16T00:00:00",
        "completed_at": None if status == "running" else "2026-07-16T00:00:01",
        "latency_ms": 0 if status == "running" else 1000,
        "details": {"round": 1, "retrieved_chunks": 4, "top_score": 0.72},
    }


def query_job_payload(*, status="running"):
    return {
        "job_id": "job-123",
        "status": status,
        "progress": 20 if status == "running" else 100,
        "current_stage": "第1轮向量检索" if status == "running" else "查询完成",
        "events": [trace_event(status="running" if status == "running" else "completed")],
        "result": response_payload() if status == "completed" else None,
        "error": {"type": "query_timeout", "message": "Agentic RAG 查询超过 20 秒"}
        if status == "failed" else None,
    }


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, invalid_json=False):
        self.payload = payload
        self.status_code = status_code
        self.invalid_json = invalid_json
        self.request = httpx.Request("POST", "http://agentic.test/api/v1/query")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("remote failure", request=self.request, response=response)

    def json(self):
        if self.invalid_json:
            raise json.JSONDecodeError("invalid", "x", 0)
        return self.payload


class FakeAsyncClient:
    response = FakeResponse(response_payload())
    error = None
    last_init = None
    last_url = None
    last_headers = None
    last_json = None

    def __init__(self, **kwargs):
        type(self).last_init = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, headers, json):
        type(self).last_url = url
        type(self).last_headers = headers
        type(self).last_json = json
        if type(self).error:
            raise type(self).error
        return type(self).response

    async def get(self, url, *, headers):
        type(self).last_url = url
        type(self).last_headers = headers
        type(self).last_json = None
        if type(self).error:
            raise type(self).error
        return type(self).response


class AgenticRagClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.response = FakeResponse(response_payload())
        FakeAsyncClient.error = None
        FakeAsyncClient.last_init = None
        FakeAsyncClient.last_url = None
        FakeAsyncClient.last_headers = None
        FakeAsyncClient.last_json = None

    async def test_remote_response_and_metrics_are_passed_through(self):
        service = RagService(api_url="http://agentic.test", timeout_seconds=7)
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            result = await service.answer(
                "学生票规则",
                "student_ticket",
                session_id="session-1",
                request_id="request-1",
            )

        self.assertEqual(FakeAsyncClient.last_url, "http://agentic.test/api/v1/query")
        self.assertEqual(FakeAsyncClient.last_init["timeout"], 7)
        self.assertFalse(FakeAsyncClient.last_init["trust_env"])
        self.assertNotIn("Authorization", FakeAsyncClient.last_headers)
        self.assertEqual(FakeAsyncClient.last_json, {
            "session_id": "session-1",
            "request_id": "request-1",
            "query": "学生票规则",
            "category": "student_ticket",
        })
        self.assertTrue(result["found"])
        self.assertEqual(result["trace_id"], "trace-123")
        self.assertEqual(result["meta"]["input_tokens"], 2215)
        self.assertEqual(result["meta"]["estimated_cost_cny"], 0.004187)

    async def test_optional_api_key_adds_bearer_header(self):
        service = RagService(api_url="http://agentic.test/api/v1/query", api_key="demo-key")
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            await service.answer("学生票", "student_ticket", session_id="s1")
        self.assertEqual(FakeAsyncClient.last_headers["Authorization"], "Bearer demo-key")

    async def test_start_and_poll_query_job(self):
        service = RagService(api_url="http://agentic.test")
        FakeAsyncClient.response = FakeResponse({
            "job_id": "job-123",
            "status": "queued",
            "progress": 2,
            "poll_url": "/api/v1/query-jobs/job-123",
        })
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            created = await service.start_job(
                "学生票规则",
                "student_ticket",
                session_id="session-1",
                request_id="request-1",
            )
        self.assertEqual(created["job_id"], "job-123")
        self.assertTrue(FakeAsyncClient.last_url.endswith("/api/v1/query-jobs"))
        self.assertEqual(FakeAsyncClient.last_json["request_id"], "request-1")

        FakeAsyncClient.response = FakeResponse(query_job_payload(status="running"))
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            running = await service.get_job("job-123")
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["events"][0]["event_type"], "tool")

        FakeAsyncClient.response = FakeResponse(query_job_payload(status="completed"))
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            completed = await service.get_job("job-123")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["meta"]["input_tokens"], 2215)

    async def test_not_found_still_returns_query_metrics(self):
        FakeAsyncClient.response = FakeResponse(response_payload(found=False))
        service = RagService(api_url="http://agentic.test")
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            result = await service.answer("未知规则", "student_ticket", session_id="s2")
        self.assertFalse(result["found"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["meta"]["retrieval_rounds"], 1)

    async def test_timeout_has_browser_safe_error(self):
        request = httpx.Request("POST", "http://agentic.test/api/v1/query")
        FakeAsyncClient.error = httpx.ReadTimeout("secret upstream detail", request=request)
        service = RagService(api_url="http://agentic.test", timeout_seconds=3)
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            with self.assertRaises(RagServiceError) as caught:
                await service.answer("学生票", "student_ticket", session_id="s3")
        self.assertIn("查询超时", str(caught.exception))
        self.assertNotIn("secret upstream detail", str(caught.exception))

    async def test_http_error_does_not_expose_remote_body(self):
        FakeAsyncClient.response = FakeResponse(status_code=503)
        service = RagService(api_url="http://agentic.test")
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(RagServiceError, "HTTP 503"):
                await service.answer("学生票", "student_ticket", session_id="s4")

    async def test_invalid_json_and_missing_fields_are_rejected(self):
        service = RagService(api_url="http://agentic.test")
        FakeAsyncClient.response = FakeResponse(invalid_json=True)
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(RagServiceError, "非法 JSON"):
                await service.answer("学生票", "student_ticket", session_id="s5")

        invalid = response_payload()
        del invalid["meta"]["max_retrieval_rounds"]
        FakeAsyncClient.response = FakeResponse(invalid)
        with patch("backend.rag.httpx.AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(RagServiceError, "max_retrieval_rounds"):
                await service.answer("学生票", "student_ticket", session_id="s6")


class RagMetricsFrontendContractTests(unittest.TestCase):
    def test_rag_branch_renders_all_metric_cards(self):
        html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("beginRagPolling(resp)", html)
        self.assertIn("function renderRagTraceEvents", html)
        self.assertIn("function pollRagJob", html)
        self.assertIn("resp.trace || []", html)
        for label in ("输入 / 输出 Token", "检索轮次", "总耗时", "DeepSeek 估算成本"):
            self.assertIn(label, html)
        for label in (
            "将复杂问题拆分为",
            "最高相似度",
            "证据不足，缺少",
            "更具体的检索问题",
            "仅使用通过评估的证据生成回答",
            "校验结果：通过，可以发布",
        ):
            self.assertIn(label, html)
        self.assertIn("按缓存未命中保守估算", html)
        self.assertIn("max_retrieval_rounds", html)


if __name__ == "__main__":
    unittest.main()
