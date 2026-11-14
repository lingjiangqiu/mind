import unittest

from app.core.enums import RiskLevel
from app.services.reframing import PUBLIC_DEMO_TEXTS, PhilosophicalReframingService, is_public_demo_text


class PhilosophicalReframingTests(unittest.TestCase):
    def setUp(self):
        self.service = PhilosophicalReframingService()

    def test_academic_stress_returns_three_practical_lenses(self):
        result = self.service.reframe("我怕考试挂科，复习任务太多，完全不知道从哪里开始。")

        self.assertFalse(result.safetyOverride)
        self.assertEqual(result.riskLevel, RiskLevel.LOW.value)
        self.assertEqual(len(result.cards), 3)
        self.assertEqual({card.id for card in result.cards}, {"meaning", "agency", "action"})
        self.assertTrue(all(card.nextStep for card in result.cards))

    def test_high_risk_bypasses_all_philosophical_output(self):
        result = self.service.reframe("我不想活了，想结束生命。")

        self.assertTrue(result.safetyOverride)
        self.assertEqual(result.riskLevel, RiskLevel.HIGH.value)
        self.assertEqual(result.cards, [])
        self.assertGreaterEqual(len(result.safetyActions), 3)
        self.assertNotIn("斯多葛", result.safetyMessage or "")

    def test_english_high_risk_variants_bypass_all_philosophical_output(self):
        messages = [
            "I want to die and I feel unsafe tonight.",
            "I have prepared to end my life tonight.",
        ]

        for message in messages:
            with self.subTest(message=message):
                result = self.service.reframe(message)

                self.assertTrue(result.safetyOverride)
                self.assertEqual(result.riskLevel, RiskLevel.HIGH.value)
                self.assertEqual(result.cards, [])
                self.assertGreaterEqual(len(result.safetyActions), 3)

    def test_identifiers_are_sanitized_before_response(self):
        result = self.service.reframe("考试压力很大，请联系 13800138000 或 a@example.com")

        self.assertNotIn("13800138000", result.sanitizedText)
        self.assertNotIn("a@example.com", result.sanitizedText)
        self.assertEqual(result.sanitizedText.count("[已脱敏]"), 2)

    def test_normal_route_exposes_non_sensitive_decision_trace(self):
        result = self.service.reframe("我怕考试挂科，复习任务太多。")

        self.assertEqual(result.decisionTrace.privacyStep, "APPLIED")
        self.assertEqual(result.decisionTrace.hardRiskDecision, "NO_HIGH_RISK")
        self.assertEqual(result.decisionTrace.routeDecision, "REFRAME_CARDS")
        self.assertEqual(result.decisionTrace.outputContract, "3_CARDS+MINIMUM_ACTION")

    def test_high_risk_route_exposes_safety_contract(self):
        result = self.service.reframe("我不想活了，想结束生命。")

        self.assertEqual(result.decisionTrace.hardRiskDecision, "HIGH_RISK")
        self.assertEqual(result.decisionTrace.routeDecision, "SAFETY_OVERRIDE")
        self.assertEqual(result.decisionTrace.outputContract, "0_CARDS+SAFETY_ACTIONS>=3")

    def test_decision_trace_never_contains_raw_input_or_identifiers(self):
        phone = "13800138000"
        email = "qa@example.com"
        result = self.service.reframe(f"我不想活了，请联系 {phone} 或 {email}")
        trace = result.decisionTrace.model_dump_json()

        self.assertNotIn(phone, trace)
        self.assertNotIn(email, trace)
        self.assertNotIn("不想活", trace)

    def test_public_demo_scenarios_cover_reframing_and_safety_override(self):
        results = [self.service.reframe(text) for text in PUBLIC_DEMO_TEXTS]

        self.assertEqual(len(results), 4)
        self.assertEqual(sum(result.safetyOverride for result in results), 1)
        self.assertEqual(sum(len(result.cards) == 3 for result in results), 3)
        self.assertEqual(sum(len(result.cards) == 0 for result in results), 1)

    def test_public_demo_rejects_unlisted_personal_text(self):
        self.assertTrue(is_public_demo_text("  我怕考试挂科，复习任务太多，完全不知道从哪里开始。  "))
        self.assertFalse(is_public_demo_text("这是访客临时输入的一段真实经历。"))


if __name__ == "__main__":
    unittest.main()
