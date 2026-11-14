"""Text-only Responses adapter. Credentials and provider bodies never enter errors."""
import json
from typing import Any

import httpx

from app.core.config import Settings


class ModelGatewayError(RuntimeError):
    pass


class ResponsesClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def payload(self, messages: list[dict[str, str]], *, max_tokens: int, json_mode: bool = False, stream: bool = False) -> dict[str, Any]:
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] in {"system", "developer"})
        if not instructions:
            instructions = "Answer the user's question directly and safely."
        if json_mode:
            instructions += "\nReturn only the requested JSON object, without Markdown fences."
        items = []
        for message in messages:
            if message["role"] in {"system", "developer"}:
                continue
            role = message["role"]
            if role not in {"user", "assistant"}:
                raise ModelGatewayError("Unsupported message role")
            items.append({"type": "message", "role": role, "content": [{
                "type": "output_text" if role == "assistant" else "input_text", "text": message["content"],
            }]})
        effort = self.settings.openai_reasoning_effort
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ModelGatewayError("Unsupported reasoning effort")
        return {
            "model": self.settings.openai_model,
            "instructions": instructions,
            "input": items,
            "reasoning": {"effort": effort},
            "max_output_tokens": max(max_tokens, self.settings.openai_min_output_tokens),
            "store": False,
            "stream": stream,
        }

    @property
    def url(self) -> str:
        return self.settings.openai_base_url.rstrip("/") + "/responses"

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.openai_api_key.strip():
            raise ModelGatewayError("Model gateway is not configured")
        return {"Authorization": "Bearer " + self.settings.openai_api_key, "Content-Type": "application/json"}

    def parse(self, payload: Any) -> str:
        if not isinstance(payload, dict) or payload.get("status") != "completed" or payload.get("error"):
            raise ModelGatewayError("Model response was not completed")
        texts = []
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise ModelGatewayError("Model declined the request")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        text = "".join(texts)
        if not text.strip() or len(text) > self.settings.openai_max_output_chars:
            raise ModelGatewayError("Model response was empty or exceeded the output limit")
        return text

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int, json_mode: bool = False) -> str:
        try:
            response = httpx.post(self.url, headers=self.headers, json=self.payload(messages, max_tokens=max_tokens, json_mode=json_mode), timeout=self.settings.openai_timeout_seconds)
            if response.status_code >= 400:
                raise ModelGatewayError("Model gateway returned HTTP " + str(response.status_code))
            return self.parse(response.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError("Model gateway request or response failed") from exc

    async def complete_async(self, messages: list[dict[str, str]], *, max_tokens: int, json_mode: bool = False) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.settings.openai_timeout_seconds) as client:
                response = await client.post(self.url, headers=self.headers, json=self.payload(messages, max_tokens=max_tokens, json_mode=json_mode))
            if response.status_code >= 400:
                raise ModelGatewayError("Model gateway returned HTTP " + str(response.status_code))
            return self.parse(response.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError("Model gateway request or response failed") from exc

    async def stream(self, messages: list[dict[str, str]], *, max_tokens: int):
        # Buffer until a completed envelope: truncated streams never reach students.
        completed = None
        try:
            async with httpx.AsyncClient(timeout=self.settings.openai_timeout_seconds) as client:
                async with client.stream("POST", self.url, headers=self.headers, json=self.payload(messages, max_tokens=max_tokens, stream=True)) as response:
                    if response.status_code >= 400:
                        raise ModelGatewayError("Model gateway returned HTTP " + str(response.status_code))
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        event = json.loads(raw)
                        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                            raise ModelGatewayError("Model stream did not complete")
                        if event.get("type") == "response.completed":
                            completed = event.get("response")
            text = self.parse(completed)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError("Model stream request or response failed") from exc
        for index in range(0, len(text), 12):
            yield text[index:index + 12]
