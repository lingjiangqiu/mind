import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.core.config import Settings
from app.services.ai import AiClient
from app.schemas.dtos import AiMessage
from app.services.compatible import ModelGatewayError, ResponsesClient
from app.services.deepseek import DeepSeekClient, DeepSeekError


def envelope(text="hello", status="completed"):
    return {"status": status, "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    ]}


class CompatibleModelTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, ai_provider="openai", openai_api_key="fixture-not-a-real-key",
                                 openai_base_url="https://gateway.invalid/v1", openai_model="gpt-6.1-sol")
        self.client = ResponsesClient(self.settings)

    def test_payload_keeps_instructions_and_history_without_coding_defaults(self):
        payload = self.client.payload([
            {"role": "system", "content": "校园陪伴，禁止诊断"},
            {"role": "user", "content": "有些焦虑"},
            {"role": "assistant", "content": "愿意说说吗？"},
            {"role": "user", "content": "担心考试"},
        ], max_tokens=500, json_mode=True)
        self.assertIn("校园陪伴", payload["instructions"])
        self.assertEqual([x["content"][0]["type"] for x in payload["input"]], ["input_text", "output_text", "input_text"])
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["max_output_tokens"], 4096)
        self.assertFalse(payload["store"])
        self.assertNotIn("service_tier", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("fixture-not-a-real-key", json.dumps(payload))

    def test_parse_skips_reasoning(self):
        self.assertEqual(self.client.parse(envelope("正文")), "正文")

    def test_incomplete_and_empty_outputs_rejected(self):
        for data in [envelope("partial", "incomplete"), envelope(""), {"status": "failed"}, None]:
            with self.subTest(data=data), self.assertRaises(ModelGatewayError):
                self.client.parse(data)

    def test_refusal_and_oversized_output_rejected(self):
        data = envelope()
        data["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
        with self.assertRaises(ModelGatewayError): self.client.parse(data)
        with self.assertRaises(ModelGatewayError): self.client.parse(envelope("x" * 24001))

    def test_missing_key_rejected(self):
        self.settings.openai_api_key = ""
        with self.assertRaises(ModelGatewayError): _ = self.client.headers

    def test_sync_ai_client_uses_responses(self):
        with patch("app.services.compatible.httpx.post", return_value=httpx.Response(200, json=envelope("CONSULT"))) as post:
            result = AiClient(self.settings).complete([AiMessage(role="user", content="焦虑")])
        self.assertEqual(result, "CONSULT")
        self.assertEqual(post.call_args.args[0], "https://gateway.invalid/v1/responses")

    def test_upstream_error_does_not_echo_body_or_key(self):
        with patch("app.services.compatible.httpx.post", return_value=httpx.Response(401, text="fixture-not-a-real-key")):
            with self.assertRaises(ModelGatewayError) as ctx:
                self.client.complete([{"role": "user", "content": "hi"}], max_tokens=500)
        self.assertNotIn("fixture-not-a-real-key", str(ctx.exception))

    def test_shared_portfolio_gateway_uses_selected_model(self):
        async def run():
            with patch.object(ResponsesClient, "complete_async", new=AsyncMock(return_value='{"answer":"ok"}')):
                result, _ = await DeepSeekClient(self.settings).generate_json(system_prompt="JSON", user_prompt="hi", max_tokens=700)
            return result
        gateway = DeepSeekClient(self.settings)
        self.assertEqual(gateway.provider, "openai-compatible")
        self.assertEqual(gateway.model, "gpt-6.1-sol")
        self.assertTrue(gateway.configured)
        self.assertEqual(asyncio.run(run()), {"answer": "ok"})

    def test_shared_gateway_rejects_non_json(self):
        async def run():
            with patch.object(ResponsesClient, "complete_async", new=AsyncMock(return_value="not JSON")):
                await DeepSeekClient(self.settings).generate_json(system_prompt="JSON", user_prompt="hi", max_tokens=700)
        with self.assertRaises(DeepSeekError): asyncio.run(run())

    def test_stream_requires_completed_event(self):
        async def run(events):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, text="\n".join("data: " + json.dumps(e) + "\n" for e in events)))
            real_client = httpx.AsyncClient
            with patch("app.services.compatible.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
                return "".join([part async for part in self.client.stream([{"role": "user", "content": "hi"}], max_tokens=500)])
        events = [{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.completed", "response": envelope("complete")}]
        self.assertEqual(asyncio.run(run(events)), "complete")
        with self.assertRaises(ModelGatewayError): asyncio.run(run(events[:1]))


if __name__ == "__main__":
    unittest.main()
