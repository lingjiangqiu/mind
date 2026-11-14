import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, get_db
from app.core.security import create_demo_token
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.services.ai import AiClient
from app.services.demo_chat import DemoChatStore, DemoConversation, run_public_turn, stream_public_turn
from app.services.philosophy import fallback_as_model_json


def config(**extra):
    values = dict(ai_provider="mock", knowledge_vector_enabled=False, tool_queue_enabled=False, agent_framework="langgraph")
    values.update(extra)
    return Settings(_env_file=None, **values)


class DemoLimitTests(unittest.TestCase):
    def setUp(self):
        self.store = DemoChatStore()
        self.settings = config()
        self.expiry = int(time.time()) + 100

    def reserve(self, key="a", **extra):
        return self.store.reserve(key, self.expiry, "127.0.0.1", "我怕考试挂科", **dict(session_id=None, settings=self.settings, **extra))

    def test_tokens_are_unique_even_with_same_expiry(self):
        self.assertNotEqual(create_demo_token("student", "test", 60), create_demo_token("student", "test", 60))
        import base64, hashlib, hmac
        from app.core.security import verify_demo_token
        data = {"sub": "student", "scope": "demo", "v": 1, "exp": int(time.time()) + 60}
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(b"test", encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        with self.assertRaises(HTTPException):
            verify_demo_token(encoded + "." + signature, "test")

    def test_independent_tokens_never_share_history(self):
        a = self.reserve()
        a.history.append(SimpleNamespace(role="user", content="private"))
        b = self.reserve("b")
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(b.history, [])

    def test_cannot_supply_another_sessions_id(self):
        a = self.reserve()
        with self.assertRaises(HTTPException) as caught:
            self.store.reserve("b", self.expiry, "127.0.0.1", "hello", a.session_id, self.settings)
        self.assertEqual(caught.exception.status_code, 409)

    def test_parallel_turn_rejected_and_active_slot_released(self):
        a = self.reserve()
        with self.assertRaises(HTTPException):
            self.reserve()
        self.store.finish(a)
        self.reserve()
        self.assertEqual(self.store.active, 1)

    def test_reset_clears_history_but_preserves_spent_turns(self):
        a = self.reserve()
        self.store.finish(a)
        a.history.append(SimpleNamespace(content="private"))
        previous_id = a.session_id
        self.store.reset("a")
        self.assertEqual(a.history, [])
        self.assertEqual(a.turns, 1)
        self.assertNotEqual(a.session_id, previous_id)

    def test_turn_limit_cannot_be_reset(self):
        self.settings.demo_chat_max_turns = 1
        a = self.reserve()
        self.store.finish(a)
        self.store.reset("a")
        with self.assertRaises(HTTPException) as caught:
            self.reserve()
        self.assertEqual(caught.exception.status_code, 429)

    def test_ip_limit_applies_across_new_tokens(self):
        self.settings.demo_chat_ip_requests = 1
        a = self.reserve()
        self.store.finish(a)
        with self.assertRaises(HTTPException) as caught:
            self.reserve("b")
        self.assertEqual(caught.exception.status_code, 429)

    def test_expired_memory_is_pruned(self):
        a = self.reserve()
        self.store.finish(a)
        a.expires_at = 1
        self.reserve("b")
        self.assertNotIn("a", self.store.conversations)

    def test_expired_token_rejected_before_reserving(self):
        self.expiry = 1
        with self.assertRaises(HTTPException) as caught:
            self.reserve()
        self.assertEqual(caught.exception.status_code, 401)

    def test_busy_reset_is_rejected(self):
        self.reserve()
        with self.assertRaises(HTTPException):
            self.store.reset("a")

    def test_input_cap_enforced_without_consuming_turn(self):
        with self.assertRaises(HTTPException):
            self.store.reserve("a", self.expiry, "ip", "a" * 1001, None, self.settings)
        self.assertEqual(self.store.active, 0)

    def test_global_concurrency_and_capacity_are_bounded(self):
        self.settings.demo_chat_max_concurrent = 1
        a = self.reserve()
        with self.assertRaises(HTTPException) as caught:
            self.reserve("b")
        self.assertEqual(caught.exception.status_code, 503)
        self.store.finish(a)
        self.settings.demo_chat_max_sessions = 2
        with self.assertRaises(HTTPException):
            self.reserve("c")


class PublicPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = UserAccount(username="student", display_name="Demo Student", password_hash="unused", roles_csv="ROLE_USER")
        self.db.add(self.user)
        self.db.commit()
        self.settings = config()
        self.state = DemoConversation("temporary-session", int(time.time()) + 100)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def run_turn(self, text):
        return run_public_turn(self.db, self.settings, self.user, self.state, text)

    def test_actual_langgraph_nodes_and_no_database_writes(self):
        before = {table.name: self.db.execute(table.select()).all() for table in Base.metadata.sorted_tables}
        with mock.patch("app.services.memory.RedisShortTermMemoryStore.load_recent", side_effect=AssertionError("must not read shared memory")):
            text, trace = self.run_turn("我怕考试挂科，任务太多不知道从哪里开始。")
        self.assertIn("PhilosophyAgent", [step["agent"] for step in trace["steps"]])
        self.assertEqual(trace["source"], "fallback")
        self.assertNotIn("【", text)
        after = {table.name: self.db.execute(table.select()).all() for table in Base.metadata.sorted_tables}
        self.assertEqual(before, after)
        self.assertEqual(trace["toolCalls"], 0)
        self.assertFalse(trace["persistentWrites"])

    def test_short_followup_keeps_emotion_context_and_theme(self):
        self.run_turn("我怕考试挂科。")
        text, trace = self.run_turn("是分数。")
        self.assertEqual(trace["lens"], "斯多葛控制圈")
        self.assertEqual(len(self.state.history), 4)
        self.assertIn("PhilosophyAgent", [s["agent"] for s in trace["steps"]])

    def test_high_risk_and_violence_bypass_philosophy_without_model(self):
        for text in ["我想伤害他人。", "I want to hurt myself", "我不想活了"]:
            with self.subTest(text=text), mock.patch.object(AiClient, "complete", side_effect=AssertionError("no model needed")):
                reply, trace = self.run_turn(text)
                self.assertTrue(trace["safetyOverride"])
                self.assertIsNone(trace["lens"])
                self.assertNotIn("PhilosophyAgent", [s["agent"] for s in trace["steps"]])
                self.assertIn("110", reply)
                self.assertIn("没有真人", reply)

    def test_high_risk_followup_stays_on_safety_path(self):
        self.run_turn("我不想活了")
        _, trace = self.run_turn("就这样吧")
        self.assertTrue(trace["safetyOverride"])

    def test_model_receives_and_memory_keeps_only_redacted_identifiers(self):
        self.settings.ai_provider = "deepseek"
        values = [json.dumps({"emotion": "ANXIETY", "risk": "LOW"}), json.dumps(fallback_as_model_json("academic"), ensure_ascii=False)]
        with mock.patch.object(AiClient, "complete", side_effect=values) as complete:
            self.run_turn("我怕考试，电话13812345678，邮箱student@example.com")
        sent = str(complete.call_args_list)
        self.assertNotIn("13812345678", sent)
        self.assertNotIn("student@example.com", sent)
        self.assertNotIn("13812345678", str(self.state.history))

    def test_valid_live_model_json_is_displayed_as_model_source(self):
        self.settings.ai_provider = "deepseek"
        data = fallback_as_model_json("academic")
        data["empathy"] = "担心考试结果，心里紧绷是可以理解的。"
        values = [json.dumps({"emotion": "ANXIETY", "risk": "LOW"}), json.dumps(data, ensure_ascii=False)]
        with mock.patch.object(AiClient, "complete", side_effect=values):
            text, trace = self.run_turn("我怕考试")
        self.assertEqual(trace["source"], "model")
        self.assertIsNone(trace["fallbackReason"])
        self.assertIn(data["empathy"], text)

    def test_bad_model_json_reports_fallback(self):
        self.settings.ai_provider = "deepseek"
        with mock.patch.object(AiClient, "complete", return_value="not json"):
            _, trace = self.run_turn("我怕考试")
        self.assertEqual(trace["source"], "fallback")
        self.assertEqual(trace["fallbackReason"], "invalid_json")

    def test_authenticated_demo_stream_and_reset_do_not_write_records(self):
        from app.main import create_app
        app = create_app()
        app.dependency_overrides[get_db] = lambda: self.db
        self.settings.demo_token_secret = "test-only-secret"
        with mock.patch("app.api.routes.get_settings", return_value=self.settings), mock.patch("app.core.security.get_settings", return_value=self.settings):
            client = TestClient(app)
            token = create_demo_token("student", "test-only-secret", 60)
            headers = {"Authorization": "Bearer " + token}
            response = client.post("/api/chat/stream", headers=headers, json={"message": "我怕考试"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("event: done", response.text)
            self.assertIn('"persistentWrites": false', response.text)
            self.assertEqual(client.delete("/api/demo/conversation", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/reports/me", headers=headers).status_code, 403)
            self.assertEqual(client.get("/api/admin/reports", headers=headers).status_code, 403)
        self.assertEqual(self.db.query(ChatSession).count(), 0)
        self.assertEqual(self.db.query(ChatMessage).count(), 0)

    def test_stream_failure_is_generic_and_releases_slot(self):
        self.state.busy = True
        with mock.patch("app.services.demo_chat.run_public_turn", side_effect=RuntimeError("secret must not leak")):
            async def collect():
                return "".join([part async for part in stream_public_turn(self.db, self.settings, self.user, self.state, "hello")])
            events = asyncio.run(collect())
        self.assertIn("event: error", events)
        self.assertNotIn("secret must", events)
        self.assertFalse(self.state.busy)


class DeepSeekAdapterTests(unittest.TestCase):
    def test_deepseek_adapter_uses_server_configuration_and_json_mode(self):
        settings = config()
        settings.ai_provider = "deepseek"
        settings.deepseek_api_key = "test-only-key"
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}
        with mock.patch("app.services.ai.httpx.post", return_value=response) as post:
            self.assertEqual(AiClient(settings).complete([], json_mode=True), '{"ok": true}')
        self.assertTrue(post.call_args.args[0].startswith(settings.deepseek_base_url))
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-only-key")
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})

    def test_incomplete_or_empty_model_output_is_rejected(self):
        settings = config()
        settings.ai_provider, settings.deepseek_api_key = "deepseek", "test-only-key"
        for content, reason in [("", "stop"), ("{}", "length")]:
            response = mock.Mock()
            response.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": reason}]}
            with mock.patch("app.services.ai.httpx.post", return_value=response), self.assertRaises(RuntimeError):
                AiClient(settings).complete([])


if __name__ == "__main__":
    unittest.main()
