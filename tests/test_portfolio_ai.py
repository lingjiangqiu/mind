import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio_routes import _normalize_model_paper, router


class PortfolioAiApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_model_status_does_not_expose_credentials(self):
        response = self.client.get("/portfolio-ai/model-status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provider"], "deepseek")
        self.assertNotIn("apiKey", payload)
        self.assertNotIn("deepseek_api_key", payload)

    def test_wecom_fallback_redacts_pii_and_preserves_rule_decision(self):
        response = self.client.post(
            "/portfolio-ai/wecom/reply",
            json={"question": "我的手机号是13812345678，项目什么时候截止？"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("K01", payload["citations"])
        self.assertIn("K06", payload["citations"])
        self.assertFalse(payload["handoff"])
        self.assertFalse(payload["liveModel"])
        self.assertNotIn("13812345678", payload["sanitizedQuestion"])
        self.assertIn("[已脱敏]", payload["sanitizedQuestion"])

    def test_wecom_handoff_never_enters_model_generation(self):
        response = self.client.post(
            "/portfolio-ai/wecom/reply",
            json={"question": "我对退款有争议，要求人工处理。"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["handoff"])
        self.assertEqual(payload["mode"], "deterministic-handoff")
        self.assertFalse(payload["liveModel"])

    def test_acoustic_fallback_returns_audited_paper_draft(self):
        response = self.client.post(
            "/portfolio-ai/acoustic/paper",
            json={
                "sources": [
                    {
                        "id": "paper-a",
                        "title": "Speech transmission in sports halls",
                        "abstract": "Measurements found that STI decreased as RT60 increased in the observed sports halls.",
                        "authors": "A. Researcher",
                        "journal": "Applied Acoustics",
                        "year": "2025",
                    },
                    {
                        "id": "paper-b",
                        "title": "Early decay time and clarity",
                        "abstract": "EDT and C50 were evaluated under occupied and unoccupied conditions.",
                        "authors": "B. Researcher",
                        "journal": "Building Acoustics",
                        "year": "2024",
                    },
                ],
                "options": {"customTopic": "体育馆语音清晰度"},
            },
        )

        self.assertEqual(response.status_code, 200)
        paper = response.json()["data"]
        self.assertIn("论文初稿", paper["title"])
        self.assertEqual(len(paper["references"]), 2)
        self.assertEqual(paper["audit"]["status"], "PASS")
        self.assertEqual(paper["audit"]["invalidCitationNumbers"], [])
        self.assertFalse(paper["provenance"]["liveModel"])
        self.assertTrue(paper["provenance"]["fallbackReason"])

        sources = [
            {
                "id": "paper-a",
                "title": "Speech transmission in sports halls",
                "abstract": "STI decreased as RT60 increased in the observed sports halls.",
                "authors": "A. Researcher",
                "journal": "Applied Acoustics",
                "year": "2025",
                "doi": "",
            },
            {
                "id": "paper-b",
                "title": "Early decay time and clarity",
                "abstract": "EDT and C50 were evaluated under occupied and unoccupied conditions.",
                "authors": "B. Researcher",
                "journal": "Building Acoustics",
                "year": "2024",
                "doi": "",
            },
        ]
        raw_model_paper = {
            "title": "体育馆语音清晰度研究初稿",
            "keywords": ["STI", "RT60"],
            "abstract": "现有摘要讨论体育馆语音清晰度与混响条件。",
            "introduction": "输入材料覆盖语音传输与早期衰减指标。",
            "methods": "按摘要级证据进行结构化整理。",
            "results": "输入摘要分别报告 STI、RT60、EDT 与 C50。",
            "discussion": "不同研究条件尚不能直接比较。",
            "limitations": "当前未读取全文或原始数据。",
            "conclusion": "现有材料可形成待人工复核的研究初稿。",
        }
        normalized = _normalize_model_paper(
            raw_model_paper,
            sources,
            {"customTopic": "体育馆语音清晰度", "taskBrief": "", "sampleText": ""},
            1200,
            "deepseek-chat",
        )
        self.assertTrue(normalized["provenance"]["liveModel"])
        self.assertEqual(normalized["provenance"]["citationPolicy"], "server-completed")
        self.assertEqual(normalized["audit"]["status"], "PASS")
        self.assertEqual(normalized["audit"]["sourceCoverage"], 1)

    def test_philosophy_fallback_has_three_bounded_sections(self):
        response = self.client.post(
            "/portfolio-ai/philosophy/reframe",
            json={"question": "为什么周日晚上会焦虑？", "density": "standard"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["riskMode"], "standard")
        self.assertEqual(
            set(payload["result"]),
            {"plainExplanation", "differentPerspective", "reflectionQuestion"},
        )
        self.assertTrue(payload["result"]["reflectionQuestion"].endswith("？"))
        self.assertFalse(payload["provenance"]["liveModel"])

    def test_philosophy_high_risk_input_skips_reframe(self):
        response = self.client.post(
            "/portfolio-ai/philosophy/reframe",
            json={"question": "我不想活了，想结束生命。", "density": "deep"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["riskMode"], "safety-redirect")
        self.assertIsNone(payload["result"])
        self.assertGreaterEqual(len(payload["safety"]["actions"]), 2)
        self.assertEqual(payload["provenance"]["mode"], "safety-redirect")

    def test_cross_origin_requests_are_rejected(self):
        response = self.client.get(
            "/portfolio-ai/model-status",
            headers={"Origin": "https://example.invalid"},
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
