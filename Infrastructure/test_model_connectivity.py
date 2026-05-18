"""
API 连通性测试：对 model_registry.json 中每个已配置 API key 的供应商下的每个模型发起一次最小对话请求。

运行（在项目根目录）::

    /Users/duanyufi/anaconda3/bin/python -m unittest Infrastructure.test_model_connectivity -v
    /Users/duanyufi/anaconda3/bin/python Infrastructure/test_model_connectivity.py --provider huiyan_cn -v
    /Users/duanyufi/anaconda3/bin/python Infrastructure/test_model_connectivity.py --provider huiyan_openai_claude --model text-embedding-3-small -v

也可以在 unittest 入口下用环境变量筛选供应商::

    TTA_TEST_PROVIDER=huiyan_cn /Users/duanyufi/anaconda3/bin/python -m unittest Infrastructure.test_model_connectivity -v

需要：对应供应商的环境变量已设置（见 registry 中 api_key 占位符）、网络可达。
未设置 key 的供应商会从测试中排除（临时生成的 registry 不含这些条目）。
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
import uuid
from argparse import ArgumentParser
from pathlib import Path
from urllib.error import HTTPError

try:
    from .llm_infrastructure import LLMInfrastructure, ModelRegistry
except ImportError:  # pragma: no cover - direct script execution fallback
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from Infrastructure.llm_infrastructure import LLMInfrastructure, ModelRegistry


def _split_names(raw: str) -> set[str]:
    return {name.strip() for name in raw.split(",") if name.strip()}


def _provider_filter_from_env() -> set[str] | None:
    raw = os.getenv("TTA_TEST_PROVIDERS") or os.getenv("TTA_TEST_PROVIDER") or ""
    selected = _split_names(raw)
    return selected or None


def _model_filter_from_env() -> set[str] | None:
    raw = os.getenv("TTA_TEST_MODELS") or os.getenv("TTA_TEST_MODEL") or ""
    selected = _split_names(raw)
    return selected or None


def _is_embedding_model(model_name: str) -> bool:
    return "embedding" in model_name.lower()


def _api_key_available(raw: str) -> bool:
    """与 llm_infrastructure._resolve_env_value 一致的占位符规则，但不抛错。"""
    if raw.startswith("${") and raw.endswith("}"):
        env_name = raw[2:-1].strip()
        if not env_name:
            return False
        return bool(os.getenv(env_name))
    return bool(raw)


def _filtered_registry(
    path: Path,
    provider_filter: set[str] | None = None,
    model_filter: set[str] | None = None,
) -> tuple[dict, list[str]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("providers", [])
    if not isinstance(records, list):
        raise ValueError("`providers` must be a list")

    configured_providers = {str(record.get("provider", "")) for record in records}
    if provider_filter:
        unknown = provider_filter - configured_providers
        if unknown:
            available = ", ".join(sorted(configured_providers))
            requested = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown provider filter: {requested}. Available: [{available}]")

    configured_models: set[str] = set()
    for record in records:
        prov = str(record.get("provider", ""))
        if provider_filter and prov not in provider_filter:
            continue
        raw_models = record.get("model_name", [])
        if isinstance(raw_models, list):
            configured_models.update(str(model) for model in raw_models)
    if model_filter:
        unknown = model_filter - configured_models
        if unknown:
            available = ", ".join(sorted(configured_models))
            requested = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown model filter: {requested}. Available: [{available}]")

    kept: list[dict] = []
    skipped_providers: list[str] = []
    for record in records:
        prov = str(record.get("provider", ""))
        if provider_filter and prov not in provider_filter:
            continue

        key_field = record.get("api_key", "")
        if not _api_key_available(str(key_field)):
            skipped_providers.append(prov or "(unknown)")
            continue

        filtered_record = dict(record)
        if model_filter:
            filtered_record["model_name"] = [
                str(model)
                for model in record.get("model_name", [])
                if str(model) in model_filter
            ]
            if not filtered_record["model_name"]:
                continue
        kept.append(filtered_record)

    return {"providers": kept}, skipped_providers


def _configure_cli_args(argv: list[str]) -> list[str]:
    parser = ArgumentParser(add_help=False)
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Only test one provider. Can be passed multiple times.",
    )
    parser.add_argument(
        "--providers",
        default="",
        help="Comma-separated provider list, for example: huiyan_cn,deepseek.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Only test one model. Can be passed multiple times.",
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated model list, for example: text-embedding-3-small,gpt-5.4.",
    )
    parser.add_argument(
        "--help-connectivity",
        action="store_true",
        help="Show connectivity-test-specific arguments and exit.",
    )
    args, remaining = parser.parse_known_args(argv)

    if args.help_connectivity:
        parser.print_help()
        raise SystemExit(0)

    selected: set[str] = set()
    for provider in args.provider:
        selected.update(_split_names(provider))
    selected.update(_split_names(args.providers))
    if selected:
        TestModelRegistryConnectivity.provider_filter = selected

    selected_models: set[str] = set()
    for model in args.model:
        selected_models.update(_split_names(model))
    selected_models.update(_split_names(args.models))
    if selected_models:
        TestModelRegistryConnectivity.model_filter = selected_models

    return remaining


class TestModelRegistryConnectivity(unittest.TestCase):
    """针对 registry 中每个模型做一次最小 POST 请求以验证联通性。"""

    registry_path = Path(__file__).resolve().parent / "model_registry.json"
    provider_filter: set[str] | None = _provider_filter_from_env()
    model_filter: set[str] | None = _model_filter_from_env()
    _tmp_path: str | None = None
    skipped_no_key: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        payload, cls.skipped_no_key = _filtered_registry(
            cls.registry_path,
            cls.provider_filter,
            cls.model_filter,
        )
        if not payload["providers"]:
            filter_note = ""
            if cls.provider_filter:
                filter_note = f" provider filter={sorted(cls.provider_filter)};"
            if cls.model_filter:
                filter_note += f" model filter={sorted(cls.model_filter)};"
            raise unittest.SkipTest(
                "model_registry.json 中没有任何供应商能解析 API key；"
                + filter_note
                + " 请在 .env 中设置对应环境变量。"
            )

        fd, cls._tmp_path = tempfile.mkstemp(suffix=".json", prefix="model_registry_filtered_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp)

            cls.registry = ModelRegistry(cls._tmp_path)
            cls.infra = LLMInfrastructure(cls.registry)
        except Exception:
            if cls._tmp_path and os.path.isfile(cls._tmp_path):
                os.unlink(cls._tmp_path)
                cls._tmp_path = None
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._tmp_path and os.path.isfile(cls._tmp_path):
            os.unlink(cls._tmp_path)

    def test_all_models_reachable(self) -> None:
        failures: list[tuple[str, str, str]] = []
        checked = 0
        checked_chat = 0
        checked_embedding = 0
        ping = "Reply with exactly one word: OK"

        for provider in self.registry.providers():
            for model_name in self.registry.models_for_provider(provider):
                checked += 1
                try:
                    if _is_embedding_model(model_name):
                        checked_embedding += 1
                        result = self.infra.embed_texts_with_usage(
                            provider,
                            model_name,
                            ["TTA connectivity embedding probe."],
                            timeout_seconds=180,
                        )
                        vector = result.embeddings[0] if result.embeddings else []
                        if not vector:
                            failures.append((provider, model_name, "empty embedding vector"))
                        elif not all(math.isfinite(value) for value in vector):
                            failures.append(
                                (provider, model_name, "embedding vector contains non-finite values")
                            )
                    else:
                        checked_chat += 1
                        session_id = str(uuid.uuid4())
                        self.infra.create_session(session_id)
                        result = self.infra.chat_once_with_usage(
                            provider,
                            model_name,
                            session_id,
                            ping,
                            max_tokens=128,
                            temperature=None,
                            timeout_seconds=180,
                        )
                        if not (result.reply or "").strip():
                            failures.append((provider, model_name, "empty assistant reply"))
                except HTTPError as exc:
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:800]
                    except Exception:
                        detail = ""
                    msg = f"{type(exc).__name__}: {exc}"
                    if detail.strip():
                        msg += f" | body: {detail.strip()}"
                    failures.append((provider, model_name, msg))
                except Exception as exc:
                    failures.append((provider, model_name, f"{type(exc).__name__}: {exc}"))

        prefix = ""
        if self.provider_filter:
            prefix += "本次仅检查供应商: " + ", ".join(sorted(self.provider_filter)) + "\n\n"
        if self.model_filter:
            prefix += "本次仅检查模型: " + ", ".join(sorted(self.model_filter)) + "\n\n"
        if self.skipped_no_key:
            prefix += (
                "以下供应商因缺少 API key 未纳入本次检查: "
                + ", ".join(sorted(set(self.skipped_no_key)))
                + "\n\n"
            )
        self.assertGreater(checked, 0, "没有任何模型被纳入本次联通性检查。")
        if failures:
            self.fail(
                prefix
                + f"已实际请求 {checked} 个模型"
                + f"（chat={checked_chat}, embedding={checked_embedding}）；"
                + "下列模型联通性检查失败:\n"
                + "\n".join(f"  - {p} / {m}: {err}" for p, m, err in failures)
            )


if __name__ == "__main__":
    sys.argv = [sys.argv[0], *_configure_cli_args(sys.argv[1:])]
    unittest.main()
