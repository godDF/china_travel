"""SQLite persistence and local authentication for ChinaTravel.

This module deliberately has no FastAPI dependency.  It owns durable user,
authentication, conversation, message, summary, and runtime-checkpoint data;
HTTP cookie handling and request/response schemas stay in ``backend.main``.

The store opens one SQLite connection per operation so it is safe to use from
FastAPI worker threads.  WAL mode and a busy timeout keep the local demo
responsive when a background planning task and a browser request write at the
same time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path(
    os.getenv("APP_DB_PATH", str(PROJECT_ROOT / "data" / "app.sqlite3"))
)
PASSWORD_ITERATIONS = 310_000
SCHEMA_VERSION = 2
VALID_ROLES = frozenset({"user", "admin"})
VALID_MESSAGE_ROLES = frozenset({"user", "assistant", "system"})


class AppStoreError(RuntimeError):
    """Base class for application-store failures."""


class StoreConflict(AppStoreError):
    """The requested write conflicts with an existing durable record."""


class StoreNotFound(AppStoreError):
    """The requested record does not exist or is not owned by the caller."""


class InvalidCredentials(AppStoreError):
    """Authentication failed without revealing which credential was wrong."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or _utc_now()).isoformat(timespec="microseconds")


def _normalize_username(username: str) -> str:
    return " ".join(str(username or "").strip().split()).casefold()


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def hash_password(
    password: str,
    *,
    salt: Optional[bytes] = None,
    iterations: int = PASSWORD_ITERATIONS,
) -> tuple[str, str, int]:
    """Return ``(hash, salt, iterations)`` for a PBKDF2-SHA256 password."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must not be empty")
    if iterations < 100_000:
        raise ValueError("PBKDF2 iterations must be at least 100000")
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, iterations
    )
    return _encode_bytes(digest), _encode_bytes(actual_salt), iterations


def verify_password(
    password: str,
    expected_hash: str,
    salt: str,
    iterations: int,
) -> bool:
    """Verify a password without leaking timing information."""
    if not isinstance(password, str):
        return False
    try:
        digest, _, _ = hash_password(
            password, salt=_decode_bytes(salt), iterations=int(iterations)
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest, expected_hash)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _encode_cursor(updated_at: str, conversation_id: str) -> str:
    raw = _json_dump([updated_at, conversation_id]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return str(value[0]), str(value[1])
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid conversation cursor") from exc


class AppStore:
    """Durable application data and local authentication repository."""

    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        *,
        busy_timeout_ms: int = 5000,
        auth_session_days: Optional[int] = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        configured_days = auth_session_days
        if configured_days is None:
            configured_days = int(os.getenv("AUTH_SESSION_DAYS", "7"))
        self.auth_session_days = max(1, int(configured_days))

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def initialize(self, *, bootstrap_from_env: bool = True) -> None:
        """Create schema idempotently and optionally seed configured users."""
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'admin')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    auth_session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    last_message_seq INTEGER NOT NULL DEFAULT 0
                        CHECK(last_message_seq >= 0)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    seq INTEGER NOT NULL CHECK(seq > 0),
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    intent TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    context_eligible INTEGER NOT NULL DEFAULT 1
                        CHECK(context_eligible IN (0, 1)),
                    token_estimate INTEGER NOT NULL DEFAULT 0
                        CHECK(token_estimate >= 0),
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, seq),
                    UNIQUE(conversation_id, request_id, role, message_type)
                );

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY
                        REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    summary_text TEXT NOT NULL,
                    summary_through_seq INTEGER NOT NULL
                        CHECK(summary_through_seq >= 0),
                    source_message_count INTEGER NOT NULL
                        CHECK(source_message_count >= 0),
                    token_estimate INTEGER NOT NULL DEFAULT 0
                        CHECK(token_estimate >= 0),
                    summary_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_checkpoints (
                    conversation_id TEXT PRIMARY KEY
                        REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    runtime_session_id TEXT,
                    runtime_state TEXT NOT NULL,
                    extracted_json TEXT NOT NULL DEFAULT '{}',
                    traveler_groups_json TEXT NOT NULL DEFAULT '[]',
                    pending_mixed_json TEXT,
                    review_id TEXT,
                    review_status TEXT,
                    last_rag_category TEXT,
                    rag_job_id TEXT,
                    current_request_id TEXT,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                    ON auth_sessions(user_id, expires_at);
                CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                    ON conversations(user_id, updated_at DESC, conversation_id DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq
                    ON messages(conversation_id, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_context
                    ON messages(conversation_id, context_eligible, seq);
                """
            )
            # ``CREATE TABLE IF NOT EXISTS`` does not add columns to an existing
            # v1 database.  Keep migrations small and deterministic so users can
            # upgrade their local SQLite file without deleting conversation
            # history.
            checkpoint_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(conversation_checkpoints)"
                ).fetchall()
            }
            migrations = {
                "runtime_session_id": "TEXT",
                "rag_job_id": "TEXT",
                "current_request_id": "TEXT",
            }
            for column_name, column_type in migrations.items():
                if column_name not in checkpoint_columns:
                    connection.execute(
                        f"ALTER TABLE conversation_checkpoints "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoints_runtime_session
                   ON conversation_checkpoints(runtime_session_id)
                   WHERE runtime_session_id IS NOT NULL"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_checkpoints_review
                   ON conversation_checkpoints(review_id)
                   WHERE review_id IS NOT NULL"""
            )
            connection.execute(
                """INSERT INTO app_schema_metadata(key, value) VALUES('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if bootstrap_from_env:
            self.bootstrap_from_environment()

    # ---- users and authentication -------------------------------------------------

    @staticmethod
    def _decode_user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_user_row(self, connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise StoreNotFound("user not found")
        return row

    def create_user(self, username: str, password: str, *, role: str = "user") -> dict[str, Any]:
        clean_username = " ".join(str(username or "").strip().split())
        normalized = _normalize_username(clean_username)
        if not normalized:
            raise ValueError("username must not be empty")
        if len(clean_username) > 80:
            raise ValueError("username is too long")
        if role not in VALID_ROLES:
            raise ValueError("role must be user or admin")
        password_hash, salt, iterations = hash_password(password)
        now = _timestamp()
        user_id = uuid.uuid4().hex
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute(
                    """INSERT INTO users(
                           user_id, username, username_normalized, password_hash,
                           password_salt, password_iterations, role, is_active,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        user_id,
                        clean_username,
                        normalized,
                        password_hash,
                        salt,
                        iterations,
                        role,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreConflict("username already exists") from exc
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = self._get_user_row(connection, user_id)
        return self._decode_user(row)

    def get_user_by_username(self, username: str) -> dict[str, Any]:
        normalized = _normalize_username(username)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_normalized = ?", (normalized,)
            ).fetchone()
        if not row:
            raise StoreNotFound("user not found")
        return self._decode_user(row)

    def authenticate_password(self, username: str, password: str) -> dict[str, Any]:
        normalized = _normalize_username(username)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_normalized = ?", (normalized,)
            ).fetchone()
        if not row or not bool(row["is_active"]):
            raise InvalidCredentials("invalid username or password")
        if not verify_password(
            password,
            row["password_hash"],
            row["password_salt"],
            row["password_iterations"],
        ):
            raise InvalidCredentials("invalid username or password")
        return self._decode_user(row)

    def update_user(
        self,
        user_id: str,
        *,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> dict[str, Any]:
        if role is not None and role not in VALID_ROLES:
            raise ValueError("role must be user or admin")
        with closing(self.connect()) as connection, connection:
            self._get_user_row(connection, user_id)
            fields: list[str] = []
            values: list[Any] = []
            if role is not None:
                fields.append("role = ?")
                values.append(role)
            if is_active is not None:
                fields.append("is_active = ?")
                values.append(1 if is_active else 0)
            if fields:
                fields.append("updated_at = ?")
                values.append(_timestamp())
                values.append(user_id)
                connection.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values
                )
                if is_active is False:
                    connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id = ?", (user_id,)
                    )
        return self.get_user(user_id)

    def change_password(self, user_id: str, new_password: str) -> None:
        password_hash, salt, iterations = hash_password(new_password)
        with closing(self.connect()) as connection, connection:
            self._get_user_row(connection, user_id)
            connection.execute(
                """UPDATE users
                   SET password_hash = ?, password_salt = ?, password_iterations = ?,
                       updated_at = ?
                   WHERE user_id = ?""",
                (password_hash, salt, iterations, _timestamp(), user_id),
            )
            connection.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    def delete_user(self, user_id: str) -> bool:
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return cursor.rowcount == 1

    def bootstrap_user(self, username: str, password: str, *, role: str) -> dict[str, Any]:
        """Create a seed user once; never overwrite an existing account."""
        try:
            return self.get_user_by_username(username)
        except StoreNotFound:
            try:
                return self.create_user(username, password, role=role)
            except StoreConflict:
                # A concurrent process may have inserted it after our read.
                return self.get_user_by_username(username)

    def bootstrap_from_environment(self) -> list[dict[str, Any]]:
        created_or_existing: list[dict[str, Any]] = []
        pairs = (
            (
                os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin").strip(),
                os.getenv("BOOTSTRAP_ADMIN_PASSWORD", ""),
                "admin",
            ),
            (
                os.getenv("BOOTSTRAP_USER_USERNAME", "demo").strip(),
                os.getenv("BOOTSTRAP_USER_PASSWORD", ""),
                "user",
            ),
        )
        for username, password, role in pairs:
            if username and password:
                created_or_existing.append(
                    self.bootstrap_user(username, password, role=role)
                )
        return created_or_existing

    def create_auth_session(
        self, user_id: str, *, session_days: Optional[int] = None
    ) -> dict[str, Any]:
        days = self.auth_session_days if session_days is None else max(1, int(session_days))
        token = secrets.token_urlsafe(32)
        auth_session_id = uuid.uuid4().hex
        now_dt = _utc_now()
        now = _timestamp(now_dt)
        expires_at = _timestamp(now_dt + timedelta(days=days))
        with closing(self.connect()) as connection, connection:
            user = self._get_user_row(connection, user_id)
            if not bool(user["is_active"]):
                raise InvalidCredentials("account is inactive")
            connection.execute(
                """INSERT INTO auth_sessions(
                       auth_session_id, user_id, token_hash, expires_at,
                       created_at, last_seen_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (auth_session_id, user_id, _token_hash(token), expires_at, now, now),
            )
        return {
            "auth_session_id": auth_session_id,
            "token": token,
            "expires_at": expires_at,
            "user": self.get_user(user_id),
        }

    def authenticate_token(self, token: str, *, touch: bool = True) -> dict[str, Any]:
        if not token:
            raise InvalidCredentials("invalid or expired session")
        now = _timestamp()
        with closing(self.connect()) as connection, connection:
            row = connection.execute(
                """SELECT u.*, a.auth_session_id, a.expires_at
                   FROM auth_sessions AS a
                   JOIN users AS u ON u.user_id = a.user_id
                   WHERE a.token_hash = ?""",
                (_token_hash(token),),
            ).fetchone()
            if not row or not bool(row["is_active"]) or row["expires_at"] <= now:
                if row:
                    connection.execute(
                        "DELETE FROM auth_sessions WHERE auth_session_id = ?",
                        (row["auth_session_id"],),
                    )
                raise InvalidCredentials("invalid or expired session")
            if touch:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at = ? WHERE auth_session_id = ?",
                    (now, row["auth_session_id"]),
                )
        result = self._decode_user(row)
        result["auth_session_id"] = row["auth_session_id"]
        result["expires_at"] = row["expires_at"]
        return result

    def revoke_auth_session(self, token: str) -> bool:
        if not token:
            return False
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),)
            )
        return cursor.rowcount == 1

    def revoke_all_auth_sessions(self, user_id: str) -> int:
        with closing(self.connect()) as connection, connection:
            self._get_user_row(connection, user_id)
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE user_id = ?", (user_id,)
            )
        return cursor.rowcount

    def purge_expired_auth_sessions(self) -> int:
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (_timestamp(),)
            )
        return cursor.rowcount

    # ---- conversations ------------------------------------------------------------

    @staticmethod
    def _decode_conversation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
            "last_message_seq": int(row["last_message_seq"]),
        }

    def _require_conversation(
        self, connection: sqlite3.Connection, user_id: str, conversation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM conversations
               WHERE conversation_id = ? AND user_id = ?""",
            (conversation_id, user_id),
        ).fetchone()
        if not row:
            # Deliberately identical for a missing and a foreign conversation.
            raise StoreNotFound("conversation not found")
        return row

    def create_conversation(self, user_id: str, *, title: str = "新对话") -> dict[str, Any]:
        clean_title = str(title or "新对话").strip()[:120] or "新对话"
        now = _timestamp()
        conversation_id = uuid.uuid4().hex
        with closing(self.connect()) as connection, connection:
            self._get_user_row(connection, user_id)
            connection.execute(
                """INSERT INTO conversations(
                       conversation_id, user_id, title, created_at, updated_at,
                       archived_at, last_message_seq
                   ) VALUES (?, ?, ?, ?, ?, NULL, 0)""",
                (conversation_id, user_id, clean_title, now, now),
            )
        return self.get_conversation(user_id, conversation_id)

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = self._require_conversation(connection, user_id, conversation_id)
        return self._decode_conversation(row)

    def list_conversations(
        self,
        user_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 20,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        actual_limit = min(100, max(1, int(limit)))
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if cursor:
            cursor_time, cursor_id = _decode_cursor(cursor)
            clauses.append(
                "(updated_at < ? OR (updated_at = ? AND conversation_id < ?))"
            )
            params.extend([cursor_time, cursor_time, cursor_id])
        params.append(actual_limit + 1)
        query = f"""SELECT * FROM conversations
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, conversation_id DESC
                    LIMIT ?"""
        with closing(self.connect()) as connection:
            self._get_user_row(connection, user_id)
            rows = connection.execute(query, params).fetchall()
        has_more = len(rows) > actual_limit
        selected = rows[:actual_limit]
        items = [self._decode_conversation(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = _encode_cursor(last["updated_at"], last["conversation_id"])
        return {"items": items, "next_cursor": next_cursor}

    def update_conversation_title(
        self, user_id: str, conversation_id: str, title: str
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()[:120]
        if not clean_title:
            raise ValueError("conversation title must not be empty")
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            connection.execute(
                """UPDATE conversations SET title = ?, updated_at = ?
                   WHERE conversation_id = ?""",
                (clean_title, _timestamp(), conversation_id),
            )
        return self.get_conversation(user_id, conversation_id)

    def archive_conversation(
        self, user_id: str, conversation_id: str
    ) -> dict[str, Any]:
        now = _timestamp()
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            connection.execute(
                """UPDATE conversations
                   SET archived_at = COALESCE(archived_at, ?), updated_at = ?
                   WHERE conversation_id = ?""",
                (now, now, conversation_id),
            )
        return self.get_conversation(user_id, conversation_id)

    def unarchive_conversation(
        self, user_id: str, conversation_id: str
    ) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            connection.execute(
                """UPDATE conversations
                   SET archived_at = NULL, updated_at = ? WHERE conversation_id = ?""",
                (_timestamp(), conversation_id),
            )
        return self.get_conversation(user_id, conversation_id)

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            cursor = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )
        return cursor.rowcount == 1

    # ---- messages -----------------------------------------------------------------

    @staticmethod
    def _decode_message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "request_id": row["request_id"],
            "seq": int(row["seq"]),
            "role": row["role"],
            "type": row["message_type"],
            "content": row["content"],
            "intent": row["intent"],
            "metadata": _json_load(row["metadata_json"], {}),
            "context_eligible": bool(row["context_eligible"]),
            "token_estimate": int(row["token_estimate"]),
            "created_at": row["created_at"],
        }

    def append_message(
        self,
        user_id: str,
        conversation_id: str,
        *,
        role: str,
        message_type: str,
        content: str,
        request_id: Optional[str] = None,
        intent: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        context_eligible: bool = True,
        token_estimate: int = 0,
    ) -> dict[str, Any]:
        if role not in VALID_MESSAGE_ROLES:
            raise ValueError("invalid message role")
        clean_type = str(message_type or "").strip()
        if not clean_type:
            raise ValueError("message_type must not be empty")
        if token_estimate < 0:
            raise ValueError("token_estimate must not be negative")
        actual_request_id = str(request_id or uuid.uuid4().hex)
        metadata_json = _json_dump(dict(metadata or {}))
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._require_conversation(
                connection, user_id, conversation_id
            )
            existing = connection.execute(
                """SELECT * FROM messages
                   WHERE conversation_id = ? AND request_id = ?
                         AND role = ? AND message_type = ?""",
                (conversation_id, actual_request_id, role, clean_type),
            ).fetchone()
            if existing:
                connection.commit()
                return self._decode_message(existing)

            seq = int(conversation["last_message_seq"]) + 1
            now = _timestamp()
            message_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO messages(
                       message_id, conversation_id, request_id, seq, role,
                       message_type, content, intent, metadata_json,
                       context_eligible, token_estimate, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    conversation_id,
                    actual_request_id,
                    seq,
                    role,
                    clean_type,
                    str(content),
                    intent,
                    metadata_json,
                    1 if context_eligible else 0,
                    int(token_estimate),
                    now,
                ),
            )
            connection.execute(
                """UPDATE conversations
                   SET last_message_seq = ?, updated_at = ?
                   WHERE conversation_id = ?""",
                (seq, now, conversation_id),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            connection.commit()
            return self._decode_message(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_message(
        self, user_id: str, conversation_id: str, message_id: str
    ) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            self._require_conversation(connection, user_id, conversation_id)
            row = connection.execute(
                """SELECT * FROM messages
                   WHERE message_id = ? AND conversation_id = ?""",
                (message_id, conversation_id),
            ).fetchone()
        if not row:
            raise StoreNotFound("message not found")
        return self._decode_message(row)

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        before_seq: Optional[int] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        actual_limit = min(200, max(1, int(limit)))
        clauses = ["conversation_id = ?"]
        params: list[Any] = [conversation_id]
        if before_seq is not None:
            clauses.append("seq < ?")
            params.append(int(before_seq))
        params.append(actual_limit + 1)
        with closing(self.connect()) as connection:
            self._require_conversation(connection, user_id, conversation_id)
            rows = connection.execute(
                f"""SELECT * FROM messages WHERE {' AND '.join(clauses)}
                    ORDER BY seq DESC LIMIT ?""",
                params,
            ).fetchall()
        has_more = len(rows) > actual_limit
        selected_desc = rows[:actual_limit]
        items = [self._decode_message(row) for row in reversed(selected_desc)]
        next_before_seq = None
        if has_more and selected_desc:
            next_before_seq = int(selected_desc[-1]["seq"])
        return {"items": items, "next_before_seq": next_before_seq}

    def list_context_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        after_seq: int = 0,
        through_seq: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["conversation_id = ?", "context_eligible = 1", "seq > ?"]
        params: list[Any] = [conversation_id, int(after_seq)]
        if through_seq is not None:
            clauses.append("seq <= ?")
            params.append(int(through_seq))
        with closing(self.connect()) as connection:
            self._require_conversation(connection, user_id, conversation_id)
            rows = connection.execute(
                f"""SELECT * FROM messages WHERE {' AND '.join(clauses)}
                    ORDER BY seq ASC""",
                params,
            ).fetchall()
        return [self._decode_message(row) for row in rows]

    # ---- summaries ----------------------------------------------------------------

    @staticmethod
    def _decode_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "summary_text": row["summary_text"],
            "summary_through_seq": int(row["summary_through_seq"]),
            "source_message_count": int(row["source_message_count"]),
            "token_estimate": int(row["token_estimate"]),
            "summary_version": row["summary_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_summary(
        self, user_id: str, conversation_id: str
    ) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            self._require_conversation(connection, user_id, conversation_id)
            row = connection.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return self._decode_summary(row) if row else None

    def upsert_summary(
        self,
        user_id: str,
        conversation_id: str,
        *,
        summary_text: str,
        summary_through_seq: int,
        source_message_count: int,
        token_estimate: int,
        summary_version: str = "v1",
    ) -> dict[str, Any]:
        if summary_through_seq < 0 or source_message_count < 0 or token_estimate < 0:
            raise ValueError("summary counters must not be negative")
        now = _timestamp()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._require_conversation(
                connection, user_id, conversation_id
            )
            if summary_through_seq > int(conversation["last_message_seq"]):
                raise ValueError("summary cannot cover messages that do not exist")
            existing = connection.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if existing and summary_through_seq < int(existing["summary_through_seq"]):
                raise StoreConflict("summary cursor cannot move backwards")
            if existing:
                connection.execute(
                    """UPDATE conversation_summaries
                       SET summary_text = ?, summary_through_seq = ?,
                           source_message_count = ?, token_estimate = ?,
                           summary_version = ?, updated_at = ?
                       WHERE conversation_id = ?""",
                    (
                        str(summary_text),
                        int(summary_through_seq),
                        int(source_message_count),
                        int(token_estimate),
                        str(summary_version),
                        now,
                        conversation_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO conversation_summaries(
                           conversation_id, summary_text, summary_through_seq,
                           source_message_count, token_estimate, summary_version,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        str(summary_text),
                        int(summary_through_seq),
                        int(source_message_count),
                        int(token_estimate),
                        str(summary_version),
                        now,
                        now,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_summary(user_id, conversation_id)
        assert result is not None
        return result

    # ---- runtime checkpoints -------------------------------------------------------

    @staticmethod
    def _decode_checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "runtime_session_id": row["runtime_session_id"],
            "runtime_state": row["runtime_state"],
            "extracted": _json_load(row["extracted_json"], {}),
            "traveler_groups": _json_load(row["traveler_groups_json"], []),
            "pending_mixed": _json_load(row["pending_mixed_json"], None),
            "review_id": row["review_id"],
            "review_status": row["review_status"],
            "last_rag_category": row["last_rag_category"],
            "rag_job_id": row["rag_job_id"],
            "current_request_id": row["current_request_id"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
        }

    def save_checkpoint(
        self,
        user_id: str,
        conversation_id: str,
        *,
        runtime_session_id: Optional[str] = None,
        runtime_state: str,
        extracted: Optional[Mapping[str, Any]] = None,
        traveler_groups: Optional[list[str]] = None,
        pending_mixed: Any = None,
        review_id: Optional[str] = None,
        review_status: Optional[str] = None,
        last_rag_category: Optional[str] = None,
        rag_job_id: Optional[str] = None,
        current_request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = _timestamp()
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            connection.execute(
                """INSERT INTO conversation_checkpoints(
                       conversation_id, runtime_session_id, runtime_state, extracted_json,
                       traveler_groups_json, pending_mixed_json, review_id,
                       review_status, last_rag_category, rag_job_id,
                       current_request_id, version, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       runtime_session_id = excluded.runtime_session_id,
                       runtime_state = excluded.runtime_state,
                       extracted_json = excluded.extracted_json,
                       traveler_groups_json = excluded.traveler_groups_json,
                       pending_mixed_json = excluded.pending_mixed_json,
                       review_id = excluded.review_id,
                       review_status = excluded.review_status,
                       last_rag_category = excluded.last_rag_category,
                       rag_job_id = excluded.rag_job_id,
                       current_request_id = excluded.current_request_id,
                       version = conversation_checkpoints.version + 1,
                       updated_at = excluded.updated_at""",
                (
                    conversation_id,
                    runtime_session_id,
                    str(runtime_state),
                    _json_dump(dict(extracted or {})),
                    _json_dump(list(traveler_groups or [])),
                    _json_dump(pending_mixed) if pending_mixed is not None else None,
                    review_id,
                    review_status,
                    last_rag_category,
                    rag_job_id,
                    current_request_id,
                    now,
                ),
            )
        result = self.get_checkpoint(user_id, conversation_id)
        assert result is not None
        return result

    def get_checkpoint(
        self, user_id: str, conversation_id: str
    ) -> Optional[dict[str, Any]]:
        with closing(self.connect()) as connection:
            self._require_conversation(connection, user_id, conversation_id)
            row = connection.execute(
                "SELECT * FROM conversation_checkpoints WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return self._decode_checkpoint(row) if row else None

    def find_checkpoint_by_review_id(self, review_id: str) -> Optional[dict[str, Any]]:
        """Resolve an asynchronous review callback to its durable owner.

        This method is intentionally internal/trusted: unlike user-facing reads,
        it does not accept a caller-provided user id.  The returned record always
        includes the owner copied from the joined conversation row.
        """
        if not review_id:
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT cp.*, c.user_id
                   FROM conversation_checkpoints AS cp
                   JOIN conversations AS c
                     ON c.conversation_id = cp.conversation_id
                   WHERE cp.review_id = ?
                   ORDER BY cp.updated_at DESC
                   LIMIT 1""",
                (review_id,),
            ).fetchone()
        if not row:
            return None
        result = self._decode_checkpoint(row)
        result["user_id"] = row["user_id"]
        return result

    def find_checkpoint_by_runtime_session_id(
        self, runtime_session_id: str
    ) -> Optional[dict[str, Any]]:
        """Resolve an old browser session after a process restart."""
        if not runtime_session_id:
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT cp.*, c.user_id
                   FROM conversation_checkpoints AS cp
                   JOIN conversations AS c
                     ON c.conversation_id = cp.conversation_id
                   WHERE cp.runtime_session_id = ?
                   LIMIT 1""",
                (runtime_session_id,),
            ).fetchone()
        if not row:
            return None
        result = self._decode_checkpoint(row)
        result["user_id"] = row["user_id"]
        return result

    def clear_checkpoint(self, user_id: str, conversation_id: str) -> bool:
        with closing(self.connect()) as connection, connection:
            self._require_conversation(connection, user_id, conversation_id)
            cursor = connection.execute(
                "DELETE FROM conversation_checkpoints WHERE conversation_id = ?",
                (conversation_id,),
            )
        return cursor.rowcount == 1
