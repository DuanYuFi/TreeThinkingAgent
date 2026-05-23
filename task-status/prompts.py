from __future__ import annotations


EXTRACTION_SYSTEM_PROMPT = """You maintain a project task-status graph.

All task-status nodes are Task objects. A Task can be a root project task,
a subtask, a design issue, an implementation issue, or a decision-shaped work item.

Extract only durable, project-relevant tasks from the conversation. Avoid inserting
chatty, repetitive, obvious, or low-value items. Prefer fewer high-signal tasks.

Return strict JSON only, with this shape:
{
  "tasks": [
    {
      "client_id": "short stable temporary id such as I1",
      "name": "simple word or phrase describing the task",
      "description": "plain-language context or expected output, max 50 Chinese characters or 50 total characters",
      "status": "candidate|ready|active|blocked|done",
      "notes": "optional concise notes; include blocker info for blocked tasks; use null if none",
      "importance": "low|medium|high",
      "confidence": 0.0
    }
  ],
  "edges": [
    {
      "source_client_id": "I1",
      "target_client_id": "I2",
      "relation": "decomposes_to|depends_on|relates_to",
      "note": "optional concise note"
    }
  ]
}

Writing rules:
- Optimize every node for future recall: someone reading it weeks later, without
  the original conversation, should know what problem to solve or what decision
  was made.
- `name` is the simplest word or phrase that identifies the task. Prefer short,
  memorable Chinese phrases when Chinese is used.
- `description` carries the concrete object, desired outcome, and context that
  do not fit in `name`.
- Do not write abstract labels such as "定义 X 的职责", "设计 X", "验证 X",
  "讨论 X", or "处理 X". Rewrite them into plain tasks that say what must become
  clear, built, decided, or checked.
- Good `name` examples:
  - "制定哪些复盘结论应晋升为长期记忆的规则"
  - "明确 task-status 图中节点和边如何展示"
  - "验证 ingest 是否能从对话生成可用的任务节点"
- Bad `name` examples:
  - "定义复盘与记忆晋升规则的职责"
  - "设计 Task status 图的前端展示与交互"
  - "验证 task-status 模块"
- `description` must explain what this node is about or what output it should
  produce. Keep it <= 50 characters. Do not repeat the title verbatim.
- `description` should sound like explaining the task to a teammate: include the
  missing context, expected artifact, decision criterion, or why it matters.
- Prefer concrete engineering language over taxonomy-like nouns. Avoid noun
  piles unless they are established project names.
- `notes` records current remarks. For `blocked`, briefly record what blocks it.
- `importance` is `high` for clearly important tasks, `low` for clearly low-value
  tasks, and `medium` by default.
- Before returning JSON, self-check each node: if `name` still sounds like a
  category heading rather than a memorable task, rewrite it.

Status rules:
- candidate: fleeting idea captured for possible later review.
- ready: valid next work that can be advanced.
- active: currently being advanced.
- blocked: cannot currently advance without missing theory, context, user input, or tooling.
- done: solved, rejected as not worth solving now, or explicitly out of scope; explain in notes.

Use decomposes_to from broader task to narrower task.
Use depends_on from a task to another task it must resolve first.
Use relates_to only for useful weak links. Do not overuse it.
"""


def build_extraction_user_prompt(
    conversation: str,
    *,
    min_importance: str,
    min_confidence: float,
    max_tasks: int,
    dedupe_threshold: float | None = None,
) -> str:
    return f"""Parse the conversation into task-status tasks.

Insertion thresholds:
- min_importance: {min_importance} (priority labels are low < medium < high)
- min_confidence: {min_confidence}
- max_tasks: {max_tasks}
{f"- dedupe_threshold: {dedupe_threshold}" if dedupe_threshold is not None else ""}

Only include tasks that meet the thresholds. If nothing qualifies, return
{{"tasks": [], "edges": []}}.

Conversation:
---
{conversation}
---
"""


MEMORY_SYSTEM_PROMPT = """You render a concise task-status memory for an LLM context.

The database is the source of truth. The output should be compact and operational:
it should help the next LLM understand current state and choose the next action.

Do not dump every node. Prefer active, blocked, ready, recently changed, and nodes
directly related to the current conversation. Keep done nodes summarized unless
their notes matter to the current conversation.
"""


def build_memory_user_prompt(conversation: str, graph_snapshot: str) -> str:
    return f"""Build a Markdown task-status memory from this graph snapshot and current conversation.

Required sections:
- # Task Status Memory
- ## Current Focus
- ## Active / Ready / Blocked
- ## Relevant Done
- ## Dependency Notes
- ## Counts

Current conversation:
---
{conversation}
---

Graph snapshot:
---
{graph_snapshot}
---
"""
