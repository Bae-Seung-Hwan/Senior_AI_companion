"""하루 요약의 재료(그날 대화)와 결과(명세 3 요청).

A 대화 기록 저장(#18)이 생기기 전이라, 하루치 대화를 JSON 파일로 받는다 (samples/ 참고).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from ..clock import KST

log = logging.getLogger(__name__)

SECTIONS = ("schedule", "health", "mood")
MOOD_LEVELS = ("good", "normal", "low")
SENIOR = "senior"
AI = "ai"


@dataclass(frozen=True)
class Turn:
    speaker: str  # "senior" | "ai"
    text: str     # [민감정보] 로그에 남기지 않음
    at: dt.datetime


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    started_at: dt.datetime
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class DayLog:
    user_id: str
    date: dt.date
    conversations: tuple[Conversation, ...]
    completed_todos: int | None = None  # B에서 받아 올 방법이 아직 없음 (명세에 조회 API 없음). 모르면 None

    @property
    def senior_turn_count(self) -> int:
        return sum(1 for c in self.conversations for t in c.turns if t.speaker == SENIOR)

    @property
    def conversation_count(self) -> int:
        """어르신이 한 마디라도 한 대화 수. 답이 없던 AI 안부 인사는 세지 않는다."""
        return sum(1 for c in self.conversations if any(t.speaker == SENIOR for t in c.turns))

    @classmethod
    def from_dict(cls, data: dict) -> DayLog:
        day = dt.date.fromisoformat(data["date"])
        conversations = []
        for c in data.get("conversations", []):
            conv = Conversation(
                conversation_id=c["conversation_id"],
                started_at=_aware(c["started_at"]),
                turns=tuple(Turn(t["speaker"], t["text"], _aware(t["at"])) for t in c["turns"]),
            )
            # 대화가 시작된 날(한국 시간)로 날짜를 정한다 (명세 6 대화 횟수와 같은 기준). 다른 날 대화는 뺀다
            if conv.started_at.astimezone(KST).date() != day:
                log.warning("다른 날짜의 대화를 뺌: %s", conv.conversation_id)
                continue
            conversations.append(conv)
        return cls(user_id=data["user_id"], date=day, completed_todos=data.get("completed_todos"),
                   conversations=tuple(conversations))


def _aware(value: str) -> dt.datetime:
    at = dt.datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError(f"시각에 시간대가 없습니다: {value}")  # 공통 규칙: 시간대 필수
    return at


@dataclass(frozen=True)
class DailyReport:
    brief: str
    sections: dict = field(default_factory=dict)  # schedule/health/mood → 문장 또는 None
    mood_level: str = "normal"

    def to_request(self, *, request_id: str, day: DayLog, generated_at: dt.datetime) -> dict:
        """명세 3 `POST /guardian-reports/daily` 요청 본문."""
        stats = {"conversation_count": day.conversation_count}
        if day.completed_todos is not None:
            stats["completed_todos"] = day.completed_todos
        return {
            "request_id": request_id,
            "user_id": day.user_id,
            "date": day.date.isoformat(),
            "brief": self.brief,
            "sections": {name: self.sections.get(name) for name in SECTIONS},
            "mood_level": self.mood_level,
            "stats": stats,
            "generated_at": generated_at.isoformat(timespec="seconds"),
        }
