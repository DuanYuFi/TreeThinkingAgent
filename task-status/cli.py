from __future__ import annotations

import argparse
import sys
from pathlib import Path

from embeddings import (
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingUnavailableError,
    embedding_config_from_env,
    refresh_embeddings,
)
from llm_client import complete_with_infrastructure, extract_json_object
from prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    MEMORY_SYSTEM_PROMPT,
    build_extraction_user_prompt,
    build_memory_user_prompt,
)
from render import render_memory_from_snapshot
from state_memory import render_global_state_memory, render_local_state_memory
from store import (
    DEFAULT_DB_PATH,
    connect,
    graph_snapshot,
    init_db,
    parse_extraction_payload,
    preview_task_changes,
    upsert_tasks,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    conn = connect(db_path)
    init_db(conn)

    if args.command == "init":
        print(f"Initialized task-status database: {db_path}")
        return 0

    if args.command == "ingest":
        conversation = read_conversation(args)
        return ingest(args, conn, conversation)
    if args.command == "preview":
        conversation = read_conversation(args)
        return preview(args, conn, conversation)
    if args.command == "render":
        conversation = read_conversation(args)
        return render(args, conn, conversation)
    if args.command == "state":
        return state(args, conn)
    if args.command == "embed":
        return embed(args, conn)

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite-backed Task DAG for GTA task-status memory."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path. Defaults to .gta-local-memory/task-status.sqlite.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create or migrate the task-status database.")

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Use an LLM to parse a conversation and insert high-signal tasks.",
    )
    add_conversation_args(ingest_parser)
    add_llm_args(ingest_parser)
    ingest_parser.add_argument("--source-label", default=None)
    ingest_parser.add_argument(
        "--min-importance",
        choices=("low", "medium", "high"),
        default="medium",
        help="Minimum task priority to ingest.",
    )
    ingest_parser.add_argument("--min-confidence", type=float, default=0.55)
    ingest_parser.add_argument("--max-tasks", type=int, default=8)
    ingest_parser.add_argument(
        "--dedupe-threshold",
        type=float,
        default=0.72,
        help="Similarity threshold for skipping related existing tasks.",
    )

    preview_parser = subparsers.add_parser(
        "preview",
        help="Use an LLM to parse tasks and preview changes without writing them.",
    )
    add_conversation_args(preview_parser)
    add_llm_args(preview_parser)
    preview_parser.add_argument(
        "--min-importance",
        choices=("low", "medium", "high"),
        default="medium",
        help="Minimum task priority to preview.",
    )
    preview_parser.add_argument("--min-confidence", type=float, default=0.55)
    preview_parser.add_argument("--max-tasks", type=int, default=8)
    preview_parser.add_argument(
        "--dedupe-threshold",
        type=float,
        default=0.72,
        help="Similarity threshold for marking related existing tasks as duplicates.",
    )

    render_parser = subparsers.add_parser(
        "render",
        help="Build concise Markdown task-status memory from a conversation and database.",
    )
    add_conversation_args(render_parser)
    add_llm_args(render_parser)
    render_parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Use local deterministic rendering instead of LLM rendering.",
    )
    render_parser.add_argument("--limit", type=int, default=40)
    render_parser.add_argument("--output", default=None)

    state_parser = subparsers.add_parser(
        "state",
        help="Render deterministic state memory from the current Task DAG.",
    )
    state_mode = state_parser.add_mutually_exclusive_group(required=True)
    state_mode.add_argument(
        "--global",
        dest="global_state",
        action="store_true",
        help="Render root plus nodes directly connected to root.",
    )
    state_mode.add_argument(
        "--query",
        help="Render local memory for the best matching node and its nearby graph context.",
    )
    state_parser.add_argument("--project-id", default="default")
    state_parser.add_argument("--min-score", type=float, default=0.08)
    state_parser.add_argument(
        "--retrieval",
        choices=("auto", "embedding", "local"),
        default="auto",
        help="auto uses embeddings when available and falls back to local token matching.",
    )
    add_embedding_args(state_parser)
    state_parser.add_argument(
        "--no-refresh-embeddings",
        action="store_true",
        help="Do not refresh missing or stale node embeddings before query matching.",
    )
    state_parser.add_argument("--output", default=None)

    embed_parser = subparsers.add_parser(
        "embed",
        help="Build or refresh cached node embeddings for state queries.",
    )
    add_embedding_args(embed_parser)
    embed_parser.add_argument("--force", action="store_true")
    embed_parser.add_argument("--batch-size", type=int, default=64)
    return parser


def add_conversation_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--conversation", help="Conversation text.")
    source.add_argument("--conversation-file", help="Path to a UTF-8 conversation file.")
    source.add_argument("--stdin", action="store_true", help="Read conversation from stdin.")


def add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="huiyan_openai_claude")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.1)


def add_embedding_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--embedding-provider", default=DEFAULT_EMBEDDING_PROVIDER)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--embedding-dimensions", type=int, default=None)


def read_conversation(args: argparse.Namespace) -> str:
    if getattr(args, "conversation", None):
        return str(args.conversation)
    if getattr(args, "conversation_file", None):
        return Path(args.conversation_file).read_text(encoding="utf-8")
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    raise ValueError("No conversation source provided.")


def ingest(args: argparse.Namespace, conn, conversation: str) -> int:
    tasks, edges, _payload = extract_task_candidates(args, conversation)
    result = upsert_tasks(
        conn,
        tasks,
        edges,
        conversation=conversation,
        source_label=args.source_label,
        provider=args.provider,
        model_name=args.model,
        min_importance=args.min_importance,
        min_confidence=args.min_confidence,
        max_tasks=args.max_tasks,
        dedupe_threshold=args.dedupe_threshold,
    )
    print_json(result)
    return 0


def preview(args: argparse.Namespace, conn, conversation: str) -> int:
    tasks, edges, payload = extract_task_candidates(args, conversation)
    result = preview_task_changes(
        conn,
        tasks,
        edges,
        conversation=conversation,
        min_importance=args.min_importance,
        min_confidence=args.min_confidence,
        max_tasks=args.max_tasks,
        dedupe_threshold=args.dedupe_threshold,
    )
    print_json(
        {
            "mode": "dry_run",
            "provider": args.provider,
            "model": args.model,
            "raw_payload": payload,
            "preview": result,
        }
    )
    return 0


def extract_task_candidates(
    args: argparse.Namespace,
    conversation: str,
) -> tuple[list, list, dict]:
    user_prompt = build_extraction_user_prompt(
        conversation,
        min_importance=args.min_importance,
        min_confidence=args.min_confidence,
        max_tasks=args.max_tasks,
        dedupe_threshold=args.dedupe_threshold,
    )
    reply = complete_with_infrastructure(
        provider=args.provider,
        model_name=args.model,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    payload = extract_json_object(reply)
    tasks, edges = parse_extraction_payload(payload)
    return tasks, edges, payload


def render(args: argparse.Namespace, conn, conversation: str) -> int:
    snapshot = graph_snapshot(conn, conversation=conversation, limit=args.limit)
    if args.deterministic:
        markdown = render_memory_from_snapshot(snapshot)
    else:
        reply = complete_with_infrastructure(
            provider=args.provider,
            model_name=args.model,
            system_prompt=MEMORY_SYSTEM_PROMPT,
            user_prompt=build_memory_user_prompt(conversation, snapshot),
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
        markdown = reply.strip() + "\n"

    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    return 0


def state(args: argparse.Namespace, conn) -> int:
    if args.global_state:
        markdown = render_global_state_memory(conn, project_id=args.project_id)
    else:
        embedding_config = embedding_config_from_env(
            provider=args.embedding_provider,
            model=args.embedding_model,
            dimensions=args.embedding_dimensions,
        )
        try:
            markdown = render_local_state_memory(
                conn,
                query=args.query,
                project_id=args.project_id,
                min_score=args.min_score,
                retrieval=args.retrieval,
                embedding_config=embedding_config,
                refresh_embedding_index=not args.no_refresh_embeddings,
            )
        except EmbeddingUnavailableError as exc:
            print(f"Embedding unavailable: {exc}", file=sys.stderr)
            return 2

    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    return 0


def embed(args: argparse.Namespace, conn) -> int:
    config = embedding_config_from_env(
        provider=args.embedding_provider,
        model=args.embedding_model,
        dimensions=args.embedding_dimensions,
    )
    try:
        result = refresh_embeddings(
            conn,
            config=config,
            force=args.force,
            batch_size=args.batch_size,
        )
    except EmbeddingUnavailableError as exc:
        print(f"Embedding unavailable: {exc}", file=sys.stderr)
        return 2
    print_json(
        {
            **result,
            "provider": config.provider,
            "model": config.model,
            "dimensions": config.dimensions,
        }
    )
    return 0


def print_json(payload: dict) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
