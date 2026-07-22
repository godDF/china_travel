import unittest
import uuid
from types import SimpleNamespace

from backend.memory import (
    ContextBuilder,
    ContextConfig,
    ConversationSummary,
    LongTermMemory,
    MandatoryContextTooLarge,
    MemoryMessage,
    QdrantLongTermMemoryStore,
    complete_incremental_summary,
    estimate_text_tokens,
    plan_incremental_summary,
    recent_turn_window,
    stable_memory_id,
)


def conversation(turns=8, text="内容"):
    messages = []
    seq = 1
    for index in range(1, turns + 1):
        messages.append(MemoryMessage(seq=seq, role="user", content=f"问题{index}{text}"))
        seq += 1
        messages.append(
            MemoryMessage(seq=seq, role="assistant", content=f"回答{index}{text}")
        )
        seq += 1
    return messages


class TokenEstimatorTests(unittest.TestCase):
    def test_estimates_chinese_and_ascii_without_native_tokenizer(self):
        self.assertEqual(estimate_text_tokens("中国旅行"), 4)
        self.assertEqual(estimate_text_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_text_tokens("中国abcdefgh"), 4)
        self.assertEqual(estimate_text_tokens(""), 0)


class SlidingWindowTests(unittest.TestCase):
    def test_keeps_last_six_complete_user_turns(self):
        window = recent_turn_window(conversation(8), recent_turns=6)
        self.assertEqual([message.seq for message in window], list(range(5, 17)))
        self.assertEqual(window[0].role, "user")
        self.assertEqual(window[-1].role, "assistant")

    def test_context_ineligible_messages_do_not_enter_window(self):
        messages = conversation(2)
        messages.insert(
            2,
            MemoryMessage(
                seq=100,
                role="assistant",
                content="内部工具原始结果",
                context_eligible=False,
            ),
        )
        window = recent_turn_window(messages)
        self.assertNotIn(100, [message.seq for message in window])


class IncrementalSummaryTests(unittest.TestCase):
    def test_summary_cursor_only_selects_new_old_messages(self):
        previous = ConversationSummary(
            summary_text="旧摘要",
            summary_through_seq=2,
            source_message_count=2,
            token_estimate=3,
        )
        batch = plan_incremental_summary(conversation(8), previous)
        self.assertIsNotNone(batch)
        self.assertEqual([message.seq for message in batch.messages], [3, 4])
        self.assertEqual(batch.start_seq, 3)
        self.assertEqual(batch.end_seq, 4)
        self.assertIn("已有摘要", batch.source_text())

        completed = complete_incremental_summary(batch, "旧摘要与新增事实的合并摘要")
        self.assertEqual(completed.summary_through_seq, 4)
        self.assertEqual(completed.source_message_count, 4)

        next_batch = plan_incremental_summary(conversation(8), completed)
        self.assertIsNone(next_batch)

    def test_six_or_fewer_turns_need_no_summary(self):
        self.assertIsNone(plan_incremental_summary(conversation(6)))


class ContextBuilderTests(unittest.TestCase):
    def test_order_is_system_memory_summary_recent_history_current(self):
        memory = LongTermMemory(
            memory_id=str(uuid.uuid4()),
            user_id="user-1",
            normalized_text="用户通常从深圳出发",
            score=0.9,
        )
        summary = ConversationSummary(
            summary_text="用户之前咨询过北京行程",
            summary_through_seq=4,
            source_message_count=4,
            token_estimate=12,
        )
        history = [
            MemoryMessage(seq=5, role="user", content="继续讨论行程"),
            MemoryMessage(seq=6, role="assistant", content="可以继续补充"),
            MemoryMessage(seq=7, role="user", content="我从深圳出发"),
            MemoryMessage(seq=8, role="assistant", content="已记录出发地"),
        ]
        result = ContextBuilder().build(
            system_instruction="你是旅行助手",
            current_question="规划北京三日游",
            history=history,
            summary=summary,
            long_term_memories=[memory],
        )

        self.assertEqual(result.messages[0]["role"], "system")
        self.assertIn("相关长期记忆", result.messages[1]["content"])
        self.assertIn("历史对话摘要", result.messages[2]["content"])
        self.assertEqual(result.messages[-1]["content"], "规划北京三日游")
        self.assertEqual(result.included_memory_ids, (memory.memory_id,))
        self.assertEqual(result.included_history_seqs, (5, 6, 7, 8))

    def test_budget_trims_soft_components_but_preserves_mandatory_blocks(self):
        memories = [
            LongTermMemory(
                memory_id=str(uuid.uuid4()),
                user_id="user-1",
                normalized_text="长期事实" * 20,
                score=0.9 - index * 0.1,
            )
            for index in range(4)
        ]
        config = ContextConfig(
            token_budget=150,
            recent_turns=6,
            minimum_recent_turns=2,
            long_term_top_k=4,
            long_term_token_budget=100,
            summary_token_budget=60,
        )
        result = ContextBuilder(config).build(
            system_instruction="系统指令不可裁剪",
            current_question="当前问题不可裁剪",
            history=conversation(6, text="较长的历史内容" * 4),
            summary=ConversationSummary(summary_text="摘要内容" * 30),
            long_term_memories=memories,
        )

        self.assertLessEqual(result.estimated_tokens, 150)
        self.assertEqual(result.messages[0]["content"], "系统指令不可裁剪")
        self.assertEqual(result.messages[-1]["content"], "当前问题不可裁剪")
        self.assertTrue(result.dropped_memory_ids or result.dropped_history_seqs)

    def test_rejects_when_mandatory_content_alone_is_too_large(self):
        builder = ContextBuilder(ContextConfig(token_budget=10))
        with self.assertRaises(MandatoryContextTooLarge):
            builder.build(
                system_instruction="系统指令非常长" * 5,
                current_question="当前问题也非常长" * 5,
            )

    def test_does_not_duplicate_current_question_from_history(self):
        history = [MemoryMessage(seq=1, role="user", content="同一个当前问题")]
        result = ContextBuilder().build(
            system_instruction="系统",
            current_question="同一个当前问题",
            history=history,
        )
        self.assertEqual(
            [message["content"] for message in result.messages].count("同一个当前问题"),
            1,
        )


class FakeEmbedder:
    def __init__(self, failure=None):
        self.failure = failure

    async def embed_one(self, text):
        if self.failure:
            raise self.failure
        return [0.1, 0.2, 0.3]


class FakeQdrant:
    def __init__(self, points=None):
        self.points = points or []
        self.query_kwargs = None
        self.upsert_kwargs = None
        self.scroll_kwargs = None

    def query_points(self, **kwargs):
        self.query_kwargs = kwargs
        return SimpleNamespace(points=self.points)

    def upsert(self, **kwargs):
        self.upsert_kwargs = kwargs
        return SimpleNamespace(status="completed")

    def scroll(self, **kwargs):
        self.scroll_kwargs = kwargs
        return self.points, None


class LongTermMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_is_always_filtered_by_authenticated_user(self):
        memory_id = str(uuid.uuid4())
        point = SimpleNamespace(
            id=memory_id,
            score=0.88,
            payload={
                "memory_id": memory_id,
                "user_id": "user-1",
                "normalized_text": "用户通常从深圳出发",
                "memory_type": "profile_fact",
                "confidence": 0.9,
                "status": "active",
            },
        )
        qdrant = FakeQdrant([point])
        store = QdrantLongTermMemoryStore(
            client=qdrant,
            embedder=FakeEmbedder(),
            collection_name="memory-test",
            vector_size=3,
        )

        result = await store.retrieve(user_id="user-1", query="从哪里出发")
        self.assertFalse(result.degraded)
        self.assertEqual(len(result.memories), 1)
        query_filter = qdrant.query_kwargs["query_filter"]
        conditions = {condition.key: condition.match.value for condition in query_filter.must}
        self.assertEqual(conditions["user_id"], "user-1")
        self.assertEqual(conditions["status"], "active")
        self.assertEqual(qdrant.query_kwargs["using"], "dense")

    async def test_retrieval_failure_degrades_to_empty_result(self):
        store = QdrantLongTermMemoryStore(
            client=FakeQdrant(),
            embedder=FakeEmbedder(RuntimeError("embedding service unavailable")),
            collection_name="memory-test",
            vector_size=3,
        )
        result = await store.retrieve(user_id="user-1", query="从哪里出发")
        self.assertTrue(result.degraded)
        self.assertEqual(result.memories, ())
        self.assertIn("embedding service unavailable", result.error)

    async def test_upsert_uses_named_dense_vector_and_stable_id(self):
        qdrant = FakeQdrant()
        store = QdrantLongTermMemoryStore(
            client=qdrant,
            embedder=FakeEmbedder(),
            collection_name="memory-test",
            vector_size=3,
        )
        memory_id = stable_memory_id(
            "user-1", "profile_fact", "usual_start_city", "深圳"
        )
        memory = LongTermMemory(
            memory_id=memory_id,
            user_id="user-1",
            normalized_text="用户通常从深圳出发",
            canonical_key="usual_start_city",
            canonical_value="深圳",
        )
        result = await store.upsert(memory)

        self.assertTrue(result.success)
        point = qdrant.upsert_kwargs["points"][0]
        self.assertEqual(point.id, memory_id)
        self.assertEqual(point.vector, {"dense": [0.1, 0.2, 0.3]})
        self.assertEqual(point.payload["user_id"], "user-1")

    async def test_list_is_filtered_and_does_not_request_vectors(self):
        memory_id = str(uuid.uuid4())
        qdrant = FakeQdrant([
            SimpleNamespace(
                id=memory_id,
                payload={
                    "memory_id": memory_id,
                    "user_id": "user-1",
                    "normalized_text": "用户明确要求记住：常从深圳出发",
                    "memory_type": "explicit_memory",
                    "confidence": 1.0,
                    "status": "active",
                    "updated_at": "2026-07-21T00:00:00+00:00",
                },
            ),
            SimpleNamespace(
                id=str(uuid.uuid4()),
                payload={
                    "user_id": "user-2",
                    "normalized_text": "其他用户信息",
                    "memory_type": "explicit_memory",
                    "status": "active",
                },
            ),
        ])
        store = QdrantLongTermMemoryStore(
            client=qdrant,
            embedder=FakeEmbedder(),
            collection_name="memory-test",
            vector_size=3,
        )

        result = await store.list_for_user(user_id="user-1")

        self.assertFalse(result.degraded)
        self.assertEqual([item.memory_id for item in result.memories], [memory_id])
        self.assertFalse(qdrant.scroll_kwargs["with_vectors"])
        conditions = {
            condition.key: condition.match.value
            for condition in qdrant.scroll_kwargs["scroll_filter"].must
        }
        self.assertEqual(conditions["user_id"], "user-1")


if __name__ == "__main__":
    unittest.main()
