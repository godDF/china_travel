import asyncio
import unittest
from unittest.mock import patch

from backend import main
from backend.rag import RagServiceError
from backend.safety import IntentDecision


RAG_RESULT = {
    "found": True,
    "answer": "学生票规则回答",
    "sources": [{"title": "学生票规则", "source_url": "https://example.com"}],
    "trace_id": "trace-chat-1",
    "meta": {
        "input_tokens": 2215,
        "output_tokens": 986,
        "cache_hit_input_tokens": 0,
        "cache_miss_input_tokens": 2215,
        "cache_usage_reported": True,
        "retrieval_rounds": 1,
        "max_retrieval_rounds": 2,
        "latency_ms": 12540,
        "estimated_cost_cny": 0.004187,
        "pricing_model": "deepseek-v4-flash",
        "pricing_currency": "CNY",
    },
    "trace": [{
        "event_id": "event-chat-1",
        "event_type": "agent",
        "stage": "query_planning",
        "label": "查询规划 Agent",
        "status": "completed",
        "progress": 8,
        "started_at": "2026-07-16T00:00:00",
        "completed_at": "2026-07-16T00:00:01",
        "latency_ms": 1000,
        "details": {"subquery_count": 2, "next_action": "执行向量检索"},
    }],
}


class FakeRagService:
    def __init__(self, result=None, error=None, job_status="completed"):
        self.result = result or RAG_RESULT
        self.error = error
        self.job_status = job_status
        self.calls = []

    async def start_job(self, query, category, **kwargs):
        self.calls.append({"query": query, "category": category, **kwargs})
        if self.error:
            raise self.error
        return {"job_id": "job-chat-1", "status": "queued", "progress": 2}

    async def get_job(self, job_id):
        if self.job_status == "running":
            return {
                "job_id": job_id,
                "status": "running",
                "progress": 38,
                "current_stage": "证据评估 Agent",
                "events": self.result["trace"],
                "result": None,
                "error": None,
            }
        if self.job_status == "failed":
            return {
                "job_id": job_id,
                "status": "failed",
                "progress": 38,
                "current_stage": "查询失败",
                "events": self.result["trace"],
                "result": None,
                "error": {"type": "query_timeout", "message": "Agentic RAG 查询超过 20 秒"},
            }
        return {
            "job_id": job_id,
            "status": "completed",
            "progress": 100,
            "current_stage": "查询完成",
            "events": self.result["trace"],
            "result": self.result,
            "error": None,
        }


class AgenticRagChatIntegrationTests(unittest.TestCase):
    def tearDown(self):
        main.sessions.clear()

    def test_rag_branch_starts_job_then_returns_metrics_and_resets_session(self):
        fake_rag = FakeRagService()

        async def scenario():
            session = await main.create_session()
            request = main.ChatRequest(session_id=session["session_id"], message="学生票规则")
            with patch.object(main, "Deepseek", return_value=object()):
                with patch.object(
                    main,
                    "classify_intent",
                    return_value=IntentDecision("rag_query", "student_ticket"),
                ):
                    with patch.object(main, "rag_service", fake_rag):
                        started = await main.api_chat(request)
                        completed = await main.api_get_rag_job(
                            started["job_id"], session["session_id"]
                        )
            return session, started, completed

        session, started, response = asyncio.run(scenario())
        self.assertEqual(started["type"], "rag_status")
        self.assertEqual(started["status"], "queued")
        self.assertEqual(response["type"], "rag")
        self.assertEqual(response["trace_id"], "trace-chat-1")
        self.assertEqual(response["rag_meta"]["input_tokens"], 2215)
        self.assertEqual(response["rag_meta"]["max_retrieval_rounds"], 2)
        self.assertEqual(response["trace"][0]["stage"], "query_planning")
        self.assertTrue(response["reset"])
        self.assertEqual(session["state"], "init")
        self.assertEqual(session["messages"], [])
        self.assertEqual(fake_rag.calls[0]["query"], "学生票规则")
        self.assertEqual(fake_rag.calls[0]["session_id"], session["session_id"])
        self.assertTrue(fake_rag.calls[0]["request_id"])

    def test_mixed_choice_queries_the_original_user_question(self):
        fake_rag = FakeRagService()
        original = "帮我规划北京三日游，顺便说明学生票规则"

        async def scenario():
            session = await main.create_session()
            session["state"] = "awaiting_intent_choice"
            session["pending_mixed_query"] = original
            session["pending_mixed_category"] = "student_ticket"
            request = main.ChatRequest(session_id=session["session_id"], message="规则查询")
            with patch.object(main, "Deepseek", return_value=object()):
                with patch.object(main, "rag_service", fake_rag):
                    response = await main.api_chat(request)
            return response

        response = asyncio.run(scenario())
        self.assertEqual(response["type"], "rag_status")
        self.assertEqual(fake_rag.calls[0]["query"], original)

    def test_remote_failure_stays_in_rag_branch_and_resets(self):
        fake_rag = FakeRagService(error=RagServiceError("无法连接 Agentic RAG 服务"))

        async def scenario():
            session = await main.create_session()
            request = main.ChatRequest(session_id=session["session_id"], message="学生票规则")
            with patch.object(main, "Deepseek", return_value=object()):
                with patch.object(
                    main,
                    "classify_intent",
                    return_value=IntentDecision("rag_query", "student_ticket"),
                ):
                    with patch.object(main, "rag_service", fake_rag):
                        response = await main.api_chat(request)
            return session, response

        session, response = asyncio.run(scenario())
        self.assertEqual(response["type"], "error")
        self.assertIn("无法连接 Agentic RAG 服务", response["message"])
        self.assertTrue(response["reset"])
        self.assertEqual(session["state"], "init")

    def test_running_job_exposes_live_trace_without_reset(self):
        fake_rag = FakeRagService(job_status="running")

        async def scenario():
            session = await main.create_session()
            session["state"] = "rag_querying"
            session["rag_job_id"] = "job-chat-1"
            with patch.object(main, "rag_service", fake_rag):
                response = await main.api_get_rag_job("job-chat-1", session["session_id"])
            return session, response

        session, response = asyncio.run(scenario())
        self.assertEqual(response["type"], "rag_status")
        self.assertEqual(response["current_stage"], "证据评估 Agent")
        self.assertEqual(response["events"][0]["label"], "查询规划 Agent")
        self.assertEqual(session["state"], "rag_querying")


if __name__ == "__main__":
    unittest.main()
