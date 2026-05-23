from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any

from embeddings import (
    EmbeddingConfig,
    EmbeddingUnavailableError,
    cosine_similarity,
    embed_query,
    load_embedding_index,
    refresh_embeddings,
)
from store import IMPORTANCE_ORDER_SQL, IMPORTANCE_RANK, name_similarity, similarity_tokens


NODE_COLUMNS = """
    id, project_id, is_root, name, description, status, notes,
    importance, confidence, created_at, updated_at
"""


def render_global_state_memory(conn: sqlite3.Connection, *, project_id: str = "default") -> str:
    root = fetch_root(conn, project_id=project_id)
    lines = [
        "# Task Status State Memory",
        "",
        "## Scope",
        "- mode: global",
        "- selection: root node plus directly connected nodes",
        "",
    ]
    if not root:
        lines.append("## Root")
        lines.append("- No root node is available.")
        return "\n".join(lines).rstrip() + "\n"

    edges = fetch_edges(conn, project_id=project_id)
    nodes_by_id = fetch_nodes_by_id(conn, project_id=project_id)
    adjacent_ids = sorted(
        {
            edge["target_id"] if edge["source_id"] == root["id"] else edge["source_id"]
            for edge in edges
            if edge["source_id"] == root["id"] or edge["target_id"] == root["id"]
        },
        key=lambda node_id: node_sort_key(nodes_by_id[node_id]),
    )

    lines.append("## Root")
    lines.extend(format_node_block(root, explanation="全局根节点；描述当前项目的顶层目标。"))
    lines.append("")
    lines.append("## Directly Connected Nodes")
    if not adjacent_ids:
        lines.append("- No nodes are directly connected to the root.")
    else:
        for node_id in adjacent_ids:
            node = nodes_by_id[node_id]
            relation_text = explain_relations_between(root["id"], node_id, edges)
            lines.extend(format_node_block(node, explanation=f"与根节点直接相连。{relation_text}"))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_local_state_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    project_id: str = "default",
    min_score: float = 0.08,
    retrieval: str = "auto",
    embedding_config: EmbeddingConfig | None = None,
    refresh_embedding_index: bool = True,
) -> str:
    root = fetch_root(conn, project_id=project_id)
    nodes_by_id = fetch_nodes_by_id(conn, project_id=project_id)
    edges = fetch_edges(conn, project_id=project_id)
    match = find_best_query_match(
        conn,
        nodes_by_id.values(),
        edges=edges,
        query=query,
        min_score=min_score,
        retrieval=retrieval,
        embedding_config=embedding_config,
        refresh_embedding_index=refresh_embedding_index,
    )

    lines = [
        "# Task Status State Memory",
        "",
        "## Scope",
        "- mode: local",
        f"- query: {query}",
        f"- retrieval: {match['retrieval'] if match else retrieval}",
        "- selection: shortest root-to-match path plus direct children of the matched node",
        "",
    ]
    if not root:
        lines.append("## Match")
        lines.append("- No root node is available, so local path memory cannot be rendered.")
        return "\n".join(lines).rstrip() + "\n"
    if not match:
        lines.append("## Match")
        lines.append(f"- No node matched the query above min_score={min_score:.2f}.")
        return "\n".join(lines).rstrip() + "\n"

    matched_node = match["node"]
    path = shortest_path(root["id"], matched_node["id"], edges)
    path_ids = path["nodes"] if path else [matched_node["id"]]
    path_edges = path["edges"] if path else []
    child_edges = direct_child_edges(matched_node["id"], edges)

    lines.append("## Match")
    lines.append(f"- matched_id: `{matched_node['id']}`")
    lines.append(f"- match_score: {match['score']:.3f}")
    lines.append(f"- root_distance: {match['root_distance']}")
    lines.append("")

    lines.append("## Root To Match Path")
    for index, node_id in enumerate(path_ids):
        node = nodes_by_id[node_id]
        explanation = path_node_explanation(
            node_id=node_id,
            root_id=root["id"],
            matched_id=matched_node["id"],
            path_edges=path_edges,
            index=index,
            match_score=match["score"],
        )
        lines.extend(format_node_block(node, explanation=explanation))
        lines.append("")

    lines.append("## Direct Children Of Match")
    if not child_edges:
        lines.append("- The matched node has no direct `decomposes_to` children.")
    else:
        for edge in child_edges:
            child = nodes_by_id[edge["target_id"]]
            explanation = f"查询命中节点的一级子节点；通过 `decomposes_to` 承接更细任务。"
            if edge["note"]:
                explanation += f" 边说明：{edge['note']}"
            lines.extend(format_node_block(child, explanation=explanation))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def fetch_root(conn: sqlite3.Connection, *, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT {NODE_COLUMNS}
        FROM tasks
        WHERE project_id = ? AND is_root = 1
        ORDER BY {IMPORTANCE_ORDER_SQL} DESC, updated_at DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()


def fetch_nodes_by_id(conn: sqlite3.Connection, *, project_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        f"""
        SELECT {NODE_COLUMNS}
        FROM tasks
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    return {row["id"]: row for row in rows}


def fetch_edges(conn: sqlite3.Connection, *, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.id, e.source_id, e.target_id, e.relation, e.note, e.created_at
        FROM task_edges e
        JOIN tasks source ON source.id = e.source_id
        JOIN tasks target ON target.id = e.target_id
        WHERE source.project_id = ? AND target.project_id = ?
        ORDER BY e.created_at ASC, e.id ASC
        """,
        (project_id, project_id),
    ).fetchall()


def find_best_query_match(
    conn: sqlite3.Connection,
    nodes: Any,
    *,
    edges: list[sqlite3.Row],
    query: str,
    min_score: float,
    retrieval: str,
    embedding_config: EmbeddingConfig | None,
    refresh_embedding_index: bool,
) -> dict[str, Any] | None:
    if retrieval not in {"auto", "embedding", "local"}:
        raise ValueError(f"Unsupported retrieval mode: {retrieval}")
    node_list = list(nodes)
    if retrieval in {"auto", "embedding"}:
        try:
            embedding_match = find_best_embedding_match(
                conn,
                node_list,
                edges=edges,
                query=query,
                min_score=min_score,
                config=embedding_config or EmbeddingConfig(),
                refresh_embedding_index=refresh_embedding_index,
            )
            if embedding_match or retrieval == "embedding":
                return embedding_match
        except EmbeddingUnavailableError:
            if retrieval == "embedding":
                raise

    return find_best_local_match(
        node_list,
        edges=edges,
        query=query,
        min_score=min_score,
    )


def find_best_embedding_match(
    conn: sqlite3.Connection,
    nodes: list[sqlite3.Row],
    *,
    edges: list[sqlite3.Row],
    query: str,
    min_score: float,
    config: EmbeddingConfig,
    refresh_embedding_index: bool,
) -> dict[str, Any] | None:
    if not nodes:
        return None
    if refresh_embedding_index:
        refresh_embeddings(conn, config=config)
    index = load_embedding_index(conn, config=config)
    if not index:
        raise EmbeddingUnavailableError("No cached embeddings are available.")
    query_vector = embed_query(query, config=config)
    depths = root_distances(nodes, edges)
    scored: list[dict[str, Any]] = []
    for node in nodes:
        vector = index.get(node["id"])
        if vector is None:
            continue
        score = cosine_similarity(query_vector, vector)
        if score < min_score:
            continue
        distance = depths.get(node["id"])
        depth = distance if distance is not None else 0
        scored.append(
            {
                "node": node,
                "score": score,
                "root_distance": distance if distance is not None else "unknown",
                "rank_score": score + min(depth, 8) * 0.015,
                "retrieval": f"embedding:{config.provider}/{config.model}"
                + (f":{config.dimensions}d" if config.dimensions else ""),
            }
        )
    if not scored:
        return None
    return max(
        scored,
        key=lambda item: (
            item["rank_score"],
            0 if item["root_distance"] == "unknown" else item["root_distance"],
            IMPORTANCE_RANK.get(item["node"]["importance"], 1),
            item["node"]["updated_at"],
        ),
    )


def find_best_local_match(
    nodes: list[sqlite3.Row],
    *,
    edges: list[sqlite3.Row],
    query: str,
    min_score: float,
) -> dict[str, Any] | None:
    query_tokens = similarity_tokens(query)
    if not query_tokens:
        return None

    depths = root_distances(nodes, edges)
    scored: list[dict[str, Any]] = []
    for node in nodes:
        text = node_search_text(node)
        score = name_similarity(query_tokens, similarity_tokens(text))
        if score < min_score:
            continue
        distance = depths.get(node["id"])
        depth = distance if distance is not None else 0
        scored.append(
            {
                "node": node,
                "score": score,
                "root_distance": distance if distance is not None else "unknown",
                "rank_score": score + min(depth, 8) * 0.025,
                "retrieval": "local-token",
            }
        )

    if not scored:
        return None
    return max(
        scored,
        key=lambda item: (
            item["rank_score"],
            0 if item["root_distance"] == "unknown" else item["root_distance"],
            IMPORTANCE_RANK.get(item["node"]["importance"], 1),
            item["node"]["updated_at"],
        ),
    )


def root_distances(nodes: Any, edges: list[sqlite3.Row]) -> dict[str, int]:
    node_list = list(nodes)
    node_ids = {node["id"] for node in node_list}
    roots = [node["id"] for node in node_list if int(node["is_root"] or 0) == 1]
    adjacency = {node_id: set() for node_id in node_ids}
    for edge in edges:
        if edge["source_id"] in node_ids and edge["target_id"] in node_ids:
            adjacency[edge["source_id"]].add(edge["target_id"])
            adjacency[edge["target_id"]].add(edge["source_id"])

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


def shortest_path(start_id: str, target_id: str, edges: list[sqlite3.Row]) -> dict[str, list] | None:
    if start_id == target_id:
        return {"nodes": [start_id], "edges": []}

    adjacency: dict[str, list[tuple[str, sqlite3.Row]]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source_id"], []).append((edge["target_id"], edge))
        adjacency.setdefault(edge["target_id"], []).append((edge["source_id"], edge))

    queue = deque([start_id])
    visited = {start_id}
    previous: dict[str, tuple[str, sqlite3.Row]] = {}
    while queue:
        current = queue.popleft()
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            previous[neighbor] = (current, edge)
            if neighbor == target_id:
                return unwind_path(start_id, target_id, previous)
            queue.append(neighbor)
    return None


def unwind_path(
    start_id: str,
    target_id: str,
    previous: dict[str, tuple[str, sqlite3.Row]],
) -> dict[str, list]:
    nodes = [target_id]
    path_edges: list[sqlite3.Row] = []
    current = target_id
    while current != start_id:
        parent_id, edge = previous[current]
        nodes.append(parent_id)
        path_edges.append(edge)
        current = parent_id
    nodes.reverse()
    path_edges.reverse()
    return {"nodes": nodes, "edges": path_edges}


def direct_child_edges(node_id: str, edges: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        edge
        for edge in edges
        if edge["source_id"] == node_id and edge["relation"] == "decomposes_to"
    ]


def path_node_explanation(
    *,
    node_id: str,
    root_id: str,
    matched_id: str,
    path_edges: list[sqlite3.Row],
    index: int,
    match_score: float,
) -> str:
    if node_id == root_id and node_id == matched_id:
        return f"该节点既是根节点也是查询命中节点；match_score={match_score:.3f}。"
    if node_id == root_id:
        return "根节点；提供局部记忆路径的全局目标。"
    if node_id == matched_id:
        relation = explain_path_edge(path_edges[index - 1]) if index > 0 else ""
        return f"自然语言查询命中的最相关节点；match_score={match_score:.3f}。{relation}"
    relation = explain_path_edge(path_edges[index - 1]) if index > 0 else ""
    return f"根节点到查询命中节点的最短路径节点。{relation}"


def explain_path_edge(edge: sqlite3.Row) -> str:
    text = f"路径边：`{edge['relation']}` from `{edge['source_id']}` to `{edge['target_id']}`."
    if edge["note"]:
        text += f" 边说明：{edge['note']}"
    return text


def explain_relations_between(root_id: str, node_id: str, edges: list[sqlite3.Row]) -> str:
    relation_texts = []
    for edge in edges:
        if {edge["source_id"], edge["target_id"]} != {root_id, node_id}:
            continue
        direction = "root -> node" if edge["source_id"] == root_id else "node -> root"
        text = f"`{edge['relation']}` ({direction})"
        if edge["note"]:
            text += f": {edge['note']}"
        relation_texts.append(text)
    if not relation_texts:
        return ""
    return "关系：" + "; ".join(relation_texts)


def node_search_text(node: sqlite3.Row) -> str:
    parts = [node["name"], node["description"], node["notes"] or ""]
    return " ".join(part for part in parts if part)


def node_sort_key(node: sqlite3.Row) -> tuple:
    status_order = {"active": 0, "blocked": 1, "ready": 2, "done": 3, "candidate": 4}
    return (
        status_order.get(node["status"], 9),
        -IMPORTANCE_RANK.get(node["importance"], 1),
        node["name"],
    )


def format_node_block(node: sqlite3.Row, *, explanation: str) -> list[str]:
    lines = [
        f"- **{node['name']}** (`{node['id']}`)",
        f"  - description: {node['description'] or 'No description recorded.'}",
        f"  - status: {node['status']}",
        f"  - importance: {node['importance']}",
        f"  - explanation: {explanation}",
    ]
    if node["notes"]:
        lines.append(f"  - notes: {node['notes']}")
    return lines
