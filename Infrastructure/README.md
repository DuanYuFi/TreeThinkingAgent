# Infrastructure

本目录提供一个可扩展的大模型基础设施，目标是支持后续「两个不同模型进行对话」实验。

## 1) 模型配置文件

使用 `model_registry.json` 管理 provider 定义。每个 provider 包含：

- `provider`：供应商名称（调用时需要指定）
- `model_name`：模型名称列表（调用时还需要指定具体模型）
- `base_url`：接口地址
- `api_key`：密钥（推荐使用 `${ENV_VAR}`）
- `api_style`：`openai` 或 `anthropic`

## 2) 模型与会话管理类

`llm_infrastructure.py` 提供：

- `ModelRegistry`：加载和校验模型配置
- `LLMInfrastructure`：管理会话、上下文、请求构造和单轮调用
- `ChatSession`：保存某个 session 的上下文消息

## 3) 最小用法示例

```python
from Infrastructure.llm_infrastructure import ModelRegistry, LLMInfrastructure

registry = ModelRegistry("Infrastructure/model_registry.json")
infra = LLMInfrastructure(registry)

session = infra.create_session(
    session_id="demo",
    system_prompt="You are a thoughtful podcast host."
)

reply = infra.chat_once(
    provider="huiyan_cn",
    model_name="gpt-5.4",
    session_id=session.session_id,
    user_message="Hello."
)
print(reply)
```

> 注意：如果 `api_key` 使用 `${OPENAI_API_KEY}` 这类占位符，请先在环境变量中设置对应 key。
