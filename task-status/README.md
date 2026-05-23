# Task Status

This module stores GTA task state as a SQLite-backed Task DAG and renders a
compact Markdown memory for LLM context.

All nodes are `Task` objects:

- `name`: the simplest word or phrase describing the task.
- `description`: plain-language context, expected output, or decision criterion, capped at 50 characters.
- `status`: `candidate`, `ready`, `active`, `blocked`, or `done`.
- `notes`: current notes. For blocked tasks, briefly record blocker information.
- `importance`: `low`, `medium`, or `high`; use `medium` by default unless the task is clearly low or high priority.

Edges express structure:

- `decomposes_to`: a broader task breaks into a narrower task.
- `depends_on`: one task requires another to be resolved first. This relation is kept acyclic.
- `relates_to`: weak useful relation. This relation is not part of DAG cycle checks.

`decomposes_to` and `depends_on` are treated as strong graph relations and are
kept acyclic independently.

Each project has at most one root Task. The viewer renders the root as a gold
node fixed at the graph center; it cannot be dragged.

## Usage

Initialize the local database:

```bash
python3 task-status/cli.py init
```

Ingest a conversation with an LLM:

```bash
python3 task-status/cli.py ingest \
  --conversation-file conversation.md \
  --provider huiyan_cn \
  --model gpt-5.4 \
  --min-importance medium \
  --min-confidence 0.6 \
  --max-tasks 6 \
  --dedupe-threshold 0.72
```

Preview what an ingest would do without inserting or updating tasks:

```bash
python3 task-status/cli.py preview \
  --conversation "把自然语言创建 task-status 节点做成 dry-run 测试接口" \
  --provider xiaomi \
  --model MiMo-V2.5-Pro
```

The preview output includes the raw extracted tasks plus a dry-run plan showing
which nodes would be inserted, updated, skipped as duplicates, or filtered out, and
which existing node or threshold each decision is based on.

During ingest, candidates are filtered twice: first by importance/confidence, then
against existing tasks. Exact matches update the existing task; related
matches above `--dedupe-threshold` are mapped to the existing task and are not
inserted again.

Render compact task-status memory:

```bash
python3 task-status/cli.py render \
  --conversation-file conversation.md \
  --deterministic \
  --output .gta-local-memory/TASK_STATUS.md
```

By default the database is `.gta-local-memory/task-status.sqlite`, so local task
state remains outside git.

Render deterministic state memory from the current graph:

```bash
python3 task-status/cli.py state --global
python3 task-status/cli.py state --query "任务决策流程怎么帮助 agent 判断下一步"
```

`state --global` returns the root Task and every node directly connected to it.
`state --query` uses cached embeddings through the shared model infrastructure,
falls back to local token matching otherwise, mildly favors deeper graph nodes
among relevant matches, then returns the shortest root-to-match path plus the
matched node's direct `decomposes_to` children. The default embedding provider and
model are `huiyan_openai_claude` / `text-embedding-3-small`.

Refresh embeddings explicitly:

```bash
python3 task-status/cli.py embed \
  --embedding-provider huiyan_openai_claude \
  --embedding-model text-embedding-3-small
```

## Graph Viewer

Start the local read-only graph viewer:

```bash
python3 task-status/web.py
```

Then open:

```text
http://127.0.0.1:8765
```

The viewer reads the same SQLite database, draws the current Task graph, and
shows node details when a node is clicked.

The viewer also exposes a local dry-run API for testing natural-language ingest
without writing tasks or edges:

```bash
curl -s http://127.0.0.1:8765/api/preview-ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "把自然语言创建 task-status 节点做成 dry-run 测试接口",
    "provider": "xiaomi",
    "model": "MiMo-V2.5-Pro"
  }'
```
