from __future__ import annotations

import argparse
import sys
from pathlib import Path

from llm_client import complete_with_infrastructure, extract_json_object
from prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    MEMORY_SYSTEM_PROMPT,
    build_extraction_user_prompt,
    build_memory_user_prompt,
)
from render import render_memory_from_snapshot
from store import (
    DEFAULT_DB_PATH,
    connect,
    graph_snapshot,
    init_db,
    parse_extraction_payload,
    upsert_inquiries,
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

    conversation = read_conversation(args)
    if args.command == "ingest":
        return ingest(args, conn, conversation)
    if args.command == "render":
        return render(args, conn, conversation)

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite-backed Inquiry DAG for TTA task-status memory."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path. Defaults to .tta-local-memory/task-status.sqlite.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create or migrate the task-status database.")

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Use an LLM to parse a conversation and insert high-signal inquiries.",
    )
    add_conversation_args(ingest_parser)
    add_llm_args(ingest_parser)
    ingest_parser.add_argument("--source-label", default=None)
    ingest_parser.add_argument("--min-importance", type=float, default=0.65)
    ingest_parser.add_argument("--min-confidence", type=float, default=0.55)
    ingest_parser.add_argument("--max-inquiries", type=int, default=8)

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
    return parser


def add_conversation_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--conversation", help="Conversation text.")
    source.add_argument("--conversation-file", help="Path to a UTF-8 conversation file.")
    source.add_argument("--stdin", action="store_true", help="Read conversation from stdin.")


def add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="huiyan_cn")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.1)


def read_conversation(args: argparse.Namespace) -> str:
    if getattr(args, "conversation", None):
        return str(args.conversation)
    if getattr(args, "conversation_file", None):
        return Path(args.conversation_file).read_text(encoding="utf-8")
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    raise ValueError("No conversation source provided.")


def ingest(args: argparse.Namespace, conn, conversation: str) -> int:
    user_prompt = build_extraction_user_prompt(
        conversation,
        min_importance=args.min_importance,
        min_confidence=args.min_confidence,
        max_inquiries=args.max_inquiries,
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
    inquiries, edges = parse_extraction_payload(payload)
    result = upsert_inquiries(
        conn,
        inquiries,
        edges,
        conversation=conversation,
        source_label=args.source_label,
        provider=args.provider,
        model_name=args.model,
        min_importance=args.min_importance,
        min_confidence=args.min_confidence,
        max_inquiries=args.max_inquiries,
    )
    print_json(result)
    return 0


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


def print_json(payload: dict) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
