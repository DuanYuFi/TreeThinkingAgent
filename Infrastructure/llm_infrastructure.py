from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib import request


class APIStyle(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass(slots=True)
class ProviderDefinition:
    provider: str
    model_name: list[str]
    base_url: str
    api_key: str
    api_style: APIStyle

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProviderDefinition":
        required = ("provider", "model_name", "base_url", "api_key", "api_style")
        missing = [key for key in required if key not in payload or payload[key] is None]
        if missing:
            raise ValueError(f"Provider definition is missing required fields: {missing}")

        raw_models = payload["model_name"]
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError("`model_name` must be a non-empty list in each provider definition.")
        models = [str(item) for item in raw_models]

        raw_style = str(payload["api_style"]).lower()
        try:
            style = APIStyle(raw_style)
        except ValueError as exc:
            raise ValueError(f"Unsupported api_style: {payload['api_style']}") from exc

        return cls(
            provider=str(payload["provider"]),
            model_name=models,
            base_url=str(payload["base_url"]),
            api_key=_resolve_env_value(str(payload["api_key"])),
            api_style=style,
        )


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


@dataclass(slots=True)
class ChatSession:
    session_id: str
    context: list[ChatMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def append(self, role: str, content: str) -> ChatMessage:
        message = ChatMessage(role=role, content=content)
        self.context.append(message)
        return message


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class ChatResult:
    reply: str
    usage: TokenUsage


class ModelRegistry:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self._providers: dict[str, ProviderDefinition] = {}
        self.reload()

    def reload(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        records = config.get("providers", [])
        if not isinstance(records, list):
            raise ValueError("`providers` must be a list in model registry config.")

        providers: dict[str, ProviderDefinition] = {}
        for record in records:
            definition = ProviderDefinition.from_dict(record)
            if definition.provider in providers:
                raise ValueError(f"Duplicate provider found: {definition.provider}")
            providers[definition.provider] = definition

        self._providers = providers

    def get(self, provider: str, model_name: str) -> ProviderDefinition:
        if provider not in self._providers:
            available = ", ".join(sorted(self._providers.keys()))
            raise KeyError(f"Provider `{provider}` not found. Available: [{available}]")

        definition = self._providers[provider]
        if model_name not in definition.model_name:
            available_models = ", ".join(definition.model_name)
            raise KeyError(
                f"Model `{model_name}` not found under provider `{provider}`. "
                f"Available: [{available_models}]"
            )
        return definition

    def providers(self) -> list[str]:
        return sorted(self._providers.keys())

    def models_for_provider(self, provider: str) -> list[str]:
        if provider not in self._providers:
            available = ", ".join(sorted(self._providers.keys()))
            raise KeyError(f"Provider `{provider}` not found. Available: [{available}]")
        return list(self._providers[provider].model_name)


class LLMInfrastructure:
    """
    管理模型注册、会话上下文和请求发送的统一入口。
    """

    def __init__(self, model_registry: ModelRegistry) -> None:
        self.model_registry = model_registry
        self.sessions: dict[str, ChatSession] = {}

    def create_session(
        self,
        session_id: str | None = None,
        *,
        system_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatSession:
        resolved_session_id = session_id or str(uuid.uuid4())
        if resolved_session_id in self.sessions:
            raise ValueError(f"Session `{resolved_session_id}` already exists.")

        session = ChatSession(session_id=resolved_session_id, metadata=metadata or {})
        if system_prompt:
            session.append("system", system_prompt)
        self.sessions[resolved_session_id] = session
        return session

    def get_session(self, session_id: str) -> ChatSession:
        if session_id not in self.sessions:
            raise KeyError(f"Session `{session_id}` does not exist.")
        return self.sessions[session_id]

    def append_message(self, session_id: str, role: str, content: str) -> ChatMessage:
        session = self.get_session(session_id)
        return session.append(role=role, content=content)

    def build_request(
        self,
        provider: str,
        model_name: str,
        session_id: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> tuple[str, dict[str, str], bytes]:
        definition = self.model_registry.get(provider, model_name)
        session = self.get_session(session_id)

        if definition.api_style == APIStyle.OPENAI:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": msg.role, "content": msg.content}
                    for msg in session.context
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            headers = {
                "Authorization": f"Bearer {definition.api_key}",
                "Content-Type": "application/json",
            }
        elif definition.api_style == APIStyle.ANTHROPIC:
            system_messages = [msg.content for msg in session.context if msg.role == "system"]
            non_system_messages = [
                {"role": msg.role, "content": msg.content}
                for msg in session.context
                if msg.role != "system"
            ]

            payload = {
                "model": model_name,
                "messages": non_system_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_messages:
                payload["system"] = "\n\n".join(system_messages)

            headers = {
                "x-api-key": definition.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        else:
            raise ValueError(f"Unsupported API style: {definition.api_style}")

        body = json.dumps(payload).encode("utf-8")
        return definition.base_url, headers, body

    def chat_once(
        self,
        provider: str,
        model_name: str,
        session_id: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout_seconds: int = 120,
    ) -> str:
        result = self.chat_once_with_usage(
            provider=provider,
            model_name=model_name,
            session_id=session_id,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
        return result.reply

    def chat_once_with_usage(
        self,
        provider: str,
        model_name: str,
        session_id: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout_seconds: int = 120,
    ) -> ChatResult:
        self.append_message(session_id, "user", user_message)
        url, headers, body = self.build_request(
            provider,
            model_name,
            session_id,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        http_request = request.Request(url=url, headers=headers, method="POST", data=body)
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        reply = self._extract_reply_text(
            self.model_registry.get(provider, model_name).api_style, payload
        )
        usage = self._extract_usage(
            self.model_registry.get(provider, model_name).api_style, payload
        )
        self.append_message(session_id, "assistant", reply)
        return ChatResult(reply=reply, usage=usage)

    @staticmethod
    def _extract_reply_text(api_style: APIStyle, payload: dict[str, Any]) -> str:
        if api_style == APIStyle.OPENAI:
            return payload["choices"][0]["message"]["content"]

        if api_style == APIStyle.ANTHROPIC:
            blocks = payload.get("content", [])
            texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
            if not texts:
                raise ValueError("No text block found in Anthropic response.")
            return "".join(texts)

        raise ValueError(f"Unsupported API style: {api_style}")

    @staticmethod
    def _extract_usage(api_style: APIStyle, payload: dict[str, Any]) -> TokenUsage:
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            return TokenUsage()

        if api_style == APIStyle.OPENAI:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            return TokenUsage(
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
            )

        if api_style == APIStyle.ANTHROPIC:
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
            return TokenUsage(
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
            )

        return TokenUsage()


def _resolve_env_value(raw_value: str) -> str:
    """
    支持 `${ENV_VAR}` 格式的 API key 配置。
    """
    if raw_value.startswith("${") and raw_value.endswith("}"):
        env_name = raw_value[2:-1].strip()
        if not env_name:
            raise ValueError("Invalid env var placeholder for api_key.")
        resolved = os.getenv(env_name)
        if not resolved:
            raise ValueError(f"Environment variable `{env_name}` is not set.")
        return resolved
    return raw_value

