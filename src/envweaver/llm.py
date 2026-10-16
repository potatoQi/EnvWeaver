import httpx


def call(
    base_url: str,
    model_name: str,
    api_key: str,
    messages: list[dict],
    output_schema: dict | None = None,
    *,
    timeout: float = 120,
) -> dict:
    """从 Pipeline 侧调用 OpenAI 兼容 Chat Completions 服务。

    Args:
        base_url: API 前缀，例如 https://provider.example/v1；自动追加 /chat/completions。
        model_name: 服务提供的模型名称。
        api_key: Bearer 凭证；由调用方在运行时提供。
        messages: OpenAI 格式的消息列表，可包含服务支持的文字和图片内容。
        output_schema: 可选 JSON Schema，通过严格 json_schema 响应格式发送。
        timeout: HTTP 请求超时秒数，由 httpx 按连接、读写等阶段处理。

    Returns:
        完整响应字典，不自动抽取回答或解析模型生成的 JSON。

    Raises:
        httpx.HTTPStatusError: 服务返回 HTTP 错误状态。
        httpx.RequestError: 网络或超时错误。

    不自动重试。图片和结构化输出是否可用取决于模型服务。
    pi 自己调用模型，不经过本函数。
    """
    request = {"model": model_name, "messages": messages}
    if output_schema is not None:
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": output_schema},
        }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request,
        )
        response.raise_for_status()
        return response.json()
