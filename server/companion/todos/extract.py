"""어르신 발화에서 할 일·복약을 뽑는다 (Claude, 구조화 출력).

모델은 음성 스택 조사(docs/research/voice-stack.md)의 잠정 결론대로 Claude Haiku 5.5를 기본으로 쓴다.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from ..llm import LLMCallError, LLMOutputError, call_json, voice_client
from .models import KINDS, TIME_HINTS, WEEKDAYS, Repeat, TodoDraft

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"

KOREAN_WEEKDAYS = "월화수목금토일"

SYSTEM_PROMPT = """어르신이 한 말에서 앞으로 해야 할 일(일정·복약·할 일)을 찾아 JSON으로만 답합니다.

찾는 것:
- 날짜나 시각이 있는 앞으로의 일정 ("내일 세 시에 치과", "모레 은행 가야 해")
- 반복되는 복약 ("혈압약은 매일 아침 먹어야 해요")
- 날짜가 정해진 할 일 ("금요일까지 관리비 내야 해")

찾지 않는 것 (todos를 비움):
- 이미 지난 일, 이미 한 일 ("어제 병원 다녀왔어", "약 먹었어")
- 날짜도 반복도 없는 막연한 바람 ("언젠가 여행 가고 싶어")
- 다른 사람의 일정 ("손주가 다음 주에 시험이래요")

필드 규칙:
- kind: 병원·약속·모임은 "appointment", 약은 "medication", 그 밖은 "task"
- title: 짧은 명사형 ("치과 예약", "혈압약", "관리비 내기"). 화면과 음성 알림에 그대로 쓰입니다
- date: 상대 날짜("내일", "다음 주 화요일")는 아래 오늘 날짜를 기준으로 YYYY-MM-DD로 바꿉니다. 반복이고 시작일을 말하지 않았으면 null
- time: 몇 시를 직접 말했을 때만 24시간 HH:MM. 오전·오후를 말하지 않은 1시~6시는 오후로 봅니다 (확인 질문에서 "오후 3시"로 다시 읽어 드립니다)
- time_hint: "아침에", "점심 먹고", "저녁에", "자기 전에"처럼 대략만 말했으면 morning / noon / evening / bedtime. 이때 time은 null. **대략적인 말을 시각으로 바꾸지 않습니다** ("아침" → 08:00 X)
- time과 time_hint 둘 다 모르면 둘 다 null
- repeat_freq: "매일"이면 daily, "매주 월요일"처럼 요일이 있으면 weekly(weekdays에 MON~SUN), 반복이 아니면 null
- original_text: 그 할 일이 나온 어르신의 말 그대로"""

def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "title": {"type": "string"},
                    "date": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
                    "time": _nullable({"type": "string", "description": "HH:MM (24시간)"}),
                    "time_hint": _nullable({"type": "string", "enum": list(TIME_HINTS)}),
                    "repeat_freq": _nullable({"type": "string", "enum": ["daily", "weekly"]}),
                    "weekdays": {"type": "array", "items": {"type": "string", "enum": list(WEEKDAYS)}},
                    "original_text": {"type": "string"},
                },
                "required": ["kind", "title", "date", "time", "time_hint", "repeat_freq", "weekdays", "original_text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["todos"],
    "additionalProperties": False,
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$", re.ASCII)


class ExtractionError(Exception):
    """추출 실패 (API 오류). 대화는 계속하고 할 일 제안만 건너뛴다."""


def build_user_message(utterance: str, today: dt.date, recent: list[str] | tuple[str, ...] = ()) -> str:
    lines = [f"오늘: {today.isoformat()} ({KOREAN_WEEKDAYS[today.weekday()]}요일)"]
    if recent:
        lines.append("최근 대화 (참고용):")
        lines.extend(f"- {r}" for r in recent)
    lines.append(f"어르신이 방금 한 말: {utterance}")
    return "\n".join(lines)


def to_draft(raw: dict, *, utterance: str, today: dt.date, message_id: str | None) -> TodoDraft | None:
    """모델 출력 한 개를 검사해서 초안으로 바꾼다. 쓸 수 없으면 None."""
    title = (raw.get("title") or "").strip()
    kind = raw.get("kind")
    if not title or kind not in KINDS:
        return None

    date = None
    if raw.get("date"):
        try:
            date = dt.date.fromisoformat(raw["date"])
        except ValueError:
            return None
        if date < today:
            return None  # 지난 일은 등록하지 않음

    time = None
    m = _HHMM.match(raw.get("time") or "")
    if m:
        time = dt.time(int(m.group(1)), int(m.group(2)))
    hint = raw.get("time_hint") if raw.get("time_hint") in TIME_HINTS else None
    if time is not None:
        hint = None  # 둘 중 하나만 (명세 1)

    repeat = None
    freq = raw.get("repeat_freq")
    if freq == "daily":
        repeat = Repeat("daily")
    elif freq == "weekly":
        days = tuple(d for d in WEEKDAYS if d in set(raw.get("weekdays") or ()))
        if days:
            repeat = Repeat("weekly", days)
    if date is None and repeat is None:
        return None  # MISSING_SCHEDULE이 될 요청은 만들지 않음

    return TodoDraft(
        kind=kind,
        title=title,
        original_text=(raw.get("original_text") or "").strip() or utterance,
        date=date,
        time=time,
        time_hint=hint,
        repeat=repeat,
        message_id=message_id,
    )


class ClaudeTodoExtractor:
    def __init__(self, client=None, model: str = DEFAULT_MODEL, effort: str = "low", api_errors=None):
        self.client, self.api_errors = voice_client(client, api_errors)  # 대화 턴 안이라 짧게 기다림
        self.model = model
        self.effort = effort  # 음성 대화라 지연이 중요 → low

    def extract(
        self,
        utterance: str,
        today: dt.date,
        *,
        message_id: str | None = None,
        recent: list[str] | tuple[str, ...] = (),
    ) -> list[TodoDraft]:
        try:
            raw = call_json(self.client, self.api_errors, model=self.model, system=SYSTEM_PROMPT,
                            content=build_user_message(utterance, today, recent), schema=TODO_SCHEMA,
                            effort=self.effort)
        except LLMCallError as e:  # 원문은 로그에 남기지 않는다
            log.warning("할 일 추출 API 오류: %s", e)
            raise ExtractionError(str(e)) from e
        except LLMOutputError as e:
            log.warning("할 일 추출 결과를 쓸 수 없음: %s", e)
            return []
        items = raw.get("todos")
        if not isinstance(items, list):
            log.warning("할 일 추출 결과에 todos가 없음")
            return []

        drafts = []
        for raw in items:
            if isinstance(raw, dict):
                draft = to_draft(raw, utterance=utterance, today=today, message_id=message_id)
                if draft is not None:
                    drafts.append(draft)
        return drafts
