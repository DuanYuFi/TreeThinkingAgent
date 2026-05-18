from __future__ import annotations


EXTRACTION_SYSTEM_PROMPT = """You maintain a project task-status graph.

All task-status nodes are Inquiry objects. An Inquiry can be a root project question,
a subproblem, a design issue, an implementation issue, or a decision-shaped question.

Extract only durable, project-relevant inquiries from the conversation. Avoid inserting
chatty, repetitive, obvious, or low-value items. Prefer fewer high-signal inquiries.

Return strict JSON only, with this shape:
{
  "inquiries": [
    {
      "client_id": "short stable temporary id such as I1",
      "content": "natural-language task or inquiry, in Chinese or the conversation language",
      "description": "one concrete explanation of this node, max 50 Chinese characters or 50 total characters",
      "status": "candidate|ready|active|blocked|done",
      "answer": "optional concise answer; use null if unresolved",
      "importance": 0.0,
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
- `content` should be natural and specific enough to recover context later.
- Avoid abstract labels such as "define X responsibilities". Prefer phrasing like
  "clarify what X owns, what it produces, and where its boundary is".
- `description` must explain what this node is about or what output it should
  produce. Keep it <= 50 characters. Do not repeat the title verbatim.
- Prefer concrete engineering language over taxonomy-like nouns.

Status rules:
- candidate: fleeting idea captured for possible later review.
- ready: valid next work that can be advanced.
- active: currently being advanced.
- blocked: cannot currently advance without missing theory, context, user input, or tooling.
- done: answered, solved, rejected as not worth solving now, or explicitly out of scope; explain in answer.

Use decomposes_to from broader inquiry to narrower inquiry.
Use depends_on from an inquiry to another inquiry it must resolve first.
Use relates_to only for useful weak links. Do not overuse it.
"""


def build_extraction_user_prompt(
    conversation: str,
    *,
    min_importance: float,
    min_confidence: float,
    max_inquiries: int,
    dedupe_threshold: float | None = None,
) -> str:
    return f"""Parse the conversation into task-status inquiries.

Insertion thresholds:
- min_importance: {min_importance}
- min_confidence: {min_confidence}
- max_inquiries: {max_inquiries}
{f"- dedupe_threshold: {dedupe_threshold}" if dedupe_threshold is not None else ""}

Only include inquiries that meet the thresholds. If nothing qualifies, return
{{"inquiries": [], "edges": []}}.

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
their answer matters to the current conversation.
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
