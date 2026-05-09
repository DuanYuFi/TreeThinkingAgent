from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"candidate", "ready", "active", "blocked", "done"}
VALID_RELATIONS = {"decomposes_to", "depends_on", "relates_to"}
DEFAULT_DB_PATH = Path(".tta-local-memory/task-status.sqlite")


@dataclass(frozen=True, slots=True)
class InquiryCandidate:
    client_id: str
    content: str
    status: str
    answer: str | None
    importance: float
    confidence: float


@dataclass(frozen=True, slots=True)
class EdgeCandidate:
    source_client_id: str
    target_client_id: str
    relation: str
    note: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_inquiry_id(content: str) -> str:
    digest = hashlib.sha1(normalize_text(content).encode("utf-8")).hexdigest()[:10]
    return f"inq_{digest}"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def load_schema() -> str:
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(load_schema())
    conn.commit()


def parse_extraction_payload(payload: dict[str, Any]) -> tuple[list[InquiryCandidate], list[EdgeCandidate]]:
    raw_inquiries = payload.get("inquiries", [])
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_inquiries, list):
        raise ValueError("`inquiries` must be a list.")
    if not isinstance(raw_edges, list):
        raise ValueError("`edges` must be a list.")

    inquiries: list[InquiryCandidate] = []
    for index, item in enumerate(raw_inquiries, start=1):
        if not isinstance(item, dict):
            continue
        content = normalize_text(str(item.get("content", "")))
        if not content:
            continue
        status = str(item.get("status", "candidate")).strip().lower()
        if status not in VALID_STATUSES:
            status = "candidate"
        answer_value = item.get("answer")
        answer = normalize_text(str(answer_value)) if answer_value not in (None, "") else None
        inquiries.append(
            InquiryCandidate(
                client_id=normalize_text(str(item.get("client_id") or f"I{index}")),
                content=content,
                status=status,
                answer=answer,
                importance=coerce_score(item.get("importance")),
                confidence=coerce_score(item.get("confidence")),
            )
        )

    edges: list[EdgeCandidate] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        relation = str(item.get("relation", "")).strip().lower()
        if relation not in VALID_RELATIONS:
            continue
        source = normalize_text(str(item.get("source_client_id", "")))
        target = normalize_text(str(item.get("target_client_id", "")))
        if not source or not target or source == target:
            continue
        note_value = item.get("note")
        edges.append(
            EdgeCandidate(
                source_client_id=source,
                target_client_id=target,
                relation=relation,
                note=normalize_text(str(note_value)) if note_value not in (None, "") else None,
            )
        )
    return inquiries, edges


def coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def upsert_inquiries(
    conn: sqlite3.Connection,
    inquiries: list[InquiryCandidate],
    edges: list[EdgeCandidate],
    *,
    conversation: str,
    source_label: str | None,
    provider: str,
    model_name: str,
    min_importance: float,
    min_confidence: float,
    max_inquiries: int,
) -> dict[str, Any]:
    now = utc_now()
    conv_hash = source_hash(conversation)
    selected = [
        item
        for item in inquiries
        if item.importance >= min_importance and item.confidence >= min_confidence
    ][:max_inquiries]
    selected_by_client_id = {item.client_id: item for item in selected}
    client_to_db_id: dict[str, str] = {}

    with conn:
        for item in selected:
            inquiry_id = stable_inquiry_id(item.content)
            client_to_db_id[item.client_id] = inquiry_id
            existing = conn.execute(
                "SELECT id, status, answer FROM inquiries WHERE id = ?",
                (inquiry_id,),
            ).fetchone()
            if existing:
                merged_status = merge_status(str(existing["status"]), item.status)
                merged_answer = item.answer or existing["answer"]
                conn.execute(
                    """
                    UPDATE inquiries
                    SET status = ?, answer = ?, importance = MAX(importance, ?),
                        confidence = MAX(confidence, ?), source_hash = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        merged_status,
                        merged_answer,
                        item.importance,
                        item.confidence,
                        conv_hash,
                        now,
                        inquiry_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO inquiries
                        (id, content, status, answer, importance, confidence, source_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inquiry_id,
                        item.content,
                        item.status,
                        item.answer,
                        item.importance,
                        item.confidence,
                        conv_hash,
                        now,
                        now,
                    ),
                )

        inserted_edges = 0
        for edge in edges:
            if (
                edge.source_client_id not in selected_by_client_id
                or edge.target_client_id not in selected_by_client_id
            ):
                continue
            source_id = client_to_db_id[edge.source_client_id]
            target_id = client_to_db_id[edge.target_client_id]
            if edge.relation in {"depends_on", "decomposes_to"} and creates_relation_cycle(
                conn, source_id, target_id, edge.relation
            ):
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO inquiry_edges (source_id, target_id, relation, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, target_id, edge.relation, edge.note, now),
            )
            inserted_edges += cursor.rowcount

        skipped_count = max(0, len(inquiries) - len(selected))
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_ingests
                (source_hash, source_label, provider, model_name, min_importance, min_confidence,
                 max_inquiries, inserted_count, skipped_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_hash,
                source_label,
                provider,
                model_name,
                min_importance,
                min_confidence,
                max_inquiries,
                len(selected),
                skipped_count,
                now,
            ),
        )

    return {
        "source_hash": conv_hash,
        "inserted_inquiries": len(selected),
        "skipped_inquiries": skipped_count,
        "inserted_edges": inserted_edges,
        "ids": [client_to_db_id[item.client_id] for item in selected],
    }


def merge_status(existing: str, incoming: str) -> str:
    if existing == incoming:
        return existing
    if incoming == "done":
        return "done"
    if existing == "done":
        return existing
    order = {"candidate": 0, "ready": 1, "blocked": 2, "active": 3}
    return incoming if order.get(incoming, 0) >= order.get(existing, 0) else existing


def creates_relation_cycle(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relation: str,
) -> bool:
    rows = conn.execute(
        """
        WITH RECURSIVE deps(node_id) AS (
            SELECT target_id FROM inquiry_edges
            WHERE source_id = ? AND relation = ?
            UNION
            SELECT e.target_id FROM inquiry_edges e
            JOIN deps d ON e.source_id = d.node_id
            WHERE e.relation = ?
        )
        SELECT 1 FROM deps WHERE node_id = ? LIMIT 1
        """,
        (target_id, relation, relation, source_id),
    ).fetchone()
    return rows is not None


def graph_snapshot(conn: sqlite3.Connection, *, conversation: str, limit: int = 40) -> str:
    keywords = extract_keywords(conversation)
    rows = select_render_rows(conn, keywords=keywords, limit=limit)
    counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM inquiries GROUP BY status"
        ).fetchall()
    }
    edges = conn.execute(
        """
        SELECT e.relation, e.note, s.id AS source_id, s.content AS source_content,
               t.id AS target_id, t.content AS target_content
        FROM inquiry_edges e
        JOIN inquiries s ON s.id = e.source_id
        JOIN inquiries t ON t.id = e.target_id
        WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})
        ORDER BY e.created_at DESC
        LIMIT 60
        """.format(placeholders=",".join("?" for _ in rows) or "NULL"),
        [row["id"] for row in rows] * 2,
    ).fetchall() if rows else []

    payload = {
        "counts": counts,
        "inquiries": [dict(row) for row in rows],
        "edges": [dict(row) for row in edges],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def select_render_rows(
    conn: sqlite3.Connection,
    *,
    keywords: list[str],
    limit: int,
) -> list[sqlite3.Row]:
    priority_rows = conn.execute(
        """
        SELECT id, content, status, answer, importance, confidence, updated_at
        FROM inquiries
        WHERE status IN ('active', 'blocked', 'ready')
        ORDER BY
            CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'ready' THEN 2 ELSE 3 END,
            importance DESC,
            updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    rows_by_id = {row["id"]: row for row in priority_rows}

    for keyword in keywords:
        if len(rows_by_id) >= limit:
            break
        matches = conn.execute(
            """
            SELECT id, content, status, answer, importance, confidence, updated_at
            FROM inquiries
            WHERE content LIKE ? OR COALESCE(answer, '') LIKE ?
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", max(1, limit - len(rows_by_id))),
        ).fetchall()
        for row in matches:
            rows_by_id.setdefault(row["id"], row)

    if len(rows_by_id) < limit:
        recent_done = conn.execute(
            """
            SELECT id, content, status, answer, importance, confidence, updated_at
            FROM inquiries
            WHERE status = 'done'
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
            """,
            (limit - len(rows_by_id),),
        ).fetchall()
        for row in recent_done:
            rows_by_id.setdefault(row["id"], row)

    return list(rows_by_id.values())[:limit]


def extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", text)
    stop = {
        "这个",
        "那个",
        "我们",
        "你可以",
        "现在",
        "一个",
        "需要",
        "任务",
        "系统",
    }
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if token in stop or token.lower() in stop:
            continue
        if token not in seen:
            keywords.append(token)
            seen.add(token)
        if len(keywords) >= 12:
            break
    return keywords
