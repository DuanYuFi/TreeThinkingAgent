"""
API 连通性测试：对 model_registry.json 中每个已配置 API key 的供应商下的每个模型发起一次最小对话请求。

运行（在项目根目录）::

    /Users/duanyufi/anaconda3/bin/python -m unittest Infrastructure.test_model_connectivity -v

需要：对应供应商的环境变量已设置（见 registry 中 api_key 占位符）、网络可达。
未设置 key 的供应商会从测试中排除（临时生成的 registry 不含这些条目）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError

import Infrastructure  # noqa: F401 — 触发包内 load_dotenv

from Infrastructure import LLMInfrastructure, ModelRegistry


def _api_key_available(raw: str) -> bool:
    """与 llm_infrastructure._resolve_env_value 一致的占位符规则，但不抛错。"""
    if raw.startswith("${") and raw.endswith("}"):
        env_name = raw[2:-1].strip()
        if not env_name:
            return False
        return bool(os.getenv(env_name))
    return bool(raw)


def _filtered_registry(path: Path) -> tuple[dict, list[str]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("providers", [])
    if not isinstance(records, list):
        raise ValueError("`providers` must be a list")

    kept: list[dict] = []
    skipped_providers: list[str] = []
    for record in records:
        prov = str(record.get("provider", ""))
        key_field = record.get("api_key", "")
        if not _api_key_available(str(key_field)):
            skipped_providers.append(prov or "(unknown)")
            continue
        kept.append(record)

    return {"providers": kept}, skipped_providers


class TestModelRegistryConnectivity(unittest.TestCase):
    """针对 registry 中每个模型做一次最小 POST 请求以验证联通性。"""

    registry_path = Path(__file__).resolve().parent / "model_registry.json"
    _tmp_path: str | None = None
    skipped_no_key: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        payload, cls.skipped_no_key = _filtered_registry(cls.registry_path)
        if not payload["providers"]:
            raise unittest.SkipTest(
                "model_registry.json 中没有任何供应商能解析 API key；请在 .env 中设置对应环境变量。"
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
        ping = "Reply with exactly one word: OK"

        for provider in self.registry.providers():
            for model_name in self.registry.models_for_provider(provider):
                session_id = str(uuid.uuid4())
                try:
                    self.infra.create_session(session_id)
                    result = self.infra.chat_once_with_usage(
                        provider,
                        model_name,
                        session_id,
                        ping,
                        max_tokens=32,
                        temperature=0.0,
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
        if self.skipped_no_key:
            prefix = (
                "以下供应商因缺少 API key 未纳入本次检查: "
                + ", ".join(sorted(set(self.skipped_no_key)))
                + "\n\n"
            )
        self.assertEqual(
            failures,
            [],
            prefix
            + "下列模型联通性检查失败:\n"
            + "\n".join(f"  - {p} / {m}: {err}" for p, m, err in failures),
        )


if __name__ == "__main__":
    unittest.main()
