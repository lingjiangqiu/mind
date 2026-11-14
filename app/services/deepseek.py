import json
import time
from typing import Any

import httpx

from app.core.config import Settings


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.deepseek_api_key.strip())

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> tuple[dict[str, Any], int]:
        if not self.configured:
            raise DeepSeekError("DeepSeek is not configured")

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.settings.deepseek_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.deepseek_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.settings.deepseek_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DeepSeekError("DeepSeek request timed out or failed") from exc

        if response.status_code >= 400:
            raise DeepSeekError(f"DeepSeek returned HTTP {response.status_code}")

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise DeepSeekError("DeepSeek returned an invalid response envelope") from exc

        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("DeepSeek returned empty content")
        if len(content) > self.settings.deepseek_max_output_chars:
            raise DeepSeekError("DeepSeek response exceeded the configured output limit")

        try:
            data = _parse_json_object(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeepSeekError("DeepSeek did not return a JSON object") from exc

        return data, round((time.perf_counter() - started) * 1000)


def _parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise TypeError("expected a JSON object")
    return parsed
