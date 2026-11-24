"""하루 요약의 재료(그날 대화)와 결과(명세 3 요청).

A 대화 기록 저장(#18)이 생기기 전이라, 하루치 대화를 JSON 파일로 받는다 (samples/ 참고).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

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

    @classmethod
    def from_dict(cls, data: dict) -> DayLog:
        return cls(
            user_id=data["user_id"],
            date=dt.date.fromisoformat(data["date"]),
            completed_todos=data.get("completed_todos"),
            conversations=tuple(
                Conversation(
                    conversation_id=c["conversation_id"],
                    started_at=_aware(c["started_at"]),
                    turns=tuple(Turn(t["speaker"], t["text"], _aware(t["at"])) for t in c["turns"]),
                )
                for c in data.get("conversations", [])
            ),
        )


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
        stats = {"conversation_count": len(day.conversations)}
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
