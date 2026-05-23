from __future__ import annotations

import math
import sqlite3
import struct
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

from llm_client import load_dotenv_if_available, repo_root
from store import IMPORTANCE_ORDER_SQL, source_hash, utc_now


DEFAULT_EMBEDDING_PROVIDER = "huiyan_openai_claude"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str = DEFAULT_EMBEDDING_PROVIDER
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int | None = None

    @property
    def dimension_request(self) -> int:
        return int(self.dimensions or 0)

    @property
    def cache_model_key(self) -> str:
        return f"{self.provider}/{self.model}"


class EmbeddingUnavailableError(RuntimeError):
    pass


def ensure_embedding_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_embeddings (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            dimension_request INTEGER NOT NULL DEFAULT 0,
            vector_dimensions INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, model, dimension_request)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_embeddings_model
        ON task_embeddings(model, dimension_request)
        """
    )
    migrate_legacy_embeddings(conn)


def migrate_legacy_embeddings(conn: sqlite3.Connection) -> None:
    has_legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inquiry_embeddings'"
    ).fetchone()
    if not has_legacy:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO task_embeddings
            (task_id, model, dimension_request, vector_dimensions, content_hash,
             embedding, created_at, updated_at)
        SELECT inquiry_id, model, dimension_request, vector_dimensions, content_hash,
               embedding, created_at, updated_at
        FROM inquiry_embeddings
        """
    )


def embedding_config_from_env(
    *,
    provider: str = DEFAULT_EMBEDDING_PROVIDER,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int | None = None,
) -> EmbeddingConfig:
    load_dotenv_if_available()
    return EmbeddingConfig(
        provider=provider,
        model=model,
        dimensions=dimensions,
    )


def refresh_embeddings(
    conn: sqlite3.Connection,
    *,
    config: EmbeddingConfig,
    force: bool = False,
    batch_size: int = 64,
) -> dict[str, int]:
    ensure_embedding_schema(conn)
    rows = conn.execute(
        f"""
        SELECT id, name, description, notes
        FROM tasks
        ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
        """
    ).fetchall()
    stale = [
        row
        for row in rows
        if force or is_embedding_stale(conn, row, config=config)
    ]
    embedded = 0
    for index in range(0, len(stale), batch_size):
        batch = stale[index : index + batch_size]
        texts = [embedding_text(row) for row in batch]
        vectors = embed_texts(texts, config=config)
        now = utc_now()
        with conn:
            for row, vector, text in zip(batch, vectors, texts, strict=True):
                conn.execute(
                    """
                    INSERT INTO task_embeddings
                        (task_id, model, dimension_request, vector_dimensions,
                         content_hash, embedding, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, model, dimension_request)
                    DO UPDATE SET
                        vector_dimensions = excluded.vector_dimensions,
                        content_hash = excluded.content_hash,
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["id"],
                        config.cache_model_key,
                        config.dimension_request,
                        len(vector),
                        source_hash(text),
                        pack_vector(vector),
                        now,
                        now,
                    ),
                )
                embedded += 1
    return {"total_nodes": len(rows), "stale_nodes": len(stale), "embedded_nodes": embedded}


def is_embedding_stale(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    config: EmbeddingConfig,
) -> bool:
    text_hash = source_hash(embedding_text(row))
    existing = conn.execute(
        """
        SELECT content_hash
        FROM task_embeddings
        WHERE task_id = ? AND model = ? AND dimension_request = ?
        """,
        (row["id"], config.cache_model_key, config.dimension_request),
    ).fetchone()
    return existing is None or existing["content_hash"] != text_hash


def load_embedding_index(
    conn: sqlite3.Connection,
    *,
    config: EmbeddingConfig,
) -> dict[str, list[float]]:
    ensure_embedding_schema(conn)
    rows = conn.execute(
        """
        SELECT task_id, vector_dimensions, embedding
        FROM task_embeddings
        WHERE model = ? AND dimension_request = ?
        """,
        (config.cache_model_key, config.dimension_request),
    ).fetchall()
    return {
        row["task_id"]: unpack_vector(row["embedding"], row["vector_dimensions"])
        for row in rows
    }


def embed_query(query: str, *, config: EmbeddingConfig) -> list[float]:
    return embed_texts([query], config=config)[0]


def embed_texts(texts: list[str], *, config: EmbeddingConfig) -> list[list[float]]:
    if not texts:
        return []

    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        from Infrastructure.llm_infrastructure import LLMInfrastructure, ModelRegistry

        registry = ModelRegistry(str(root / "Infrastructure" / "model_registry.json"))
        infra = LLMInfrastructure(registry)
        return infra.embed_texts(
            provider=config.provider,
            model_name=config.model,
            texts=texts,
            dimensions=config.dimensions,
            timeout_seconds=120,
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise EmbeddingUnavailableError(
            f"Embedding request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise EmbeddingUnavailableError(f"Embedding request failed: {exc}") from exc
    except (KeyError, ValueError, OSError) as exc:
        raise EmbeddingUnavailableError(str(exc)) from exc


def embedding_text(row: sqlite3.Row) -> str:
    parts = [
        f"name: {row['name']}",
        f"description: {row['description']}",
    ]
    if row["notes"]:
        parts.append(f"notes: {row['notes']}")
    return "\n".join(parts)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes, dimensions: int) -> list[float]:
    return list(struct.unpack(f"<{dimensions}f", blob))
