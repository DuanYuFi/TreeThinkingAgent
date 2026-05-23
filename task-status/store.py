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
VALID_IMPORTANCE = {"low", "medium", "high"}
IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}
IMPORTANCE_ORDER_SQL = "CASE importance WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END"
MAX_DESCRIPTION_CHARS = 50
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / ".gta-local-memory/task-status.sqlite"


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    client_id: str
    name: str
    description: str
    status: str
    notes: str | None
    importance: str
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


def stable_task_id(name: str) -> str:
    digest = hashlib.sha1(normalize_text(name).encode("utf-8")).hexdigest()[:10]
    return f"tsk_{digest}"


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
    migrate_existing_schema(conn)
    ensure_embedding_schema_if_available(conn)
    ensure_project_root(conn)
    create_root_uniqueness_constraint(conn)
    conn.commit()


def migrate_existing_schema(conn: sqlite3.Connection) -> None:
    migrate_legacy_inquiries(conn)
    migrate_conversation_ingests(conn)
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "project_id" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'")
    if "is_root" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN is_root INTEGER NOT NULL DEFAULT 0")
    if "description" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
    backfill_missing_descriptions(conn)


def migrate_legacy_inquiries(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "inquiries"):
        return

    legacy_columns = table_columns(conn, "inquiries")
    if "content" not in legacy_columns:
        return
    project_expr = "project_id" if "project_id" in legacy_columns else "'default'"
    root_expr = "is_root" if "is_root" in legacy_columns else "0"
    description_expr = "description" if "description" in legacy_columns else "''"
    notes_expr = "answer" if "answer" in legacy_columns else "NULL"
    confidence_expr = "confidence" if "confidence" in legacy_columns else "0.0"
    source_hash_expr = "source_hash" if "source_hash" in legacy_columns else "NULL"
    created_expr = "created_at" if "created_at" in legacy_columns else "datetime('now')"
    updated_expr = "updated_at" if "updated_at" in legacy_columns else "datetime('now')"
    importance_expr = (
        """
        CASE
            WHEN typeof(importance) IN ('integer', 'real') AND importance >= 0.8 THEN 'high'
            WHEN typeof(importance) IN ('integer', 'real') AND importance < 0.35 THEN 'low'
            WHEN lower(CAST(importance AS TEXT)) IN ('high', 'medium', 'low') THEN lower(CAST(importance AS TEXT))
            WHEN CAST(importance AS TEXT) = '高' THEN 'high'
            WHEN CAST(importance AS TEXT) = '低' THEN 'low'
            ELSE 'medium'
        END
        """
        if "importance" in legacy_columns
        else "'medium'"
    )

    conn.execute(
        f"""
        INSERT OR IGNORE INTO tasks
            (id, project_id, is_root, name, description, status, notes, importance,
             confidence, source_hash, created_at, updated_at)
        SELECT id, {project_expr}, {root_expr}, content, {description_expr}, status,
               {notes_expr}, {importance_expr}, {confidence_expr}, {source_hash_expr},
               {created_expr}, {updated_expr}
        FROM inquiries
        """
    )

    if table_exists(conn, "inquiry_edges"):
        conn.execute(
            """
            INSERT OR IGNORE INTO task_edges (id, source_id, target_id, relation, note, created_at)
            SELECT id, source_id, target_id, relation, note, created_at
            FROM inquiry_edges
            """
        )


def migrate_conversation_ingests(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "conversation_ingests"):
        return
    columns = table_columns(conn, "conversation_ingests")
    if "max_inquiries" in columns:
        conn.execute("ALTER TABLE conversation_ingests RENAME TO conversation_ingests_legacy")
        conn.execute(
            """
            CREATE TABLE conversation_ingests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_hash TEXT NOT NULL UNIQUE,
                source_label TEXT,
                provider TEXT,
                model_name TEXT,
                min_importance TEXT NOT NULL DEFAULT 'medium',
                min_confidence REAL NOT NULL,
                max_tasks INTEGER NOT NULL,
                inserted_count INTEGER NOT NULL,
                skipped_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_ingests
                (id, source_hash, source_label, provider, model_name, min_importance,
                 min_confidence, max_tasks, inserted_count, skipped_count, created_at)
            SELECT id, source_hash, source_label, provider, model_name,
                   CASE
                       WHEN typeof(min_importance) IN ('integer', 'real') AND min_importance >= 0.8 THEN 'high'
                       WHEN typeof(min_importance) IN ('integer', 'real') AND min_importance < 0.35 THEN 'low'
                       ELSE 'medium'
                   END,
                   min_confidence, max_inquiries, inserted_count, skipped_count, created_at
            FROM conversation_ingests_legacy
            """
        )
        conn.execute("DROP TABLE conversation_ingests_legacy")
        return
    if "max_tasks" not in columns:
        conn.execute("ALTER TABLE conversation_ingests ADD COLUMN max_tasks INTEGER NOT NULL DEFAULT 8")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({name})").fetchall()
    }


def ensure_embedding_schema_if_available(conn: sqlite3.Connection) -> None:
    try:
        from embeddings import ensure_embedding_schema
    except ImportError:
        return
    ensure_embedding_schema(conn)


def create_root_uniqueness_constraint(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_root_per_project
        ON tasks(project_id)
        WHERE is_root = 1
        """
    )


def ensure_project_root(conn: sqlite3.Connection, *, project_id: str = "default") -> None:
    root_rows = conn.execute(
        f"""
        SELECT id FROM tasks
        WHERE project_id = ? AND is_root = 1
        ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
        """,
        (project_id,),
    ).fetchall()
    if len(root_rows) > 1:
        keep_id = root_rows[0]["id"]
        conn.execute(
            "UPDATE tasks SET is_root = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE project_id = ?",
            (keep_id, project_id),
        )
        return
    if len(root_rows) == 1:
        return

    inferred = conn.execute(
        f"""
        SELECT id FROM tasks
        WHERE project_id = ?
          AND id NOT IN (
              SELECT target_id FROM task_edges WHERE relation = 'decomposes_to'
          )
        ORDER BY
            CASE status
                WHEN 'active' THEN 0
                WHEN 'blocked' THEN 1
                WHEN 'ready' THEN 2
                WHEN 'done' THEN 3
                ELSE 4
            END,
            {IMPORTANCE_ORDER_SQL} DESC,
            created_at ASC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if inferred:
        conn.execute(
            "UPDATE tasks SET is_root = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE project_id = ?",
            (inferred["id"], project_id),
        )


def backfill_missing_descriptions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, name, notes
        FROM tasks
        WHERE TRIM(COALESCE(description, '')) = ''
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE tasks SET description = ? WHERE id = ?",
            (
                concise_description(
                    None,
                    name=str(row["name"]),
                    notes=row["notes"],
                ),
                row["id"],
            ),
        )


def parse_extraction_payload(payload: dict[str, Any]) -> tuple[list[TaskCandidate], list[EdgeCandidate]]:
    raw_tasks = payload.get("tasks", payload.get("inquiries", []))
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_tasks, list):
        raise ValueError("`tasks` must be a list.")
    if not isinstance(raw_edges, list):
        raise ValueError("`edges` must be a list.")

    tasks: list[TaskCandidate] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            continue
        name = normalize_text(str(item.get("name") or item.get("content") or ""))
        if not name:
            continue
        status = str(item.get("status", "candidate")).strip().lower()
        if status not in VALID_STATUSES:
            status = "candidate"
        notes_value = item.get("notes", item.get("answer"))
        notes = normalize_text(str(notes_value)) if notes_value not in (None, "") else None
        description = concise_description(
            item.get("description"),
            name=name,
            notes=notes,
        )
        tasks.append(
            TaskCandidate(
                client_id=normalize_text(str(item.get("client_id") or f"I{index}")),
                name=name,
                description=description,
                status=status,
                notes=notes,
                importance=coerce_importance(item.get("importance")),
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
    return tasks, edges


def concise_description(value: Any, *, name: str, notes: str | None) -> str:
    description = normalize_text(str(value)) if value not in (None, "") else ""
    if not description:
        description = derive_description(name=name, notes=notes)
    return trim_to_char_limit(description, MAX_DESCRIPTION_CHARS)


def derive_description(*, name: str, notes: str | None) -> str:
    if notes:
        return notes

    text = normalize_text(name).strip()
    stripped = text.rstrip("。.!！?？")
    patterns = [
        (r"^定义(.+?)的职责$", "说明{}负责什么、边界在哪里、产出是什么。"),
        (r"^设计(.+)$", "明确{}的目标、结构和验收方式。"),
        (r"^验证(.+)$", "确认{}是否可用，并记录结果。"),
        (r"^决定(.+)$", "比较选项后确定{}的取舍。"),
    ]
    for pattern, template in patterns:
        match = re.match(pattern, stripped)
        if match:
            return template.format(match.group(1))

    if stripped:
        return stripped
    return "说明这个节点要解决的问题和产出。"


def trim_to_char_limit(text: str, max_chars: int) -> str:
    normalized = normalize_text(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def coerce_importance(value: Any) -> str:
    if isinstance(value, (int, float)):
        return importance_from_score(float(value))

    text = normalize_text(str(value or "")).lower()
    if text in VALID_IMPORTANCE:
        return text
    if text in {"低", "低优先级", "low priority"}:
        return "low"
    if text in {"高", "高优先级", "high priority"}:
        return "high"
    if text in {"中", "中优先级", "medium priority", "mid", "normal"}:
        return "medium"
    try:
        return importance_from_score(float(text))
    except ValueError:
        return "medium"


def importance_from_score(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score < 0.35:
        return "low"
    return "medium"


def importance_meets(value: str, minimum: str) -> bool:
    return IMPORTANCE_RANK[coerce_importance(value)] >= IMPORTANCE_RANK[coerce_importance(minimum)]


def merge_importance(existing: str, incoming: str) -> str:
    return max(
        (coerce_importance(existing), coerce_importance(incoming)),
        key=lambda value: IMPORTANCE_RANK[value],
    )


def upsert_tasks(
    conn: sqlite3.Connection,
    tasks: list[TaskCandidate],
    edges: list[EdgeCandidate],
    *,
    conversation: str,
    source_label: str | None,
    provider: str,
    model_name: str,
    min_importance: str,
    min_confidence: float,
    max_tasks: int,
    dedupe_threshold: float = 0.72,
) -> dict[str, Any]:
    now = utc_now()
    conv_hash = source_hash(conversation)
    selected = [
        item
        for item in tasks
        if importance_meets(item.importance, min_importance) and item.confidence >= min_confidence
    ][:max_tasks]
    selected_by_client_id = {item.client_id: item for item in selected}
    client_to_db_id: dict[str, str] = {}
    inserted_count = 0
    updated_count = 0
    duplicate_count = 0
    duplicates: list[dict[str, Any]] = []

    with conn:
        for item in selected:
            task_id = stable_task_id(item.name)
            existing = conn.execute(
                "SELECT id, status, notes, description, importance FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if existing:
                client_to_db_id[item.client_id] = str(existing["id"])
                merged_status = merge_status(str(existing["status"]), item.status)
                merged_notes = item.notes or existing["notes"]
                merged_importance = merge_importance(str(existing["importance"]), item.importance)
                merged_description = (
                    existing["description"]
                    or item.description
                    or concise_description(None, name=item.name, notes=merged_notes)
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, notes = ?, description = ?, importance = ?,
                        confidence = MAX(confidence, ?), source_hash = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        merged_status,
                        merged_notes,
                        trim_to_char_limit(str(merged_description), MAX_DESCRIPTION_CHARS),
                        merged_importance,
                        item.confidence,
                        conv_hash,
                        now,
                        task_id,
                    ),
                )
                updated_count += 1
            else:
                related = find_related_task(conn, item.name, threshold=dedupe_threshold)
                if related:
                    related_id = str(related["id"])
                    client_to_db_id[item.client_id] = related_id
                    duplicate_count += 1
                    duplicates.append(
                        {
                            "client_id": item.client_id,
                            "name": item.name,
                            "matched_id": related_id,
                            "matched_name": related["name"],
                            "similarity": related["similarity"],
                        }
                    )
                    continue

                client_to_db_id[item.client_id] = task_id
                conn.execute(
                    """
                    INSERT INTO tasks
                        (id, name, description, status, notes, importance, confidence, source_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        item.name,
                        item.description,
                        item.status,
                        item.notes,
                        item.importance,
                        item.confidence,
                        conv_hash,
                        now,
                        now,
                    ),
                )
                inserted_count += 1

        inserted_edges = 0
        for edge in edges:
            if (
                edge.source_client_id not in selected_by_client_id
                or edge.target_client_id not in selected_by_client_id
            ):
                continue
            source_id = client_to_db_id[edge.source_client_id]
            target_id = client_to_db_id[edge.target_client_id]
            if source_id == target_id:
                continue
            if edge.relation in {"depends_on", "decomposes_to"} and creates_relation_cycle(
                conn, source_id, target_id, edge.relation
            ):
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO task_edges (source_id, target_id, relation, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, target_id, edge.relation, edge.note, now),
            )
            inserted_edges += cursor.rowcount

        skipped_count = max(0, len(tasks) - len(selected))
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_ingests
                (source_hash, source_label, provider, model_name, min_importance, min_confidence,
                 max_tasks, inserted_count, skipped_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_hash,
                source_label,
                provider,
                model_name,
                min_importance,
                min_confidence,
                max_tasks,
                inserted_count,
                skipped_count,
                now,
            ),
        )

    return {
        "source_hash": conv_hash,
        "inserted_tasks": inserted_count,
        "updated_tasks": updated_count,
        "duplicate_tasks": duplicate_count,
        "skipped_tasks": skipped_count,
        "inserted_edges": inserted_edges,
        "ids": [client_to_db_id[item.client_id] for item in selected],
        "duplicates": duplicates,
    }


def preview_task_changes(
    conn: sqlite3.Connection,
    tasks: list[TaskCandidate],
    edges: list[EdgeCandidate],
    *,
    conversation: str,
    min_importance: str,
    min_confidence: float,
    max_tasks: int,
    dedupe_threshold: float = 0.72,
) -> dict[str, Any]:
    conv_hash = source_hash(conversation)
    thresholded: list[TaskCandidate] = []
    skip_reasons: dict[str, str] = {}
    for item in tasks:
        if not importance_meets(item.importance, min_importance) or item.confidence < min_confidence:
            skip_reasons[item.client_id] = "below_threshold"
        else:
            thresholded.append(item)

    selected = thresholded[:max_tasks]
    for item in thresholded[max_tasks:]:
        skip_reasons[item.client_id] = "max_tasks_limit"

    selected_by_client_id = {item.client_id: item for item in selected}
    client_to_db_id: dict[str, str] = {}
    task_previews: list[dict[str, Any]] = []
    action_counts = {"insert": 0, "update": 0, "duplicate": 0, "skip": 0}

    for item in tasks:
        proposed_id = stable_task_id(item.name)
        base_payload = {
            "client_id": item.client_id,
            "proposed_id": proposed_id,
            "name": item.name,
            "description": item.description,
            "status": item.status,
            "notes": item.notes,
            "importance": item.importance,
            "confidence": item.confidence,
        }
        skip_reason = skip_reasons.get(item.client_id)
        if skip_reason:
            action_counts["skip"] += 1
            task_previews.append(
                {
                    **base_payload,
                    "action": "skip",
                    "reason": skip_reason,
                    "based_on": [
                        {
                            "type": "thresholds",
                            "min_importance": min_importance,
                            "min_confidence": min_confidence,
                            "max_tasks": max_tasks,
                        }
                    ],
                }
            )
            continue

        existing = conn.execute(
            "SELECT id, name, status, notes, description, importance FROM tasks WHERE id = ?",
            (proposed_id,),
        ).fetchone()
        if existing:
            client_to_db_id[item.client_id] = str(existing["id"])
            merged_notes = item.notes or existing["notes"]
            merged_importance = merge_importance(str(existing["importance"]), item.importance)
            merged_description = (
                existing["description"]
                or item.description
                or concise_description(None, name=item.name, notes=merged_notes)
            )
            action_counts["update"] += 1
            task_previews.append(
                {
                    **base_payload,
                    "action": "update",
                    "db_id": existing["id"],
                    "based_on": [
                        {
                            "type": "stable_id_match",
                            "id": existing["id"],
                            "name": existing["name"],
                        }
                    ],
                    "merged": {
                        "status": merge_status(str(existing["status"]), item.status),
                        "notes": merged_notes,
                        "importance": merged_importance,
                        "description": trim_to_char_limit(str(merged_description), MAX_DESCRIPTION_CHARS),
                    },
                }
            )
            continue

        related = find_related_task(conn, item.name, threshold=dedupe_threshold)
        if related:
            related_id = str(related["id"])
            client_to_db_id[item.client_id] = related_id
            action_counts["duplicate"] += 1
            task_previews.append(
                {
                    **base_payload,
                    "action": "duplicate",
                    "db_id": related_id,
                    "based_on": [
                        {
                            "type": "similarity_match",
                            "id": related_id,
                            "name": related["name"],
                            "similarity": related["similarity"],
                            "dedupe_threshold": dedupe_threshold,
                        }
                    ],
                }
            )
            continue

        client_to_db_id[item.client_id] = proposed_id
        action_counts["insert"] += 1
        task_previews.append(
            {
                **base_payload,
                "action": "insert",
                "db_id": proposed_id,
                "based_on": [
                    {
                        "type": "new_stable_id",
                        "source_hash": conv_hash,
                    }
                ],
            }
        )

    edge_previews: list[dict[str, Any]] = []
    edge_action_counts = {"insert": 0, "exists": 0, "skip": 0}
    proposed_edge_keys: set[tuple[str, str, str]] = set()
    for edge in edges:
        base_payload = {
            "source_client_id": edge.source_client_id,
            "target_client_id": edge.target_client_id,
            "relation": edge.relation,
            "note": edge.note,
        }
        if (
            edge.source_client_id not in selected_by_client_id
            or edge.target_client_id not in selected_by_client_id
        ):
            edge_action_counts["skip"] += 1
            edge_previews.append(
                {
                    **base_payload,
                    "action": "skip",
                    "reason": "source_or_target_not_selected",
                }
            )
            continue

        source_id = client_to_db_id[edge.source_client_id]
        target_id = client_to_db_id[edge.target_client_id]
        if source_id == target_id:
            edge_action_counts["skip"] += 1
            edge_previews.append(
                {
                    **base_payload,
                    "source_id": source_id,
                    "target_id": target_id,
                    "action": "skip",
                    "reason": "self_edge_after_dedupe",
                }
            )
            continue

        edge_key = (source_id, target_id, edge.relation)
        if edge_key in proposed_edge_keys:
            edge_action_counts["skip"] += 1
            edge_previews.append(
                {
                    **base_payload,
                    "source_id": source_id,
                    "target_id": target_id,
                    "action": "skip",
                    "reason": "duplicate_in_preview",
                }
            )
            continue
        proposed_edge_keys.add(edge_key)

        if edge.relation in {"depends_on", "decomposes_to"} and creates_relation_cycle(
            conn, source_id, target_id, edge.relation
        ):
            edge_action_counts["skip"] += 1
            edge_previews.append(
                {
                    **base_payload,
                    "source_id": source_id,
                    "target_id": target_id,
                    "action": "skip",
                    "reason": "would_create_cycle",
                }
            )
            continue

        existing_edge = conn.execute(
            """
            SELECT id FROM task_edges
            WHERE source_id = ? AND target_id = ? AND relation = ?
            """,
            (source_id, target_id, edge.relation),
        ).fetchone()
        if existing_edge:
            edge_action_counts["exists"] += 1
            edge_previews.append(
                {
                    **base_payload,
                    "source_id": source_id,
                    "target_id": target_id,
                    "action": "exists",
                    "db_id": existing_edge["id"],
                    "based_on": [{"type": "existing_edge"}],
                }
            )
            continue

        edge_action_counts["insert"] += 1
        edge_previews.append(
            {
                **base_payload,
                "source_id": source_id,
                "target_id": target_id,
                "action": "insert",
                "based_on": [{"type": "llm_extracted_edge"}],
            }
        )

    return {
        "source_hash": conv_hash,
        "thresholds": {
            "min_importance": min_importance,
            "min_confidence": min_confidence,
            "max_tasks": max_tasks,
            "dedupe_threshold": dedupe_threshold,
        },
        "candidate_count": len(tasks),
        "selected_count": len(selected),
        "tasks": task_previews,
        "edges": edge_previews,
        "summary": {
            "tasks": action_counts,
            "edges": edge_action_counts,
        },
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


def find_related_task(
    conn: sqlite3.Connection,
    name: str,
    *,
    threshold: float,
) -> dict[str, Any] | None:
    candidate_tokens = similarity_tokens(name)
    if not candidate_tokens:
        return None

    best: dict[str, Any] | None = None
    rows = conn.execute(
        f"""
        SELECT id, name, description, notes
        FROM tasks
        ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
        """
    ).fetchall()
    for row in rows:
        existing_text = f"{row['name']} {row['description']}"
        if row["notes"]:
            existing_text = f"{existing_text} {row['notes']}"
        score = name_similarity(candidate_tokens, similarity_tokens(existing_text))
        if score < threshold:
            continue
        if best is None or score > best["similarity"]:
            best = {
                "id": row["id"],
                "name": row["name"],
                "similarity": round(score, 4),
            }
    return best


def name_similarity(left_tokens: set[str], right_tokens: set[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = intersection / union if union else 0.0
    overlap = intersection / min(len(left_tokens), len(right_tokens))
    return max(jaccard, overlap * 0.88)


def similarity_tokens(text: str) -> set[str]:
    normalized = normalize_text(text).lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9_\-]{2,}", normalized):
        tokens.add(word)

    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    for run in cjk_runs:
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def creates_relation_cycle(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relation: str,
) -> bool:
    rows = conn.execute(
        """
        WITH RECURSIVE deps(node_id) AS (
            SELECT target_id FROM task_edges
            WHERE source_id = ? AND relation = ?
            UNION
            SELECT e.target_id FROM task_edges e
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
            "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
        ).fetchall()
    }
    edges = conn.execute(
        """
        SELECT e.relation, e.note, s.id AS source_id, s.name AS source_name,
               t.id AS target_id, t.name AS target_name
        FROM task_edges e
        JOIN tasks s ON s.id = e.source_id
        JOIN tasks t ON t.id = e.target_id
        WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})
        ORDER BY e.created_at DESC
        LIMIT 60
        """.format(placeholders=",".join("?" for _ in rows) or "NULL"),
        [row["id"] for row in rows] * 2,
    ).fetchall() if rows else []

    payload = {
        "counts": counts,
        "tasks": [dict(row) for row in rows],
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
        f"""
        SELECT id, name, description, status, notes, importance, confidence, updated_at
        FROM tasks
        WHERE status IN ('active', 'blocked', 'ready')
        ORDER BY
            CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'ready' THEN 2 ELSE 3 END,
            {IMPORTANCE_ORDER_SQL} DESC,
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
            f"""
            SELECT id, name, description, status, notes, importance, confidence, updated_at
            FROM tasks
            WHERE name LIKE ? OR description LIKE ? OR COALESCE(notes, '') LIKE ?
            ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", max(1, limit - len(rows_by_id))),
        ).fetchall()
        for row in matches:
            rows_by_id.setdefault(row["id"], row)

    if len(rows_by_id) < limit:
        recent_done = conn.execute(
            f"""
            SELECT id, name, description, status, notes, importance, confidence, updated_at
            FROM tasks
            WHERE status = 'done'
            ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
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
