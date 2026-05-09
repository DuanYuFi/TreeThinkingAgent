from __future__ import annotations

import json
import sys
from pathlib import Path


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def complete_with_infrastructure(
    *,
    provider: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    load_dotenv_if_available()
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from Infrastructure.llm_infrastructure import LLMInfrastructure, ModelRegistry

    registry = ModelRegistry(str(root / "Infrastructure" / "model_registry.json"))
    infra = LLMInfrastructure(registry)
    session = infra.create_session(system_prompt=system_prompt)
    return infra.chat_once(
        provider=provider,
        model_name=model_name,
        session_id=session.session_id,
        user_message=user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM response does not contain a JSON object.")
    return json.loads(stripped[start : end + 1])
