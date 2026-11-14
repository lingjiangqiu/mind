"""Shared controlled logic for the philosophy emotion translator.

Three entry points use this one module, so their safety guard, lens catalog,
output contract, validation and deterministic fallback cannot drift apart:

- the portfolio translator ``POST /portfolio-ai/philosophy/reframe``;
- the public reframing demo ``POST /api/reframe`` (three cards);
- the agent's PhilosophyAgent node (six-part structured reply).

Philosophy is only offered for low- and medium-risk consultations. Any
high-risk signal bypasses it entirely, and every model reply is validated
before a student can see it; a failed reply falls back to reviewed preset text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage, PhilosophyReplyPayload, ReframingCard
from app.services.ai import PromptTemplates, has_high_risk_signal


PHILOSOPHY_POLICY_VERSION = "philosophy-translation-2026.11.1"
DEFAULT_DENSITY = "standard"
DENSITIES = ("concise", "standard", "deep")


# ---------------------------------------------------------------------------
# Input guard: no philosophical content when any of these signals is present.
# ---------------------------------------------------------------------------

PHILOSOPHY_RISK_PATTERN = re.compile(
    r"自杀|不想活|结束生命|伤害自己|自残|杀死自己|伤害他人|杀人|"
    r"suicid(?:e|al)|self[ -]?harm|kill myself|hurt myself|kill someone",
    re.I,
)


def has_philosophy_risk_signal(text: str) -> bool:
    """Union of the translator's risk pattern and the agent's high-risk words."""
    value = text or ""
    return bool(PHILOSOPHY_RISK_PATTERN.search(value)) or has_high_risk_signal(value)


# ---------------------------------------------------------------------------
# Output checks shared by the translator and the agent.
# ---------------------------------------------------------------------------

UNVERIFIABLE_OR_DIRECTIVE_PATTERN = re.compile(
    r"�|https?://|doi\s*:|《|》|第.{0,8}章|研究表明|数据显示|"
    r"你应该|你必须|建议你|诊断为|患有",
    re.I,
)
CLINICAL_OR_COMMAND_PATTERN = re.compile(
    r"抑郁症|焦虑症|强迫症|双相|人格障碍|精神障碍|精神疾病|心理疾病|服药|用药|药物|务必|你一定要",
    re.I,
)
MINIMIZING_PATTERN = re.compile(
    r"想开(?:点|些)|看开(?:点|些)|往好处想|没什么大不了|都会过去的|塞翁失马|一切都是最好的安排|别矫情|太敏感",
    re.I,
)
MARKDOWN_PATTERN = re.compile(r"\*\*|```|^\s*#{1,6}\s", re.M)


def unsafe_output_reason(text: str) -> str | None:
    """Return why a piece of generated text must not reach a student, or None."""
    for code, pattern in (
        ("unverifiable_or_directive", UNVERIFIABLE_OR_DIRECTIVE_PATTERN),
        ("clinical_or_command", CLINICAL_OR_COMMAND_PATTERN),
        ("minimizing", MINIMIZING_PATTERN),
        ("markdown", MARKDOWN_PATTERN),
    ):
        match = pattern.search(text or "")
        if match:
            return f"{code}:{match.group(0).strip()}"
    if has_philosophy_risk_signal(text):
        return "risk_language"
    return None


class PhilosophyValidationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Lens catalog: the only perspectives a reply may use.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lens:
    name: str
    idea: str
    fits: str
    avoid: str


LENSES: tuple[Lens, ...] = (
    Lens("价值澄清", "难受的背后往往有在意的东西，先看见它在保护什么。", "压力、委屈、纠结等情绪", "不用来评判感受对不对"),
    Lens("斯多葛控制圈", "分清能决定的和不能决定的，把力气放在能决定的部分。", "学业压力、等待结果、睡眠焦虑", "欺凌、歧视、骚扰、丧亲等情境，不能用来劝人接受"),
    Lens("实用主义", "反思的价值在于让下一步更清楚，而不是得到漂亮的解释。", "事情太多、无从下手、拖延", "对方情绪还很强烈时，不急着推行动"),
    Lens("存在主义选择", "处境不能全选，但怎样回应仍然可以选。", "迷茫、选择困难、意义感低", "不公平处境中，不能暗示当事人要为处境负责"),
    Lens("边界与求助", "不公平的事不靠调整心态解决：先确认事实、守住边界、找正式支持。", "欺凌、歧视、骚扰、被不公平对待，以及关系中的边界问题", "不用来指责对方或替学生做决定"),
    Lens("陪伴与哀伤", "悲伤不需要被讲道理化解，它需要被允许、被陪伴。", "亲人离世、失去重要的人或关系", "不催促“走出来”，不急着找意义"),
)
ALL_LENSES = frozenset(lens.name for lens in LENSES)
SENSITIVE_LENSES = {
    "unfair": frozenset({"边界与求助", "价值澄清"}),
    "grief": frozenset({"陪伴与哀伤", "价值澄清"}),
}
SENSITIVE_RULES = {
    "unfair": (
        "本次困扰涉及可能的欺凌、歧视、骚扰或不公平对待：先确认感受和事实，"
        "不得用“接受不可控”“换个角度看”淡化，也不得暗示学生要为此负责；要提到可信任的人和学校的正式渠道。"
    ),
    "grief": "本次困扰涉及丧失或哀伤：先陪伴和允许悲伤，不讲道理、不催促走出来，也不急着寻找意义。",
}


def allowed_lenses(sensitive_context: str | None) -> frozenset[str]:
    return SENSITIVE_LENSES.get(sensitive_context or "", ALL_LENSES)


def lens_catalog(names: frozenset[str]) -> str:
    return "\n".join(
        f"- {lens.name}：{lens.idea}（适用：{lens.fits}；不适用：{lens.avoid}）"
        for lens in LENSES
        if lens.name in names
    )


# ---------------------------------------------------------------------------
# Context and theme detection.
# ---------------------------------------------------------------------------

UNFAIR_TERMS = (
    "霸凌", "欺凌", "校园暴力", "被孤立", "孤立我", "排挤", "歧视", "骚扰", "猥亵", "威胁我", "家暴",
    "被打了", "被人打", "挨打", "pua", "bully", "harass", "discriminat",
)
GRIEF_TERMS = ("去世", "离世", "过世", "病逝", "葬礼", "丧亲", "失去了亲人", "passed away", "funeral", "grief")


def detect_sensitive_context(text: str) -> str | None:
    """Unfair treatment or grief: contexts where acceptance-style lenses would minimise harm."""
    lowered = (text or "").lower()
    if _contains_any(lowered, UNFAIR_TERMS):
        return "unfair"
    if _contains_any(lowered, GRIEF_TERMS):
        return "grief"
    return None


def detect_theme(text: str) -> str:
    sensitive = detect_sensitive_context(text)
    if sensitive:
        return sensitive
    lowered = (text or "").lower()
    if _contains_any(lowered, ["考试", "论文", "作业", "绩点", "挂科", "保研", "毕业", "学习", "exam", "academic"]):
        return "academic"
    if _contains_any(lowered, ["家人", "父母", "室友", "朋友", "恋爱", "关系", "争吵", "relationship", "family"]):
        return "relationship"
    if _contains_any(lowered, ["失眠", "睡不着", "熬夜", "睡眠", "insomnia", "sleep"]):
        return "sleep"
    return "general"


def non_high_risk_level(text: str) -> RiskLevel:
    lowered = (text or "").lower()
    if _contains_any(lowered, ["崩溃", "绝望", "抑郁", "撑不住", "无法正常", "hopeless", "depress"]):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


# ---------------------------------------------------------------------------
# Reviewed preset content per theme: three demo cards + six-part fallback reply.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThemeContent:
    # Three cards for /api/reframe (wording for the four original themes is unchanged).
    signal: str
    control: str
    step: str
    # Six-part reply used when the agent has no validated model reply.
    lens: str
    empathy: str
    explanation: str
    perspective: str
    controllable: str
    small_step: str
    question: str
    # Card wording that differs for unfair-treatment and grief contexts.
    meaning_next: str = "先允许感受存在，不急着给自己贴标签。"
    agency_title: str = "把控制权拿回来"
    agency_lens: str = "斯多葛控制圈"
    agency_next: str = "圈出一件由你决定、且今天能够完成的事。"
    action_title: str = "从意义回到行动"
    action_content: str = "反思的价值不在于得到一句漂亮解释，而在于让下一步更清楚。"


THEME_CONTENT: dict[str, ThemeContent] = {
    "academic": ThemeContent(
        signal="你面对的不只是一个任务，也可能是对结果、评价和未来的不确定感。压力说明这件事对你很重要，但分数并不等于你的全部价值。",
        control="把问题分成可控与不可控：今天能完成的最小任务、可求助的人属于可控；最终题目、他人的评价和所有结果并不完全由你决定。",
        step="只选一个 20 分钟内可完成的动作，例如列出三道最薄弱的题或写下论文第一段的三个要点。",
        lens="斯多葛控制圈",
        empathy="任务堆在一起、又担心结果的时候，心里发慌很正常，这不说明你不行。",
        explanation="我们可以先把它拆开看：真正发生的，是复习任务多、时间有点紧；“我可能会挂科”是你对结果的担心；慌和不知道从哪下手，是这份担心带来的感受。你这么在意，也说明这件事对你很重要，不过分数并不等于你的全部价值。",
        perspective="很多焦虑来自想一下子掌控所有结果。其实你现在要面对的不是“整场考试”，只是接下来的这一小段复习。",
        controllable="今天复习哪一块、什么时候开始、要不要找人请教，这些都由你决定；题目难不难、最后考多少分，就不全在你手里了。",
        small_step="可以先只挑一件 20 分钟能做完的事，比如把最没把握的三道题列出来，做完就先歇一会儿。",
        question="除了分数本身，这次考试让你最放不下的是什么？",
    ),
    "relationship": ThemeContent(
        signal="关系中的难受常常同时包含被理解、被尊重或保留边界的需要。看见需要，不等于替任何一方判定对错。",
        control="你无法控制对方如何回应，但可以决定何时表达、表达什么事实，以及哪些边界需要说清楚。",
        step="用一句“当……发生时，我感到……，我希望……”写下准备表达的话，先不发送。",
        lens="价值澄清",
        empathy="吵完架还一直觉得不被尊重，这种难受是真实的，值得被认真对待。",
        explanation="把这件事拆开看：发生的是你们吵了一架；“对方不尊重我”是你对这件事的理解；委屈和生气，是它带给你的感受。难受的背后，往往是想被理解、被尊重，或者想守住自己的边界。",
        perspective="比起先判断谁对谁错，看清自己在意的是什么往往更有用。你在意的也许不是这一次吵赢，而是以后相处时能不能被认真对待。",
        controllable="什么时候说、说哪些事实、哪些边界要讲清楚，这些你可以自己决定；对方怎么想、会不会马上改变，就不由你决定了。",
        small_step="可以先用“当……的时候，我觉得……，我希望……”把想说的话写下来，写完先不急着发。",
        question="在这段关系里，你最希望对方明白的是哪一点？",
    ),
    "sleep": ThemeContent(
        signal="越想立刻睡着，越容易把清醒体验成一次失败。睡眠不是意志力考试，暂时清醒也不代表明天一定会失控。",
        control="你不能命令身体马上入睡，但可以减少刺激、暂停反复看时间，并给身体一个稳定的休息信号。",
        step="把手机放远，做十次缓慢呼吸；若长时间仍清醒，先离床做低刺激活动，困意回来再躺下。",
        lens="斯多葛控制圈",
        empathy="越想睡越睡不着，这样的夜晚很磨人，先别因此怪自己。",
        explanation="我们拆开看：真正发生的是今晚还没睡着；“明天一定会很糟”是你对明天的担心；着急和身体发紧，是这份担心带来的感受。越想马上睡着，就越容易把醒着当成一次失败。",
        perspective="睡觉不是意志力考试，身体什么时候入睡，没法靠命令决定。暂时醒着，也不代表明天一定会垮掉。",
        controllable="把刺激调小、别反复看时间、给身体一个安稳的休息信号，这些你做得到；几点一定睡着，就先交给身体自己。",
        small_step="可以先把手机放远一点，慢慢呼吸十次；要是很久还清醒，就起来做点安静的小事，困了再回来躺下。",
        question="如果今晚不以“马上睡着”为目标，你想先怎么让自己放松下来？",
    ),
    "general": ThemeContent(
        signal="这句话里可能同时有事实、解释和情绪。先把它们分开，会比急着说服自己“别想太多”更有帮助。",
        control="你不必一次解决全部问题。先找出此刻能影响的一小部分，以及需要他人或现实资源介入的部分。",
        step="写下一个事实、一种感受和一个需要，然后为这个需要选择今天最小的可执行动作。",
        lens="实用主义",
        empathy="事情一件件压过来的时候，乱和累都很正常，先慢一点也没关系。",
        explanation="可以先把它拆开看：到底发生了什么，你是怎么理解它的，它又让你有什么感觉。这几层分开以后，往往比急着劝自己“别多想”更有帮助。",
        perspective="想清楚的意义，不在于找到一个漂亮的解释，而在于让下一步变得清楚一点。不用一次把所有问题都解决，先找到眼下能碰到的那一小块就好。",
        controllable="先处理哪一件、今天花多少时间、要不要找人帮忙，这些你可以自己定；所有事情一下子都变好，就不是今天能决定的了。",
        small_step="可以先写下一件发生的事、一种感受和一个需要，再为这个需要挑一件今天能做的小事。",
        question="这几件事里，哪一件是你今天真正想先碰一下的？",
    ),
    "unfair": ThemeContent(
        signal="你感到的委屈或愤怒，说明自己的边界被越过了。这首先是一个需要被认真对待的事实问题，不是你想多了。",
        control="不公平的事不靠调整心态解决。你能做的是记录事实、告诉可信任的人、使用学校的正式渠道，而不是独自消化。",
        step="把发生的时间、地点和经过简单记下来，需要时交给老师或辅导员。",
        lens="边界与求助",
        empathy="遇到这样的对待，难受、委屈或者愤怒都很正常，这不是你想多了。",
        explanation="这首先是一件需要被认真对待的事：具体发生了什么、是谁、是不是一直在持续。你会委屈、会生气，说明自己的边界被越过了。",
        perspective="不公平的事不是靠调整心态来解决的，也不需要你一个人默默消化。先把事实弄清楚、守住自己的边界，再借助身边的人和学校的正式渠道。",
        controllable="记下发生了什么、先告诉谁、要不要走学校的正式渠道，这些你可以决定；对方怎么想你管不了，但对方的行为是可以被制止和处理的。",
        small_step="可以先把时间、地点和经过简单记下来，再想想哪位老师、辅导员或朋友是你愿意先告诉的。",
        question="如果先把这件事告诉一个人，你最愿意告诉谁？",
        meaning_next="先允许感受存在，不急着替对方找理由。",
        agency_title="先确认事实与边界",
        agency_lens="边界与求助",
        agency_next="想一位可以先告诉的老师、辅导员或朋友。",
    ),
    "grief": ThemeContent(
        signal="悲伤是你和重要的人之间联结的延续，它没有标准的时间表，也不需要被讲道理化解。",
        control="这种时候不需要用道理去想通。可以做的是给自己留一点安静的时间，并让身边的人知道你正在经历什么。",
        step="今天按时吃一顿饭，或者给自己留一段安静的时间。",
        lens="陪伴与哀伤",
        empathy="失去重要的人，痛会很深，不用急着让自己好起来。",
        explanation="悲伤是你和那个重要的人之间的联结还在延续。它没有标准的时间表，有时会突然涌上来，这并不说明你哪里出了问题。",
        perspective="这种时候不需要用道理把自己说通。比起解释，更需要的是允许自己难过，也让身边的人知道你正在经历什么。",
        controllable="给自己留一点安静的时间、决定想和谁说说话，这些你可以自己安排；难过会在什么时候涌上来，却不由你控制，这也没关系。",
        small_step="今天可以找一位信任的人，说一句“我最近在经历一段难过的时候”，或者给自己留一段安静的时间。",
        question="这段时间，你最希望有谁陪着你，或者被怎样陪着？",
        meaning_next="先允许自己难过，不急着让自己好起来。",
        agency_title="让自己被陪伴",
        agency_lens="陪伴与哀伤",
        agency_next="决定今天想和谁说说话。",
        action_title="照顾好今天的自己",
        action_content="悲伤的时候，下一步可以很小，照顾好今天就已经足够。",
    ),
}


def theme_cards(theme: str) -> list[ReframingCard]:
    """The three /api/reframe cards for a theme."""
    content = THEME_CONTENT.get(theme, THEME_CONTENT["general"])
    return [
        ReframingCard(id="meaning", title="这句话在保护什么", lens="价值澄清", content=content.signal, nextStep=content.meaning_next),
        ReframingCard(id="agency", title=content.agency_title, lens=content.agency_lens, content=content.control, nextStep=content.agency_next),
        ReframingCard(id="action", title=content.action_title, lens="实用主义", content=content.action_content, nextStep=content.step),
    ]


# ---------------------------------------------------------------------------
# Agent reply: eligibility, structure, validation, fallback and rendering.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhilosophyDecision:
    eligible: bool
    reason: str
    sensitive_context: str | None = None


def philosophy_decision(intent: IntentType | None, risk: RiskLevel, text: str) -> PhilosophyDecision:
    """Routing rule for the PhilosophyAgent: consultation and low/medium risk only."""
    if intent is None or intent == IntentType.CHAT:
        return PhilosophyDecision(False, "chat_intent")
    if intent == IntentType.RISK or risk == RiskLevel.HIGH:
        return PhilosophyDecision(False, "high_risk")
    if has_philosophy_risk_signal(text):
        return PhilosophyDecision(False, "risk_language")
    return PhilosophyDecision(True, "consult_low_or_medium", detect_sensitive_context(text))


@dataclass(frozen=True)
class PhilosophyReply:
    lens: str
    empathy: str
    plain_explanation: str
    perspective: str
    controllable: str
    small_step: str
    reflection_question: str
    support_note: str
    theme: str
    sensitive_context: str | None
    source: str
    fallback_reason: str | None = None
    policy_version: str = PHILOSOPHY_POLICY_VERSION

    def to_payload(self) -> PhilosophyReplyPayload:
        return PhilosophyReplyPayload(
            lens=self.lens,
            empathy=self.empathy,
            plainExplanation=self.plain_explanation,
            perspective=self.perspective,
            controllable=self.controllable,
            smallStep=self.small_step,
            reflectionQuestion=self.reflection_question,
            supportNote=self.support_note,
            source=self.source,
            policyVersion=self.policy_version,
        )

    def trace_summary(self) -> str:
        parts = [
            f"lens={self.lens}",
            f"theme={self.theme}",
            f"source={self.source}",
            "validation=PASS" if self.source == "model" else "validation=FALLBACK",
        ]
        if self.fallback_reason:
            parts.append(f"reason={self.fallback_reason}")
        return "; ".join(parts)


AGENT_FIELDS = ("lens", "empathy", "plainExplanation", "perspective", "controllable", "smallStep", "reflectionQuestion")
AGENT_LENGTH_LIMITS = {
    "concise": {"plainExplanation": 120, "perspective": 120, "controllable": 100},
    "standard": {"plainExplanation": 200, "perspective": 200, "controllable": 160},
    "deep": {"plainExplanation": 320, "perspective": 320, "controllable": 240},
}
AGENT_FIXED_LIMITS = {"lens": 12, "empathy": 80, "smallStep": 120, "reflectionQuestion": 80}
AGENT_LENGTH_RULES = {
    "concise": "plainExplanation 和 perspective 各 40-90 个中文字符，controllable 不超过 80 个中文字符",
    "standard": "plainExplanation 和 perspective 各 60-150 个中文字符，controllable 不超过 140 个中文字符",
    "deep": "plainExplanation 和 perspective 各 120-260 个中文字符，controllable 不超过 200 个中文字符",
}
AGENT_MAX_TOKENS = {"concise": 700, "standard": 1000, "deep": 1400}
MIN_FIELD_CHARS = 6
# The parts are joined into one chat message, so headings or label-style lines
# ("能控制的：……") would make it read like a form instead of a person talking.
TEMPLATE_LABEL_PATTERN = re.compile(
    r"[【】]|^\s*(?:能控制的|不能控制的|不能完全控制的|可控的?|不可控的?|事实|解读|感受|换个说法|换个视角|"
    r"能做主的部分|今天的一小步|想一想|小行动|反思问题)[^，。\n]{0,4}[：:]",
    re.M,
)
SENTENCE_ENDINGS = ("。", "！", "？", "!", "?", "…")


def support_note(risk: RiskLevel, sensitive_context: str | None) -> str:
    """Deterministic referral line appended to every reply; never model-generated."""
    if sensitive_context == "unfair":
        return "如果这件事还在发生，可以找辅导员、学校心理中心或学校的正式反映渠道；要是遇到人身危险，请直接联系校园保卫或当地紧急服务。"
    if sensitive_context == "grief":
        return "难过的时候不用一个人扛，辅导员和学校心理中心也可以陪你聊一聊。"
    if risk == RiskLevel.MEDIUM:
        return "如果这种状态已经持续了一段时间、影响到学习或睡眠，可以找学校心理中心或辅导员聊聊，一起想想怎么帮到你。"
    return "如果之后觉得越来越难扛，学校心理中心和辅导员都可以去找。"


def validate_agent_reply(
    raw: Any,
    *,
    density: str,
    allowed: frozenset[str],
    risk: RiskLevel,
    theme: str,
    sensitive_context: str | None,
) -> PhilosophyReply:
    """Accept a model reply only if every field is present, bounded and safe."""
    if not isinstance(raw, dict):
        raise PhilosophyValidationError("not_an_object")
    limits = {**AGENT_LENGTH_LIMITS[density], **AGENT_FIXED_LIMITS}
    values: dict[str, str] = {}
    for name in AGENT_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str):
            raise PhilosophyValidationError(f"missing:{name}")
        value = _clean_multiline(value, 10_000)
        if len(value) < (2 if name == "lens" else MIN_FIELD_CHARS):
            raise PhilosophyValidationError(f"missing:{name}")
        if len(value) > limits[name]:
            raise PhilosophyValidationError(f"too_long:{name}")
        values[name] = value
    if values["lens"] not in allowed:
        raise PhilosophyValidationError("lens_not_allowed")
    for name, value in values.items():
        reason = unsafe_output_reason(value)
        if reason:
            raise PhilosophyValidationError(f"{reason}@{name}")
    for name, value in values.items():
        if name != "lens" and TEMPLATE_LABEL_PATTERN.search(value):
            raise PhilosophyValidationError(f"template_label@{name}")
    question = values["reflectionQuestion"].rstrip("。.!！")
    if not question.endswith(("?", "？")):
        question += "？"
    return PhilosophyReply(
        lens=values["lens"],
        empathy=values["empathy"],
        plain_explanation=values["plainExplanation"],
        perspective=values["perspective"],
        controllable=values["controllable"],
        small_step=values["smallStep"],
        reflection_question=question,
        support_note=support_note(risk, sensitive_context),
        theme=theme,
        sensitive_context=sensitive_context,
        source="model",
    )


def fallback_reply(theme: str, risk: RiskLevel, reason: str | None) -> PhilosophyReply:
    content = THEME_CONTENT.get(theme, THEME_CONTENT["general"])
    sensitive = theme if theme in SENSITIVE_LENSES else None
    return PhilosophyReply(
        lens=content.lens,
        empathy=content.empathy,
        plain_explanation=content.explanation,
        perspective=content.perspective,
        controllable=content.controllable,
        small_step=content.small_step,
        reflection_question=content.question,
        support_note=support_note(risk, sensitive),
        theme=theme if theme in THEME_CONTENT else "general",
        sensitive_context=sensitive,
        source="fallback",
        fallback_reason=reason,
    )


def fallback_as_model_json(theme: str) -> dict[str, str]:
    """A preset reply in the model's JSON shape, used to prove presets pass validation."""
    content = THEME_CONTENT.get(theme, THEME_CONTENT["general"])
    return {
        "lens": content.lens,
        "empathy": content.empathy,
        "plainExplanation": content.explanation,
        "perspective": content.perspective,
        "controllable": content.controllable,
        "smallStep": content.small_step,
        "reflectionQuestion": content.question,
    }


def render_reply_text(reply: PhilosophyReply) -> str:
    """Plain-text reply streamed to the student and saved as the assistant message.

    The parts keep their order but carry no headings or lens names, so the message
    reads like one person talking. The referral line comes before the question so
    the reply ends by inviting the student to keep talking.
    """
    paragraphs = [
        reply.empathy,
        reply.plain_explanation,
        _join_sentences(reply.perspective, reply.controllable),
        _join_sentences(reply.small_step, reply.support_note),
        reply.reflection_question,
    ]
    return "\n\n".join(part for part in paragraphs if part)


def _join_sentences(*parts: str) -> str:
    text = ""
    for part in (value.strip() for value in parts):
        if not part:
            continue
        if text and not text.endswith(SENTENCE_ENDINGS):
            text += "。"
        text += part
    return text


class CompletionClient(Protocol):
    def complete(self, messages: list[AiMessage], *, max_tokens: int | None = None, json_mode: bool = False) -> str: ...


@dataclass(frozen=True)
class TranslationRun:
    reply: PhilosophyReply
    messages: list[AiMessage]


class PhilosophyTranslator:
    """Generate a structured reply, validate it, and fall back to presets on any failure."""

    def __init__(self, ai: CompletionClient, *, use_model: bool):
        self.ai = ai
        self.use_model = use_model

    def translate(
        self,
        text: str,
        *,
        risk: RiskLevel,
        density: str = DEFAULT_DENSITY,
        knowledge_context: str = "",
        skill_context: str = "",
        memory_brief: str = "",
    ) -> TranslationRun:
        density = density if density in DENSITIES else DEFAULT_DENSITY
        sensitive = detect_sensitive_context(text)
        theme = detect_theme(text)
        allowed = allowed_lenses(sensitive)
        messages = PromptTemplates.philosophy_translation_prompt(
            text=text,
            memory_brief=memory_brief,
            knowledge_context=knowledge_context,
            skill_context=skill_context,
            lens_catalog=lens_catalog(allowed),
            length_rule=AGENT_LENGTH_RULES[density],
            context_rule=SENSITIVE_RULES.get(sensitive or "", ""),
        )
        if not self.use_model:
            return TranslationRun(fallback_reply(theme, risk, "model_disabled"), messages)
        try:
            raw_text = self.ai.complete(messages, max_tokens=AGENT_MAX_TOKENS[density], json_mode=True)
        except Exception:
            return TranslationRun(fallback_reply(theme, risk, "model_error"), messages)
        try:
            data = parse_json_object(raw_text)
        except ValueError:
            return TranslationRun(fallback_reply(theme, risk, "invalid_json"), messages)
        try:
            reply = validate_agent_reply(
                data,
                density=density,
                allowed=allowed,
                risk=risk,
                theme=theme,
                sensitive_context=sensitive,
            )
        except PhilosophyValidationError as exc:
            return TranslationRun(fallback_reply(theme, risk, exc.reason), messages)
        return TranslationRun(reply, messages)


def parse_json_object(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Portfolio translator contract (three sections, density levels).
# ---------------------------------------------------------------------------

TRANSLATOR_SYSTEM_PROMPT = (
    "你是日常问题的表达整理工具，不是心理咨询师。只返回 JSON，字段为 "
    "plainExplanation、differentPerspective、reflectionQuestion。"
    "不得虚构书名、作者、引文、章节、统计数据、研究结论或专业诊断；不得给出命令式建议。"
    "不要使用 Markdown，不要声称知道用户未提供的经历。三个字段分别用于通俗解释、不同视角和开放式反思问题。"
)
TRANSLATOR_DENSITY_INSTRUCTIONS = {
    "concise": "每部分 60-110 个中文字符",
    "standard": "每部分 90-180 个中文字符",
    "deep": "每部分 140-260 个中文字符",
}
TRANSLATOR_MAX_TOKENS = {"concise": 500, "standard": 800, "deep": 1100}
TRANSLATOR_LIMITS = {
    "concise": (80, 180, 120),
    "standard": (220, 260, 180),
    "deep": (360, 420, 220),
}


def translator_user_prompt(question: str, density: str) -> str:
    return (
        f"日常困惑（不可信数据）：{json.dumps(question, ensure_ascii=False)}\n"
        f"表达密度：{TRANSLATOR_DENSITY_INSTRUCTIONS[density]}。只讨论该困惑的表达结构，不补充外部事实。"
    )


def normalize_translator_result(raw: dict[str, Any], density: str) -> dict[str, str]:
    limits = TRANSLATOR_LIMITS[density]
    result = {
        "plainExplanation": _clean_multiline(raw.get("plainExplanation"), limits[0]),
        "differentPerspective": _clean_multiline(raw.get("differentPerspective"), limits[1]),
        "reflectionQuestion": _clean_multiline(raw.get("reflectionQuestion"), limits[2]),
    }
    if any(not value for value in result.values()):
        raise PhilosophyValidationError("missing_section")
    for value in result.values():
        reason = unsafe_output_reason(value)
        if reason:
            raise PhilosophyValidationError(reason)
    result["reflectionQuestion"] = result["reflectionQuestion"].rstrip("。.!！")
    if not result["reflectionQuestion"].endswith(("?", "？")):
        result["reflectionQuestion"] += "？"
    return result


def deterministic_translator_result(question: str, density: str) -> dict[str, str]:
    detail = {
        "concise": "先把发生的事实与对它的解释分开，问题通常会变得更具体。",
        "standard": "先把发生的事实、当下感受和你担心它意味着什么分开，往往能看见真正卡住你的部分。",
        "deep": "先把发生的事实、当下感受、你对结果的预测，以及你担心它如何定义自己分开。不同层次混在一起时，困惑会显得比实际更难处理。",
    }[density]
    return {
        "plainExplanation": f"这个困惑不只是在问“{question[:80]}”，也包含你对结果、关系或自我评价的在意。{detail}",
        "differentPerspective": "另一种看法是，犹豫或焦虑不一定说明选择出了错，也可能说明几个同样重要的需求暂时发生冲突。这里未必存在唯一正确答案，更值得比较的是各个选择的代价、边界和可承受范围。",
        "reflectionQuestion": "如果暂时不要求自己马上解决，这件事最值得先确认的事实是什么？",
    }


def _clean_multiline(value: Any, max_length: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_length]


def _contains_any(text: str, terms) -> bool:
    return any(term in text for term in terms)
