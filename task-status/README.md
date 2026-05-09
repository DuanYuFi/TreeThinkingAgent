# Task Status

This module stores TTA task state as a SQLite-backed Inquiry DAG and renders a
compact Markdown memory for LLM context.

All nodes are `Inquiry` objects:

- `content`: the question, task, design issue, or root project inquiry.
- `status`: `candidate`, `ready`, `active`, `blocked`, or `done`.
- `answer`: optional result or resolution. For `done`, this should explain whether the inquiry was solved, rejected, or deferred.

Edges express structure:

- `decomposes_to`: a broader inquiry breaks into a narrower inquiry.
- `depends_on`: one inquiry requires another to be resolved first. This relation is kept acyclic.
- `relates_to`: weak useful relation. This relation is not part of DAG cycle checks.

`decomposes_to` and `depends_on` are treated as strong graph relations and are
kept acyclic independently.

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
  --max-inquiries 6
```

Render compact task-status memory:

```bash
python3 task-status/cli.py render \
  --conversation-file conversation.md \
  --deterministic \
  --output .tta-local-memory/TASK_STATUS.md
```

By default the database is `.tta-local-memory/task-status.sqlite`, so local task
state remains outside git.
