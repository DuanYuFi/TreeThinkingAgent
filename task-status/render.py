from __future__ import annotations

import json
from typing import Any


def render_memory_from_snapshot(snapshot_json: str) -> str:
    snapshot = json.loads(snapshot_json)
    tasks = snapshot.get("tasks", [])
    edges = snapshot.get("edges", [])
    counts = snapshot.get("counts", {})

    by_status: dict[str, list[dict[str, Any]]] = {
        "active": [],
        "blocked": [],
        "ready": [],
        "done": [],
        "candidate": [],
    }
    for item in tasks:
        by_status.setdefault(item.get("status", "candidate"), []).append(item)

    lines: list[str] = ["# Task Status Memory", ""]

    focus = first_of(by_status["active"], by_status["blocked"], by_status["ready"])
    lines.append("## Current Focus")
    if focus:
        lines.append(format_task(focus, include_notes=True))
    else:
        lines.append("- No active, blocked, or ready task is currently visible.")
    lines.append("")

    lines.append("## Active / Ready / Blocked")
    for status in ("active", "blocked", "ready"):
        items = by_status.get(status, [])
        if not items:
            continue
        lines.append(f"### {status}")
        for item in items[:8]:
            lines.append(format_task(item, include_notes=status == "blocked"))
        lines.append("")
    if not any(by_status.get(status) for status in ("active", "blocked", "ready")):
        lines.append("- No active, ready, or blocked tasks in the selected view.")
        lines.append("")

    lines.append("## Relevant Done")
    done_items = by_status.get("done", [])[:6]
    if done_items:
        for item in done_items:
            lines.append(format_task(item, include_notes=True))
    else:
        lines.append("- No done task is relevant to this context window.")
    lines.append("")

    lines.append("## Dependency Notes")
    dependency_lines = [
        format_edge(edge)
        for edge in edges
        if edge.get("relation") in {"depends_on", "decomposes_to"}
    ][:10]
    if dependency_lines:
        lines.extend(dependency_lines)
    else:
        lines.append("- No dependency edges are visible in this context window.")
    lines.append("")

    lines.append("## Counts")
    for status in ("candidate", "ready", "active", "blocked", "done"):
        lines.append(f"- {status}: {int(counts.get(status, 0))}")
    return "\n".join(lines).rstrip() + "\n"


def first_of(*groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    for group in groups:
        if group:
            return group[0]
    return None


def format_task(item: dict[str, Any], *, include_notes: bool) -> str:
    task_id = item.get("id", "unknown")
    status = item.get("status", "candidate")
    name = item.get("name", "")
    description = item.get("description", "")
    importance = item.get("importance", "medium")
    line = f"- `{task_id}` {status}: {name} (importance={importance})"
    if description:
        line += f"\n  description: {description}"
    notes = item.get("notes")
    if include_notes and notes:
        line += f"\n  notes: {notes}"
    return line


def format_edge(edge: dict[str, Any]) -> str:
    relation = edge.get("relation", "relates_to")
    source_id = edge.get("source_id", "unknown")
    target_id = edge.get("target_id", "unknown")
    source = edge.get("source_name", "")
    target = edge.get("target_name", "")
    note = edge.get("note")
    line = f"- `{source_id}` {relation} `{target_id}`: {source} -> {target}"
    if note:
        line += f" ({note})"
    return line
