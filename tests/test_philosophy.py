import json
import unittest
import uuid
from importlib.util import find_spec
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.runtime import AgentRuntimeService
from app.api.portfolio_routes import router as portfolio_router
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import Base, ChatSession, UserAccount
from app.services.philosophy import (
    AGENT_MAX_TOKENS,
    THEME_CONTENT,
    PhilosophyTranslator,
    PhilosophyValidationError,
    allowed_lenses,
    detect_sensitive_context,
    fallback_as_model_json,
    fallback_reply,
    normalize_translator_result,
    philosophy_decision,
    render_reply_text,
    validate_agent_reply,
)
from app.services.reframing import PhilosophicalReframingService
from app.services.safety_evaluation import evaluate_philosophy_guard_suite
from app.services.skills import MindBridgeSkillLibrary


class FakeCompletion:
    """Stands in for the model; records how it was called."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def complete(self, messages, *, max_tokens=None, json_mode=False):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "json_mode": json_mode})
        if self.error is not None:
            raise self.error
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)


class PhilosophyRoutingRuleTests(unittest.TestCase):
    def test_high_risk_and_risk_wording_never_get_philosophy(self):
        cases = [
            (IntentType.RISK, RiskLevel.HIGH, "我不想活了。", "high_risk"),
            (IntentType.CONSULT, RiskLevel.HIGH, "最近压力很大。", "high_risk"),
            (IntentType.RISK, RiskLevel.LOW, "最近压力很大。", "high_risk"),
            (IntentType.CONSULT, RiskLevel.LOW, "我有轻生的念头。", "risk_language"),
            (IntentType.CONSULT, RiskLevel.LOW, "I hurt myself when stressed.", "risk_language"),
            (IntentType.CONSULT, RiskLevel.MEDIUM, "我气到想伤害他人。", "risk_language"),
        ]
        for intent, risk, text, reason in cases:
            with self.subTest(text=text, risk=risk):
                decision = philosophy_decision(intent, risk, text)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.reason, reason)

    def test_chat_is_not_routed_to_philosophy(self):
        self.assertEqual(philosophy_decision(IntentType.CHAT, RiskLevel.LOW, "Python 字典怎么写").reason, "chat_intent")
        self.assertFalse(philosophy_decision(None, RiskLevel.LOW, "随便聊聊").eligible)

    def test_low_and_medium_consultations_are_routed_to_philosophy(self):
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
            with self.subTest(risk=risk):
                self.assertTrue(philosophy_decision(IntentType.CONSULT, risk, "我怕考试挂科。").eligible)

    def test_skill_selection_adds_philosophy_only_for_low_and_medium_consultations(self):
        names = MindBridgeSkillLibrary.response_skill_names
        self.assertIn("philosophical_reframing", names(IntentType.CONSULT, RiskLevel.LOW, "考试压力很大。"))
        self.assertIn("philosophical_reframing", names(IntentType.CONSULT, RiskLevel.MEDIUM, "最近很崩溃。"))
        self.assertEqual(
            names(IntentType.RISK, RiskLevel.HIGH, "我不想活了。"),
            ["supportive_response_baseline", "high_risk_safety_plan"],
        )
        self.assertEqual(names(IntentType.CHAT, RiskLevel.LOW, "Python 字典怎么写"), [])
        self.assertNotIn("philosophical_reframing", names(IntentType.CONSULT, RiskLevel.LOW, "我气到想伤害他人。"))
        context = MindBridgeSkillLibrary.response_skill_context(IntentType.CONSULT, RiskLevel.LOW, "考试压力很大。")
        self.assertIn("应用 skill: philosophical_reframing", context)


class PhilosophyReplyContractTests(unittest.TestCase):
    def test_every_preset_reply_is_complete_and_passes_validation(self):
        for theme in THEME_CONTENT:
            with self.subTest(theme=theme):
                sensitive = theme if theme in ("unfair", "grief") else None
                validated = validate_agent_reply(
                    fallback_as_model_json(theme),
                    density="standard",
                    allowed=allowed_lenses(sensitive),
                    risk=RiskLevel.LOW,
                    theme=theme,
                    sensitive_context=sensitive,
                )
                self.assertEqual(validated.lens, THEME_CONTENT[theme].lens)
                preset = fallback_reply(theme, RiskLevel.LOW, "model_disabled")
                sections = [
                    preset.empathy,
                    preset.plain_explanation,
                    preset.perspective,
                    preset.controllable,
                    preset.small_step,
                    preset.reflection_question,
                ]
                self.assertTrue(all(sections))
                self.assertTrue(preset.reflection_question.endswith("？"))
                self.assertTrue(preset.support_note)

    def test_valid_model_reply_is_used(self):
        payload = fallback_as_model_json("academic") | {"empathy": "复习任务堆着，心里发慌很正常。"}
        fake = FakeCompletion(payload)

        run = PhilosophyTranslator(fake, use_model=True).translate(
            "我怕考试挂科。",
            risk=RiskLevel.LOW,
            knowledge_context="- [kb] 学校心理中心提供预约咨询",
        )

        self.assertEqual(run.reply.source, "model")
        self.assertIsNone(run.reply.fallback_reason)
        self.assertEqual(run.reply.empathy, "复习任务堆着，心里发慌很正常。")
        self.assertTrue(fake.calls[0]["json_mode"])
        self.assertEqual(fake.calls[0]["max_tokens"], AGENT_MAX_TOKENS["standard"])
        self.assertIn("视角库", run.messages[0].content)
        self.assertIn("学校心理中心提供预约咨询", run.messages[0].content)
        self.assertIn('"我怕考试挂科。"', run.messages[1].content)

    def test_unsafe_or_malformed_model_replies_fall_back_to_presets(self):
        base = fallback_as_model_json("academic")
        cases = {
            "command": ({**base, "smallStep": "你应该马上去图书馆复习三个小时。"}, "unverifiable_or_directive"),
            "research claim": ({**base, "perspective": "研究表明，焦虑会让效率下降一半，先冷静下来再说。"}, "unverifiable_or_directive"),
            "book title": ({**base, "perspective": "正如《沉思录》里写的那样，我们只能掌控自己的判断。"}, "unverifiable_or_directive"),
            "diagnosis": ({**base, "plainExplanation": "这可能是焦虑症的表现，先把事实和感受分开来看。"}, "clinical_or_command"),
            "minimizing": ({**base, "empathy": "想开点，考试没什么大不了的。"}, "minimizing"),
            "risk wording": ({**base, "smallStep": "如果压力大到想伤害自己，就先去睡一觉。"}, "risk_language"),
            "markdown": ({**base, "perspective": "**重点**：先分清能控制和不能控制的部分，再决定今天做什么。"}, "markdown"),
            "unknown lens": ({**base, "lens": "超人哲学"}, "lens_not_allowed"),
            "missing field": ({key: value for key, value in base.items() if key != "controllable"}, "missing:controllable"),
            "too long": ({**base, "empathy": "很" * 90}, "too_long:empathy"),
            "not json": ("好的，下面是我的回答。", "invalid_json"),
            "label list": ({**base, "controllable": "能控制的：今天复习哪一块。不能控制的：最终分数。"}, "template_label"),
            "bracket heading": ({**base, "perspective": "【换个视角】很多焦虑来自想一下子掌控所有结果，其实你只需要面对接下来的这一小段复习。"}, "template_label"),
        }
        for label, (payload, reason) in cases.items():
            with self.subTest(label=label):
                reply = PhilosophyTranslator(FakeCompletion(payload), use_model=True).translate(
                    "我怕考试挂科。", risk=RiskLevel.LOW
                ).reply
                self.assertEqual(reply.source, "fallback")
                self.assertTrue(reply.fallback_reason.startswith(reason), reply.fallback_reason)
                self.assertEqual(reply.lens, THEME_CONTENT["academic"].lens)
                self.assertNotIn("你应该", render_reply_text(reply))

    def test_model_errors_and_mock_provider_use_presets(self):
        failing = FakeCompletion(error=RuntimeError("timeout"))
        reply = PhilosophyTranslator(failing, use_model=True).translate("越担心今晚睡不着，我就越清醒。", risk=RiskLevel.LOW).reply
        self.assertEqual(reply.fallback_reason, "model_error")

        unused = FakeCompletion(payload="{}")
        reply = PhilosophyTranslator(unused, use_model=False).translate("越担心今晚睡不着，我就越清醒。", risk=RiskLevel.LOW).reply
        self.assertEqual(reply.fallback_reason, "model_disabled")
        self.assertEqual(reply.theme, "sleep")
        self.assertEqual(unused.calls, [])

    def test_bullying_never_uses_acceptance_lenses(self):
        text = "我在宿舍被室友霸凌，很难过。"
        self.assertEqual(detect_sensitive_context(text), "unfair")
        self.assertNotIn("斯多葛控制圈", allowed_lenses("unfair"))
        self.assertNotIn("存在主义选择", allowed_lenses("unfair"))

        stoic = {**fallback_as_model_json("unfair"), "lens": "斯多葛控制圈"}
        run = PhilosophyTranslator(FakeCompletion(stoic), use_model=True).translate(text, risk=RiskLevel.LOW)

        self.assertEqual(run.reply.fallback_reason, "lens_not_allowed")
        self.assertEqual(run.reply.lens, "边界与求助")
        self.assertIn("正式反映渠道", run.reply.support_note)
        self.assertIn("特别规则", run.messages[0].content)
        cards = PhilosophicalReframingService().reframe(text).cards
        self.assertEqual([card.lens for card in cards], ["价值澄清", "边界与求助", "实用主义"])

    def test_grief_uses_companionship_lens(self):
        text = "外婆上个月去世了，我一直很难过。"
        reply = PhilosophyTranslator(FakeCompletion("{}"), use_model=False).translate(text, risk=RiskLevel.MEDIUM).reply
        self.assertEqual(reply.lens, "陪伴与哀伤")
        lenses = [card.lens for card in PhilosophicalReframingService().reframe(text).cards]
        self.assertNotIn("斯多葛控制圈", lenses)

    def test_reply_text_reads_as_conversation_without_labels(self):
        for theme in THEME_CONTENT:
            with self.subTest(theme=theme):
                reply = fallback_reply(theme, RiskLevel.LOW, "model_disabled")
                text = render_reply_text(reply)

                self.assertNotIn("【", text)
                self.assertNotIn("】", text)
                self.assertNotIn(reply.lens, text)
                self.assertNotRegex(text, r"(?m)^(能控制的|不能控制的|事实|换个说法|换个视角|想一想)[：:]")
                self.assertTrue(text.startswith(reply.empathy))
                self.assertTrue(text.endswith(reply.reflection_question))
                parts = [
                    reply.empathy,
                    reply.plain_explanation,
                    reply.perspective,
                    reply.controllable,
                    reply.small_step,
                    reply.support_note,
                    reply.reflection_question,
                ]
                positions = [text.index(part) for part in parts]
                self.assertEqual(positions, sorted(positions))

    def test_medium_risk_reply_always_carries_referral_note(self):
        reply = fallback_reply("academic", RiskLevel.MEDIUM, "model_disabled")
        self.assertIn("学校心理中心", reply.support_note)
        self.assertIn(reply.support_note, render_reply_text(reply))

    def test_guard_suite_passes_and_hides_raw_inputs(self):
        report = evaluate_philosophy_guard_suite()

        self.assertTrue(all(item["status"] == "PASS" for item in report["results"]), report["results"])
        self.assertTrue(all(value == 1.0 for value in report["metrics"].values()), report["metrics"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("我不想活了", serialized)
        self.assertNotIn("霸凌", serialized)


class SharedTranslatorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(portfolio_router)
        cls.client = TestClient(app)

    def test_translator_blocks_words_previously_only_known_to_the_agent(self):
        for question in ["I want to die.", "我最近有轻生的想法。"]:
            with self.subTest(question=question):
                response = self.client.post(
                    "/portfolio-ai/philosophy/reframe",
                    json={"question": question, "density": "concise"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["riskMode"], "safety-redirect")
                self.assertIsNone(response.json()["result"])

    def test_translator_rejects_new_unsafe_categories(self):
        base = {
            "plainExplanation": "先把事实和感受分开。",
            "differentPerspective": "也许几个需要在冲突。",
            "reflectionQuestion": "你最在意哪一点",
        }
        self.assertTrue(normalize_translator_result(base, "standard")["reflectionQuestion"].endswith("？"))
        for field, text in [("plainExplanation", "想开点就好了。"), ("differentPerspective", "可以考虑药物帮助。")]:
            with self.subTest(text=text):
                with self.assertRaises(PhilosophyValidationError):
                    normalize_translator_result({**base, field: text}, "standard")


class AgentPhilosophyRoutingTests(unittest.TestCase):
    """Runs the real agent runtimes offline: in-memory SQLite, mock model, no Redis."""

    def setUp(self):
        from app.harness.runner import InMemoryShortTermMemoryStore

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = UserAccount(username="student", display_name="测试学生", password_hash="unused", roles_csv="ROLE_USER")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)
        InMemoryShortTermMemoryStore.reset()
        patcher = mock.patch("app.agents.runtime.RedisShortTermMemoryStore", InMemoryShortTermMemoryStore)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings(
            ai_provider="mock",
            knowledge_vector_enabled=False,
            knowledge_vector_required=False,
            tool_queue_enabled=False,
        )

    def tearDown(self):
        self.db.close()

    def _runtimes(self):
        runtimes = [AgentRuntimeService]
        if find_spec("langgraph") is not None:
            from app.agents.langgraph_runtime import LangGraphAgentRuntimeService

            runtimes.append(LangGraphAgentRuntimeService)
        return runtimes

    def _run(self, runtime_cls, text):
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=self.user.id, title="philosophy-test")
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return runtime_cls(self.db, self.settings).run(self.user, session, text, text)

    def test_consultation_goes_through_philosophy_before_counselor(self):
        for runtime_cls in self._runtimes():
            with self.subTest(runtime=runtime_cls.__name__):
                result = self._run(runtime_cls, "我最近压力很大，连续几天失眠。")
                agents = [step.agent for step in result.steps]

                self.assertEqual(
                    agents,
                    ["MemoryAgent", "SupervisorAgent", "KnowledgeAgent", "RiskGuardianAgent", "PhilosophyAgent", "CounselorAgent"],
                )
                self.assertIsNotNone(result.philosophy)
                self.assertEqual(result.philosophy.source, "fallback")
                self.assertEqual(result.philosophy.fallback_reason, "model_disabled")
                self.assertEqual(result.prepared_reply, render_reply_text(result.philosophy))
                self.assertIn("lens=", result.steps[4].observation)

    def test_high_risk_skips_philosophy(self):
        for runtime_cls in self._runtimes():
            with self.subTest(runtime=runtime_cls.__name__):
                result = self._run(runtime_cls, "我不想活了，觉得撑不下去了。")
                agents = [step.agent for step in result.steps]

                self.assertNotIn("PhilosophyAgent", agents)
                self.assertEqual(agents[-1], "CounselorAgent")
                self.assertEqual(result.risk_level, RiskLevel.HIGH)
                self.assertIsNone(result.philosophy)
                self.assertEqual(result.prepared_reply, "")

    def test_chat_skips_philosophy(self):
        for runtime_cls in self._runtimes():
            with self.subTest(runtime=runtime_cls.__name__):
                result = self._run(runtime_cls, "帮我解释一下 Python 字典推导式。")
                agents = [step.agent for step in result.steps]

                self.assertEqual(agents, ["MemoryAgent", "SupervisorAgent", "CompanionAgent"])
                self.assertIsNone(result.philosophy)


if __name__ == "__main__":
    unittest.main()
