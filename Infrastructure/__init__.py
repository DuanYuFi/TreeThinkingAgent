from pathlib import Path

from dotenv import load_dotenv

# 从项目根目录 .env 注入环境变量，供 model_registry 中 ${VAR} 解析
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .llm_infrastructure import APIStyle, LLMInfrastructure, ModelRegistry, ProviderDefinition

__all__ = ["APIStyle", "LLMInfrastructure", "ModelRegistry", "ProviderDefinition"]
