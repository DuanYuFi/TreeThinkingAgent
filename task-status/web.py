from __future__ import annotations

import argparse
import json
import sqlite3
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from store import (
    DEFAULT_DB_PATH,
    VALID_RELATIONS,
    concise_description,
    creates_relation_cycle,
    init_db,
    normalize_text,
    utc_now,
)
STATIC_DIR = Path(__file__).with_name("web")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the task-status graph viewer.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    handler = make_handler(Path(args.db))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving task-status viewer at http://{args.host}:{args.port}")
    print(f"Database: {Path(args.db)}")
    server.serve_forever()
    return 0


def make_handler(db_path: Path):
    class TaskStatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/graph":
                self.send_json(load_graph(db_path))
                return

            path = "index.html" if parsed.path in {"", "/"} else parsed.path.lstrip("/")
            file_path = (STATIC_DIR / path).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self.send_error(403)
                return
            if not file_path.exists() or not file_path.is_file():
                self.send_error(404)
                return
            self.send_file(file_path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/edges":
                try:
                    payload = self.read_json()
                    self.send_json(create_edge(db_path, payload), status=201)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=400)
                except sqlite3.IntegrityError as exc:
                    self.send_json({"error": str(exc)}, status=409)
                return
            self.send_error(404)

        def do_PATCH(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/nodes/"):
                node_id = parsed.path.removeprefix("/api/nodes/")
                try:
                    payload = self.read_json()
                    self.send_json(update_node(db_path, node_id, payload))
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=400)
                return
            self.send_error(404)

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/edges/"):
                edge_id = parsed.path.removeprefix("/api/edges/")
                try:
                    self.send_json(delete_edge(db_path, edge_id))
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=400)
                return
            self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("JSON payload must be an object.")
            return payload

        def send_json(self, payload: dict, *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, file_path: Path) -> None:
            body = file_path.read_bytes()
            content_type = content_type_for(file_path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TaskStatusHandler


def load_graph(db_path: Path) -> dict:
    if not db_path.exists():
        return {"nodes": [], "edges": [], "counts": {}, "error": f"Database not found: {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    all_node_rows = conn.execute(
        """
        SELECT id, project_id, is_root, content, description, status, answer, importance, confidence, created_at, updated_at
        FROM inquiries
        ORDER BY
            is_root DESC,
            CASE status
                WHEN 'active' THEN 0
                WHEN 'blocked' THEN 1
                WHEN 'ready' THEN 2
                WHEN 'done' THEN 3
                ELSE 4
            END,
            importance DESC,
            updated_at DESC
        """
    ).fetchall()
    all_edge_rows = conn.execute(
        """
        SELECT id, source_id, target_id, relation, note, created_at
        FROM inquiry_edges
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    root_distances = shortest_distances_from_roots(all_node_rows, all_edge_rows)

    count_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM inquiries GROUP BY status"
    ).fetchall()
    return {
        "nodes": [node_payload(row, root_distances) for row in all_node_rows],
        "edges": [dict(row) for row in all_edge_rows],
        "counts": {row["status"]: row["count"] for row in count_rows},
    }


def update_node(db_path: Path, node_id: str, payload: dict) -> dict:
    content = normalize_text(str(payload.get("content", "")))
    if not node_id:
        raise ValueError("Node id is required.")
    if not content:
        raise ValueError("Content is required.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    existing = conn.execute(
        "SELECT id, answer, description FROM inquiries WHERE id = ?",
        (node_id,),
    ).fetchone()
    if not existing:
        raise ValueError(f"Node not found: {node_id}")
    if "description" in payload:
        description = concise_description(
            payload.get("description"),
            content=content,
            answer=existing["answer"],
        )
    else:
        description = existing["description"] or concise_description(
            None,
            content=content,
            answer=existing["answer"],
        )
    with conn:
        cursor = conn.execute(
            "UPDATE inquiries SET content = ?, description = ?, updated_at = ? WHERE id = ?",
            (content, description, utc_now(), node_id),
        )
    if cursor.rowcount == 0:
        raise ValueError(f"Node not found: {node_id}")
    return {"ok": True, "id": node_id, "content": content, "description": description}


def create_edge(db_path: Path, payload: dict) -> dict:
    source_id = normalize_text(str(payload.get("source_id", "")))
    target_id = normalize_text(str(payload.get("target_id", "")))
    relation = normalize_text(str(payload.get("relation", "")))
    note_value = payload.get("note")
    note = normalize_text(str(note_value)) if note_value not in (None, "") else None

    if not source_id or not target_id:
        raise ValueError("Both source_id and target_id are required.")
    if source_id == target_id:
        raise ValueError("Source and target must be different nodes.")
    if relation not in VALID_RELATIONS:
        raise ValueError(f"Invalid relation: {relation}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    source = conn.execute("SELECT id FROM inquiries WHERE id = ?", (source_id,)).fetchone()
    target = conn.execute("SELECT id FROM inquiries WHERE id = ?", (target_id,)).fetchone()
    if not source or not target:
        raise ValueError("Source or target node does not exist.")
    if relation in {"depends_on", "decomposes_to"} and creates_relation_cycle(conn, source_id, target_id, relation):
        raise ValueError(f"Adding {relation} would create a cycle.")

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO inquiry_edges (source_id, target_id, relation, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, target_id, relation, note, utc_now()),
        )
    return {"ok": True, "id": cursor.lastrowid}


def delete_edge(db_path: Path, edge_id: str) -> dict:
    try:
        parsed_edge_id = int(edge_id)
    except ValueError as exc:
        raise ValueError("Edge id must be an integer.") from exc

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    with conn:
        cursor = conn.execute("DELETE FROM inquiry_edges WHERE id = ?", (parsed_edge_id,))
    if cursor.rowcount == 0:
        raise ValueError(f"Edge not found: {parsed_edge_id}")
    return {"ok": True, "id": parsed_edge_id}


def shortest_distances_from_roots(node_rows: list[sqlite3.Row], edge_rows: list[sqlite3.Row]) -> dict[str, int]:
    node_ids = {row["id"] for row in node_rows}
    roots = [row["id"] for row in node_rows if int(row["is_root"] or 0) == 1]
    adjacency = {node_id: set() for node_id in node_ids}
    for edge in edge_rows:
        source_id = edge["source_id"]
        target_id = edge["target_id"]
        if source_id in node_ids and target_id in node_ids:
            adjacency[source_id].add(target_id)
            adjacency[target_id].add(source_id)

    distances = {root_id: 0 for root_id in roots}
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        for neighbor in adjacency[current]:
            if neighbor in distances:
                continue
            distances[neighbor] = distances[current] + 1
            queue.append(neighbor)
    return distances


def node_payload(row: sqlite3.Row, root_distances: dict[str, int]) -> dict:
    payload = dict(row)
    payload["root_distance"] = root_distances.get(row["id"])
    return payload


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".js":
        return "text/javascript; charset=utf-8"
    return "application/octet-stream"


if __name__ == "__main__":
    raise SystemExit(main())
