"""Minimal BGE-M3 API + Qdrant RAG implementation."""

from __future__ import annotations

import hashlib
import os
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


class RagConfigurationError(RuntimeError):
    pass


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
    def __init__(self) -> None:
        self.embedding = BgeM3ApiClient()

    async def search(self, query: str, category: str) -> list[dict[str, Any]]:
        vector = (await self.embedding.embed([query]))[0]
        response = qdrant_client().query_points(
            collection_name=COLLECTION,
            query=vector,
            query_filter=models.Filter(
                must=[models.FieldCondition(key="category", match=models.MatchValue(value=category))]
            ),
            limit=TOP_K,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD,
        )
        return [
            {"score": point.score, **(point.payload or {})}
            for point in response.points
        ]

    async def answer(self, query: str, category: str, llm: Any) -> dict[str, Any]:
        hits = await self.search(query, category)
        if not hits:
            return {
                "found": False,
                "answer": "知识库中暂未查询到可靠信息，请换一种描述，或重新选择规则查询/旅行方案制定。",
                "sources": [],
            }
        context = "\n\n".join(
            f"[{index + 1}] {hit['content']}\n来源：{hit.get('source_name')} {hit.get('source_url')}"
            for index, hit in enumerate(hits)
        )
        prompt = f"""你是旅行规则知识助手。只能依据以下检索片段回答问题，不得使用片段之外的知识或编造规则。
如果片段不足以回答，就明确说知识库信息不足。回答简洁，并在相关句子后标注 [1]、[2] 等来源编号。

用户问题：{query}

检索片段：
{context}

回答末尾提醒：具体规则可能变化，请以最新官方规定为准。"""
        answer = llm([{"role": "user", "content": prompt}], one_line=False, json_mode=False)
        sources = []
        seen = set()
        for hit in hits:
            key = (hit.get("title"), hit.get("source_url"))
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "title": hit.get("title"),
                "source_name": hit.get("source_name"),
                "source_url": hit.get("source_url"),
                "updated_at": hit.get("updated_at"),
                "score": round(float(hit.get("score", 0)), 4),
            })
        return {"found": True, "answer": answer, "sources": sources}


def stable_point_id(file_path: str, title: str, chunk_index: int) -> str:
    digest = hashlib.sha256(f"{file_path}|{title}|{chunk_index}".encode("utf-8")).hexdigest()
    return str(__import__("uuid").UUID(digest[:32]))
