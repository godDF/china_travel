#!/usr/bin/env python
"""Chunk Markdown files, call a remote BGE-M3 API, and index into Qdrant."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import models

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from backend.rag import (  # noqa: E402
    BgeM3ApiClient,
    COLLECTION,
    KB_ROOT,
    VECTOR_SIZE,
    chunk_markdown,
    parse_markdown,
    qdrant_client,
    stable_point_id,
)


async def index_knowledge_base(recreate: bool = False) -> None:
    files = sorted(KB_ROOT.rglob("*.md"))
    if not files:
        raise RuntimeError(f"{KB_ROOT} 下没有 Markdown 文档")

    chunks: list[tuple[str, dict]] = []
    failures: list[str] = []
    for path in files:
        try:
            metadata, body = parse_markdown(path)
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for index, content in enumerate(chunk_markdown(body)):
                payload = {
                    **metadata,
                    "content": content,
                    "file_path": relative,
                    "chunk_index": index,
                }
                chunks.append((stable_point_id(relative, metadata["title"], index), payload))
        except Exception as exc:
            failures.append(f"{path}: {exc}")

    if failures:
        raise RuntimeError("文档解析失败:\n" + "\n".join(failures))

    embedder = BgeM3ApiClient()
    client = qdrant_client()
    exists = client.collection_exists(COLLECTION)
    if recreate and exists:
        client.delete_collection(COLLECTION)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
        )
    client.create_payload_index(COLLECTION, "category", models.PayloadSchemaType.KEYWORD)

    batch_size = 32
    indexed = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = await embedder.embed([payload["content"] for _, payload in batch])
        client.upsert(
            collection_name=COLLECTION,
            points=[
                models.PointStruct(id=point_id, vector=vector, payload=payload)
                for (point_id, payload), vector in zip(batch, vectors)
            ],
        )
        indexed += len(batch)
        print(f"Indexed {indexed}/{len(chunks)} chunks")

    print(f"Done: {len(files)} Markdown files, {indexed} chunks, 0 failures")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true", help="Drop and recreate the Qdrant collection")
    args = parser.parse_args()
    asyncio.run(index_knowledge_base(recreate=args.recreate))


if __name__ == "__main__":
    main()
