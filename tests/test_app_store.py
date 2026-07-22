import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app_store import (
    AppStore,
    InvalidCredentials,
    StoreConflict,
    StoreNotFound,
)


class AppStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "app.sqlite3"
        self.store = AppStore(self.db_path)
        self.store.initialize(bootstrap_from_env=False)
        self.user = self.store.create_user("alice", "correct horse battery staple")
        self.other = self.store.create_user("bob", "another secure password")

    def tearDown(self):
        self.temp.cleanup()

    def test_initialization_is_idempotent_and_enables_wal(self):
        self.store.initialize(bootstrap_from_env=False)
        with self.store.connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(version, 2)
        self.assertTrue(
            {
                "users",
                "auth_sessions",
                "conversations",
                "messages",
                "conversation_summaries",
                "conversation_checkpoints",
            }.issubset(tables)
        )

    def test_password_auth_and_tokens_are_not_stored_in_plaintext(self):
        authenticated = self.store.authenticate_password(
            "ALICE", "correct horse battery staple"
        )
        self.assertEqual(authenticated["user_id"], self.user["user_id"])
        with self.assertRaises(InvalidCredentials):
            self.store.authenticate_password("alice", "wrong")

        auth_session = self.store.create_auth_session(self.user["user_id"])
        raw_token = auth_session["token"]
        current_user = self.store.authenticate_token(raw_token)
        self.assertEqual(current_user["user_id"], self.user["user_id"])
        with self.store.connect() as connection:
            user_row = connection.execute(
                "SELECT password_hash, password_salt FROM users WHERE user_id = ?",
                (self.user["user_id"],),
            ).fetchone()
            token_row = connection.execute(
                "SELECT token_hash FROM auth_sessions WHERE user_id = ?",
                (self.user["user_id"],),
            ).fetchone()
        self.assertNotIn("correct horse", user_row["password_hash"])
        self.assertNotEqual(user_row["password_salt"], "correct horse battery staple")
        self.assertNotEqual(token_row["token_hash"], raw_token)

        self.assertTrue(self.store.revoke_auth_session(raw_token))
        with self.assertRaises(InvalidCredentials):
            self.store.authenticate_token(raw_token)

    def test_duplicate_username_and_bootstrap_do_not_overwrite_account(self):
        with self.assertRaises(StoreConflict):
            self.store.create_user(" ALICE ", "different password")
        admin = self.store.bootstrap_user(
            "admin", "first bootstrap password", role="admin"
        )
        same_admin = self.store.bootstrap_user(
            "ADMIN", "replacement password", role="user"
        )
        self.assertEqual(admin["user_id"], same_admin["user_id"])
        self.assertEqual(same_admin["role"], "admin")
        self.store.authenticate_password("admin", "first bootstrap password")
        with self.assertRaises(InvalidCredentials):
            self.store.authenticate_password("admin", "replacement password")

    def test_conversation_crud_is_user_isolated(self):
        conversation = self.store.create_conversation(
            self.user["user_id"], title="北京旅行"
        )
        with self.assertRaises(StoreNotFound):
            self.store.get_conversation(
                self.other["user_id"], conversation["conversation_id"]
            )
        renamed = self.store.update_conversation_title(
            self.user["user_id"], conversation["conversation_id"], "北京三日游"
        )
        self.assertEqual(renamed["title"], "北京三日游")
        archived = self.store.archive_conversation(
            self.user["user_id"], conversation["conversation_id"]
        )
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(
            self.store.list_conversations(self.user["user_id"])["items"], []
        )
        self.assertEqual(
            len(
                self.store.list_conversations(
                    self.user["user_id"], include_archived=True
                )["items"]
            ),
            1,
        )
        self.store.unarchive_conversation(
            self.user["user_id"], conversation["conversation_id"]
        )
        self.assertTrue(
            self.store.delete_conversation(
                self.user["user_id"], conversation["conversation_id"]
            )
        )

    def test_conversation_cursor_pagination_has_no_duplicates(self):
        for title in ("会话1", "会话2", "会话3", "会话4", "会话5"):
            self.store.create_conversation(self.user["user_id"], title=title)
        first = self.store.list_conversations(self.user["user_id"], limit=2)
        second = self.store.list_conversations(
            self.user["user_id"], limit=2, cursor=first["next_cursor"]
        )
        third = self.store.list_conversations(
            self.user["user_id"], limit=2, cursor=second["next_cursor"]
        )
        ids = [
            item["conversation_id"]
            for page in (first, second, third)
            for item in page["items"]
        ]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        self.assertIsNone(third["next_cursor"])

    def test_messages_are_idempotent_sequenced_paginated_and_isolated(self):
        conversation = self.store.create_conversation(self.user["user_id"])
        conversation_id = conversation["conversation_id"]
        first = self.store.append_message(
            self.user["user_id"],
            conversation_id,
            role="user",
            message_type="chat",
            content="帮我规划北京旅行",
            request_id="request-1",
            metadata={"client": "test"},
            token_estimate=10,
        )
        duplicate = self.store.append_message(
            self.user["user_id"],
            conversation_id,
            role="user",
            message_type="chat",
            content="这份文本不会覆盖原消息",
            request_id="request-1",
        )
        self.assertEqual(first["message_id"], duplicate["message_id"])
        self.assertEqual(duplicate["content"], "帮我规划北京旅行")
        for number in range(2, 7):
            self.store.append_message(
                self.user["user_id"],
                conversation_id,
                role="assistant" if number % 2 == 0 else "user",
                message_type="chat",
                content=f"消息{number}",
                request_id=f"request-{number}",
            )
        latest = self.store.list_messages(
            self.user["user_id"], conversation_id, limit=3
        )
        earlier = self.store.list_messages(
            self.user["user_id"],
            conversation_id,
            before_seq=latest["next_before_seq"],
            limit=3,
        )
        self.assertEqual([item["seq"] for item in latest["items"]], [4, 5, 6])
        self.assertEqual([item["seq"] for item in earlier["items"]], [1, 2, 3])
        with self.assertRaises(StoreNotFound):
            self.store.list_messages(
                self.other["user_id"], conversation_id, limit=50
            )

    def test_summary_cursor_is_monotonic_and_checkpoint_round_trips(self):
        conversation = self.store.create_conversation(self.user["user_id"])
        conversation_id = conversation["conversation_id"]
        for number in range(1, 4):
            self.store.append_message(
                self.user["user_id"],
                conversation_id,
                role="user" if number % 2 else "assistant",
                message_type="chat",
                content=f"消息{number}",
                request_id=f"summary-request-{number}",
                context_eligible=number != 2,
            )
        summary = self.store.upsert_summary(
            self.user["user_id"],
            conversation_id,
            summary_text="用户要规划北京行程",
            summary_through_seq=2,
            source_message_count=2,
            token_estimate=12,
        )
        self.assertEqual(summary["summary_through_seq"], 2)
        with self.assertRaises(StoreConflict):
            self.store.upsert_summary(
                self.user["user_id"],
                conversation_id,
                summary_text="旧摘要",
                summary_through_seq=1,
                source_message_count=1,
                token_estimate=5,
            )
        eligible = self.store.list_context_messages(
            self.user["user_id"], conversation_id
        )
        self.assertEqual([item["seq"] for item in eligible], [1, 3])

        checkpoint = self.store.save_checkpoint(
            self.user["user_id"],
            conversation_id,
            runtime_session_id="browser-session-1",
            runtime_state="clarifying",
            extracted={"target_city": "北京"},
            traveler_groups=["elderly"],
            pending_mixed={"original_message": "测试"},
            review_id="review-1",
            last_rag_category="student_ticket",
            rag_job_id="rag-job-1",
            current_request_id="request-current-1",
        )
        self.assertEqual(checkpoint["version"], 1)
        self.assertEqual(checkpoint["extracted"]["target_city"], "北京")
        self.assertEqual(checkpoint["runtime_session_id"], "browser-session-1")
        self.assertEqual(checkpoint["rag_job_id"], "rag-job-1")
        self.assertEqual(checkpoint["current_request_id"], "request-current-1")
        review_checkpoint = self.store.find_checkpoint_by_review_id("review-1")
        self.assertEqual(review_checkpoint["user_id"], self.user["user_id"])
        self.assertEqual(review_checkpoint["conversation_id"], conversation_id)
        runtime_checkpoint = self.store.find_checkpoint_by_runtime_session_id(
            "browser-session-1"
        )
        self.assertEqual(runtime_checkpoint["user_id"], self.user["user_id"])
        updated = self.store.save_checkpoint(
            self.user["user_id"],
            conversation_id,
            runtime_state="confirmed",
            extracted={"target_city": "北京", "days": 3},
        )
        self.assertEqual(updated["version"], 2)
        self.assertTrue(
            self.store.clear_checkpoint(self.user["user_id"], conversation_id)
        )
        self.assertIsNone(
            self.store.get_checkpoint(self.user["user_id"], conversation_id)
        )
        self.assertIsNone(self.store.find_checkpoint_by_review_id("review-1"))

    def test_v1_checkpoint_schema_is_migrated_without_data_loss(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """CREATE TABLE conversation_checkpoints (
                       conversation_id TEXT PRIMARY KEY,
                       runtime_state TEXT NOT NULL,
                       extracted_json TEXT NOT NULL DEFAULT '{}',
                       traveler_groups_json TEXT NOT NULL DEFAULT '[]',
                       pending_mixed_json TEXT,
                       review_id TEXT,
                       review_status TEXT,
                       last_rag_category TEXT,
                       version INTEGER NOT NULL DEFAULT 1,
                       updated_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO conversation_checkpoints(
                       conversation_id, runtime_state, review_id, updated_at
                   ) VALUES ('legacy-conversation', 'pending_review',
                             'legacy-review', '2026-07-21T00:00:00+00:00')"""
            )
            connection.execute("PRAGMA user_version = 1")

        legacy_store = AppStore(legacy_path)
        legacy_store.initialize(bootstrap_from_env=False)
        with legacy_store.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(conversation_checkpoints)"
                ).fetchall()
            }
            row = connection.execute(
                """SELECT runtime_state, review_id, runtime_session_id,
                          rag_job_id, current_request_id
                   FROM conversation_checkpoints
                   WHERE conversation_id = 'legacy-conversation'"""
            ).fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue(
            {"runtime_session_id", "rag_job_id", "current_request_id"}.issubset(
                columns
            )
        )
        self.assertEqual(row["runtime_state"], "pending_review")
        self.assertEqual(row["review_id"], "legacy-review")
        self.assertIsNone(row["runtime_session_id"])
        self.assertIsNone(row["rag_job_id"])
        self.assertIsNone(row["current_request_id"])
        self.assertEqual(version, 2)

    def test_deleting_user_cascades_durable_memory_records(self):
        conversation = self.store.create_conversation(self.user["user_id"])
        self.store.append_message(
            self.user["user_id"],
            conversation["conversation_id"],
            role="user",
            message_type="chat",
            content="测试",
        )
        self.assertTrue(self.store.delete_user(self.user["user_id"]))
        with self.store.connect() as connection:
            conversation_count = connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
            message_count = connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
        self.assertEqual(conversation_count, 0)
        self.assertEqual(message_count, 0)


if __name__ == "__main__":
    unittest.main()
