# Task Status

This module stores TTA task state as a SQLite-backed Inquiry DAG and renders a
compact Markdown memory for LLM context.

All nodes are `Inquiry` objects:

- `content`: the question, task, design issue, or root project inquiry.
- `description`: one concrete explanation for the graph node, capped at 50 characters.
- `status`: `candidate`, `ready`, `active`, `blocked`, or `done`.
- `answer`: optional result or resolution. For `done`, this should explain whether the inquiry was solved, rejected, or deferred.

Edges express structure:

- `decomposes_to`: a broader inquiry breaks into a narrower inquiry.
- `depends_on`: one inquiry requires another to be resolved first. This relation is kept acyclic.
- `relates_to`: weak useful relation. This relation is not part of DAG cycle checks.

`decomposes_to` and `depends_on` are treated as strong graph relations and are
kept acyclic independently.

Each project has at most one root Inquiry. The viewer renders the root as a gold
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
  --min-importance 0.7 \
  --min-confidence 0.6 \
  --max-inquiries 6 \
  --dedupe-threshold 0.72
```

During ingest, candidates are filtered twice: first by importance/confidence, then
against existing inquiries. Exact matches update the existing inquiry; related
matches above `--dedupe-threshold` are mapped to the existing inquiry and are not
inserted again.

Render compact task-status memory:

```bash
python3 task-status/cli.py render \
  --conversation-file conversation.md \
  --deterministic \
  --output .tta-local-memory/TASK_STATUS.md
```

By default the database is `.tta-local-memory/task-status.sqlite`, so local task
state remains outside git.

Render deterministic state memory from the current graph:

```bash
python3 task-status/cli.py state --global
python3 task-status/cli.py state --query "任务决策流程怎么帮助 agent 判断下一步"
```

`state --global` returns the root Inquiry and every node directly connected to it.
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

The viewer reads the same SQLite database, draws the current Inquiry graph, and
shows node details when a node is clicked.
