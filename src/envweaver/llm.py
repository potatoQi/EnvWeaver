import httpx


def call(base_url, model_name, api_key, messages, output_schema=None, *, timeout=120):
    """Return the full OpenAI-compatible chat-completions response dictionary."""
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
