"""그날 대화로 하루 요약을 쓴다 (Claude, 구조화 출력).

하루에 한 번 돌고 보호자가 읽는 글이라, 음성 스택 조사(docs/research/voice-stack.md) 결론대로
대화용 Haiku 대신 Claude Sonnet 5.5를 기본으로 쓴다. 정책상 거절이 나면 서버가 다른 모델로 다시 쓰도록
server-side fallback을 켠다.
"""

from __future__ import annotations

import json
import logging

from ..clock import KST
from . import guard
from .models import AI, MOOD_LEVELS, SECTIONS, DailyReport, DayLog

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SPEAKER_KO = {"senior": "어르신", AI: "AI"}

SYSTEM_PROMPT = """어르신과 AI 말벗이 하루 동안 나눈 대화를 읽고, 보호자(자녀)에게 보낼 하루 요약을 JSON으로만 씁니다.

말투: 보호자에게 어르신의 하루를 전하는 따뜻한 존댓말 ("~하셨어요"). 과장하거나 걱정을 부추기지 않습니다.
(모든 보호자에게 가는 한 줄 요약은 시스템이 대화 횟수로 따로 만듭니다. 여기서는 쓰지 않습니다)

sections (각각 1~2문장, 내용이 없으면 null):
- schedule: 한 일, 다녀온 곳, 만난 사람, 챙긴 일·약 ("오후에 치과에 다녀오셨고, 혈압약도 잘 챙겨 드셨어요.")
- health: 몸 상태에 대해 하신 말 ("무릎이 조금 쑤신다고 하셔서 집에서 쉬셨어요.")
- mood: 기분과 감정 ("손주 시험 이야기를 하시며 즐거워하셨어요.")
- 보호자는 동의한 항목만 받습니다. 그래서 **각 section에는 자기 항목 내용만** 쓰고, 그것만 따로 읽어도 이해되게 씁니다
  - mood에 일정·몸 상태를 섞지 않습니다: "치과 가기 귀찮아하셨지만 손주 전화에 기뻐하셨어요" X → "손주 소식을 듣고 기뻐하셨어요" O
  - health에 다녀온 곳을 섞지 않습니다: "치과 치료가 아프지 않았다고 하셨어요" X → "무릎이 쑤셔서 오래 걷기 힘들다고 하셨어요" O
  - schedule에 기분·몸 상태를 섞지 않습니다 (복약은 schedule에 씁니다)
- 세 section을 합쳐 3~5문장 정도

mood_level: 하루 전체의 기분. good(즐겁거나 편안함) / normal(평소와 같음) / low(외롭거나 처지거나 속상해하심)

지킬 것:
- **대화에 없는 내용을 만들지 않습니다.** 짐작한 일을 사실처럼 쓰지 않습니다
- **의료 표현을 쓰지 않습니다.** 병명·진단·증상이라는 말 대신 있었던 일을 그대로 씁니다 ("치매 증상", "건망증이 심해지셨어요" X → "같은 질문을 여러 번 하셨어요" O)
- 병원에 가 보라거나 치료가 필요하다는 조언을 쓰지 않습니다
- 대화 원문을 그대로 옮기지 않고 요약합니다
- 돈·사기·응급 상황은 따로 알림이 가므로 요약에서 부풀리지 않습니다"""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "object",
            "properties": {name: {"anyOf": [{"type": "string"}, {"type": "null"}]} for name in SECTIONS},
            "required": list(SECTIONS),
            "additionalProperties": False,
        },
        "mood_level": {"type": "string", "enum": list(MOOD_LEVELS)},
    },
    "required": ["sections", "mood_level"],
    "additionalProperties": False,
}


class ReportError(Exception):
    """요약을 쓰지 못함 (API 오류, 거절, 잘림). 배치는 이 사용자를 건너뛰고 다음에 다시 시도한다."""


def format_transcript(day: DayLog) -> str:
    # 보호자에게 가는 횟수(stats.conversation_count)와 같은 기준: 어르신이 한 마디라도 한 대화만
    lines = [f"날짜: {day.date.isoformat()}", f"대화 {day.conversation_count}번"]
    for i, conv in enumerate(day.conversations, 1):
        # 시각은 한국 시간으로 (UTC로 들어와도 저녁 일을 아침 일로 쓰지 않게)
        lines.append(f"\n[대화 {i}] {conv.started_at.astimezone(KST).strftime('%H:%M')} 시작")
        for turn in conv.turns:
            at = turn.at.astimezone(KST).strftime("%H:%M")
            lines.append(f"{at} {SPEAKER_KO.get(turn.speaker, turn.speaker)}: {turn.text}")
    return "\n".join(lines)


def _final_text(response) -> str:
    """응답의 최종 텍스트. 거절 뒤 다른 모델이 이어 썼으면 마지막 fallback 블록 뒤의 글만 쓴다."""
    texts: list[str] = []
    for block in response.content:
        if block.type == "fallback":
            texts = []
        elif block.type == "text":
            texts.append(block.text)
    return "".join(texts)


def _problems(raw: dict) -> list[str]:
    found = []
    for name in SECTIONS:
        value = raw["sections"].get(name)
        found += guard.problems(name, value if isinstance(value, str) else None)
    return found


class ClaudeReportWriter:
    def __init__(self, client=None, model: str = DEFAULT_MODEL, effort: str = "medium", api_errors=None):
        if client is None or api_errors is None:
            import anthropic  # 테스트에서는 가짜 client와 api_errors를 넣어서 SDK 없이도 돈다

            client = client or anthropic.Anthropic()
            api_errors = api_errors or (anthropic.APIError,)
        self.client = client
        self.model = model
        self.effort = effort
        # API 오류만 "요약 실패"로 처리한다. 인자 이름이 틀린 TypeError 같은 프로그래밍 오류는 그대로 터뜨려 원인이 보이게
        self.api_errors = tuple(api_errors)

    def write(self, day: DayLog) -> DailyReport:
        messages = [{"role": "user", "content": format_transcript(day)}]
        raw, text = self._call(messages)
        problems = _problems(raw)
        if problems:
            # 규칙을 어겼으면 이유를 알려 주고 한 번만 다시 쓰게 한다
            log.info("하루 요약 다시 쓰기: 문제 %d개", len(problems))
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "다음 규칙을 어겼습니다. 고쳐서 JSON 전체를 다시 써 주세요.\n- "
                 + "\n- ".join(problems)},
            ]
            try:
                raw, _ = self._call(messages)
            except ReportError as e:
                # 다시 쓰기가 실패해도 그날 요약을 버리지 않고, 첫 결과를 안전하게 고쳐서 보낸다
                log.warning("하루 요약 다시 쓰기 실패, 첫 결과를 고쳐서 씀: %s", e)
        return self._sanitize(raw, day)

    def _call(self, messages: list[dict]) -> tuple[dict, str]:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=messages,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except self.api_errors as e:  # 대화 내용은 로그에 남기지 않는다
            log.warning("하루 요약 API 오류: %s", type(e).__name__)
            raise ReportError(type(e).__name__) from e
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ReportError(f"stop_reason={response.stop_reason}")
        text = _final_text(response)
        try:
            raw = json.loads(text)
        except ValueError:
            raw = None
        if not isinstance(raw, dict) or not isinstance(raw.get("sections"), dict):
            raise ReportError("요약 결과가 형식에 맞지 않음")
        return raw, text

    @staticmethod
    def _sanitize(raw: dict, day: DayLog) -> DailyReport:
        """다시 써도 규칙을 어기면 안전한 쪽으로: 문제가 있는 section은 뺀다.

        section을 빼면 그 내용은 보호자에게 가지 않지만, 동의하지 않은 내용이 새는 것보다 낫다.
        brief는 LLM 결과를 쓰지 않고 대화 횟수로 만든다 (guard.template_brief).
        """
        brief = guard.template_brief(day.conversation_count)
        sections = {}
        for name in SECTIONS:
            value = raw["sections"].get(name)
            value = value.strip() if isinstance(value, str) else None
            if value and guard.problems(name, value):
                log.info("하루 요약 %s를 뺌 (다른 항목 내용 또는 의료 표현)", name)
                value = None
            sections[name] = value or None
        # mood section을 뺐어도 mood_level은 보낸다. mood_level은 기분 공유 동의 여부로 B가 따로 거르고(명세 3),
        # "며칠째 low" 판단에도 쓰인다. section을 뺀 이유는 다른 항목 내용이 섞였기 때문이지 기분을 몰라서가 아니다
        mood = raw.get("mood_level") if raw.get("mood_level") in MOOD_LEVELS else "normal"
        return DailyReport(brief=brief, sections=sections, mood_level=mood)
