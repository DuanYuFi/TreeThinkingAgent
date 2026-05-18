# AGENTS.md

## Purpose

This subproject is the local task-status memory for building TTA. It stores durable
project work as a SQLite-backed Inquiry DAG and can render a compact Markdown
memory block for an LLM context.

Do not confuse this with the future product-level memory system. This module is a
working tool for tracking our own engineering context while building TTA.

## When To Use It

Use task-status when you need to:

- recover the current project task tree before doing nontrivial work;
- record durable decisions, open questions, blockers, or completed work;
- update the visual task graph shown by `task-status/web.py`;
- render a compact context summary for another agent.
- construct a global or query-local state memory block from the current graph.

Do not ingest every chat turn. Ingest only information that should still matter
after the current conversation is gone.

## Core Model

Every node is an `Inquiry`:

- `content`: natural-language task/question/issue. It must be specific enough to
  recover context later.
- `description`: one concrete explanation of what the node means or should
  produce. Keep it at or below 50 characters.
- `status`: `candidate`, `ready`, `active`, `blocked`, or `done`.
- `answer`: optional resolution. For `done`, explain what was decided, solved,
  rejected, or deferred.

Edges:

- `decomposes_to`: broader inquiry to narrower inquiry.
- `depends_on`: source inquiry needs target inquiry resolved first.
- `relates_to`: weak useful relation. Use sparingly.

`decomposes_to` and `depends_on` are DAG-like strong relations and should not
create cycles.

## Writing Good Nodes

Prefer natural, context-restoring language over abstract taxonomy labels.

Weak:

```text
定义任务决策流程的职责。
```

Better:

```text
说明任务决策流程如何帮助 agent 判断下一步行动。
```

Add a short explanation:

```text
任务决策流程定义何时读代码、问用户、实现、验证或回滚。
```

Rules:

- `content` should say what problem the node is about, not just name a component.
- `description` should answer "what is this for?" or "what output should exist?"
- Avoid descriptions that merely repeat the title.
- For `done` nodes, write an `answer`; future agents should not need to infer the
  resolution from the title.
- For `blocked` nodes, make the missing dependency explicit in `answer` when known.

## Commands

Run commands from the repository root. Use the project Python:

```bash
/Users/duanyufi/anaconda3/bin/python
```

Initialize or migrate the local database:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/cli.py init
```

Render deterministic task memory:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/cli.py render \
  --conversation-file conversation.md \
  --deterministic \
  --output .tta-local-memory/TASK_STATUS.md
```

Render deterministic state memory from the graph:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/cli.py state --global
/Users/duanyufi/anaconda3/bin/python task-status/cli.py state \
  --query "任务决策流程怎么帮助 agent 判断下一步"
```

Use `state --global` when an agent needs top-level orientation. It returns the
root node and every node directly connected to root.

Use `state --query` when an agent has a natural-language task and needs local
context. It uses cached embeddings through the shared model infrastructure and
falls back to local token matching otherwise. It finds the most relevant node
while mildly favoring deeper nodes, then returns the shortest path from root to
that node and the matched node's direct `decomposes_to` children.

The default embedding provider/model is
`huiyan_openai_claude` / `text-embedding-3-small`. Prefer it unless quality is
visibly insufficient; switch to `text-embedding-3-large` for higher recall if the
provider exposes it. Do not use `text-embedding-ada-002` for new indexes unless
compatibility with an old index is required.

Refresh embeddings explicitly:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/cli.py embed \
  --embedding-provider huiyan_openai_claude \
  --embedding-model text-embedding-3-small
```

Ingest a conversation summary:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/cli.py ingest \
  --conversation-file conversation.md \
  --provider xiaomi \
  --model MiMo-V2.5-Pro \
  --min-importance 0.65 \
  --min-confidence 0.55 \
  --max-inquiries 8 \
  --dedupe-threshold 0.72
```

For API-backed experiments, use this fallback order and stop if all fail:

1. `xiaomi` / `MiMo-V2.5-Pro`
2. `deepseek` / `deepseek-v4-pro`
3. `huiyan_cn` / `gpt-5.5`

Start the graph viewer:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/web.py
```

Then open:

```text
http://127.0.0.1:8765
```

If that port is already occupied, start with another port:

```bash
/Users/duanyufi/anaconda3/bin/python task-status/web.py --port 8766
```

## Agent Workflow

Before substantial work:

1. If the request is broad, run `state --global`.
2. If the request is specific, run `state --query "<natural-language request>"`.
3. If you need a broader active/blocked/ready summary, render deterministic
   memory with the current user request or a short task summary as `conversation`.
4. Use the graph viewer if relationships or stale node wording are unclear.

During work:

1. Keep normal project edits separate from `.tta-local-memory/`.
2. Do not directly edit the SQLite database unless the CLI or web API cannot do
   the needed operation.
3. If graph labels are unclear, update both `content` and `description`; do not
   hide important context only in `answer`.

After substantial work:

1. Summarize durable outcomes, decisions, blockers, and next tasks in a short
   Markdown conversation file.
2. Ingest that file with the CLI.
3. Render deterministic memory once to verify the new state is useful.
4. If the viewer is open, refresh it and inspect any newly created or updated
   nodes.

## Data And Git Hygiene

- Default database: `.tta-local-memory/task-status.sqlite`.
- `.tta-local-memory/` is intentionally ignored by git.
- Source code, schema, prompts, and web viewer files under `task-status/` are
  tracked project files and should be reviewed like normal code.
- Do not commit local memory database contents.

## Quick Checks

Use these checks after modifying this module:

```bash
/Users/duanyufi/anaconda3/bin/python -m compileall task-status
/Users/duanyufi/anaconda3/bin/python task-status/cli.py init
/Users/duanyufi/anaconda3/bin/python task-status/cli.py render \
  --conversation "检查 task-status 是否还能渲染。" \
  --deterministic
/Users/duanyufi/anaconda3/bin/python task-status/cli.py state --global
/Users/duanyufi/anaconda3/bin/python task-status/cli.py state \
  --query "任务决策流程怎么帮助 agent 判断下一步" \
  --retrieval local
```
