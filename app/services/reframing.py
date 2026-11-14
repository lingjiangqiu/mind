from __future__ import annotations

from app.core.enums import RiskLevel
from app.schemas.dtos import ReframeDecisionTrace, ReframeResponse
from app.services.philosophy import detect_theme, has_philosophy_risk_signal, non_high_risk_level, theme_cards
from app.services.privacy import PrivacySanitizer


DISCLAIMER = "这是表达整理与自我反思工具，不构成心理诊断、治疗或用药建议。"
REFRAME_POLICY_VERSION = "reframe-safety-2026.07.2"
PUBLIC_DEMO_TEXTS = frozenset({
    "我怕考试挂科，复习任务太多，完全不知道从哪里开始。",
    "和室友吵架以后，我一直觉得自己不被尊重。",
    "越担心今晚睡不着，我就越清醒。",
    "我不想活了，想结束生命。",
})


def is_public_demo_text(text: str) -> bool:
    return " ".join((text or "").split()) in PUBLIC_DEMO_TEXTS


class PhilosophicalReframingService:
    def __init__(self, privacy: PrivacySanitizer | None = None):
        self.privacy = privacy or PrivacySanitizer()

    def reframe(self, text: str) -> ReframeResponse:
        normalized = " ".join((text or "").split())
        sanitized = self.privacy.sanitize(normalized)
        if has_philosophy_risk_signal(normalized):
            return ReframeResponse(
                riskLevel=RiskLevel.HIGH.value,
                safetyOverride=True,
                sanitizedText=sanitized,
                cards=[],
                safetyTitle="先确认你此刻的安全",
                safetyMessage="我听见这句话里有很强的痛苦。现在先不做哲学解释，请把现实中的安全支持放在第一位。",
                safetyActions=[
                    "立即联系一位可信任且能到你身边的人，并明确说你现在需要陪伴。",
                    "不要独处，先离开可能增加危险的物品、地点或环境。",
                    "联系学校心理中心、辅导员、校园保卫或当地急救服务；如有即时危险，请直接拨打当地紧急电话。",
                ],
                decisionTrace=_decision_trace(high_risk=True),
                disclaimer=DISCLAIMER,
            )

        theme = detect_theme(normalized)
        risk = non_high_risk_level(normalized)
        return ReframeResponse(
            riskLevel=risk.value,
            safetyOverride=False,
            sanitizedText=sanitized,
            cards=theme_cards(theme),
            safetyActions=[],
            decisionTrace=_decision_trace(high_risk=False),
            disclaimer=DISCLAIMER,
        )


def _decision_trace(*, high_risk: bool) -> ReframeDecisionTrace:
    return ReframeDecisionTrace(
        policyVersion=REFRAME_POLICY_VERSION,
        privacyStep="APPLIED",
        hardRiskDecision="HIGH_RISK" if high_risk else "NO_HIGH_RISK",
        routeDecision="SAFETY_OVERRIDE" if high_risk else "REFRAME_CARDS",
        outputContract="0_CARDS+SAFETY_ACTIONS>=3" if high_risk else "3_CARDS+MINIMUM_ACTION",
    )
