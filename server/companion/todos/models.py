"""할 일 초안. 대화에서 뽑은 할 일을 명세 1 요청(`POST /todos`)으로 바꾸기 전의 A 쪽 표현."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..clock import KST  # noqa: F401  (flow·speech가 여기서 가져다 씀)

KINDS = ("appointment", "medication", "task")
TIME_HINTS = ("morning", "noon", "evening", "bedtime")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass(frozen=True)
class Repeat:
    freq: str  # "daily" | "weekly"
    weekdays: tuple[str, ...] = ()  # weekly일 때만. WEEKDAYS 순서

    def to_api(self) -> dict:
        if self.freq == "daily":
            return {"freq": "daily"}
        return {"freq": "weekly", "weekdays": list(self.weekdays)}


@dataclass(frozen=True)
class TodoDraft:
    kind: str
    title: str
    original_text: str  # 어르신이 실제로 한 말 [민감정보: 로그에 남기지 않음]
    date: dt.date | None = None  # 반복이면 None (B가 요청 받은 날부터 시작)
    time: dt.time | None = None  # 정확한 시각을 말했을 때만. A가 추측해서 넣지 않음
    time_hint: str | None = None  # "아침에"처럼 대략만 말했을 때
    repeat: Repeat | None = None
    message_id: str | None = None  # 이 할 일이 나온 발화

    @property
    def key(self) -> tuple:
        """같은 할 일인지 판단하는 값. 제목이 같아도 날짜·시각·반복이 다르면 다른 할 일."""
        return (self.title, self.date, self.time, self.time_hint, self.repeat)

    @property
    def needs_time(self) -> bool:
        """시각도 대략적인 때도 없음 → 보내기 전에 한 번 되묻는다 (명세 1)."""
        return self.time is None and self.time_hint is None

    def to_request(self, *, request_id: str, user_id: str, conversation_id: str) -> dict:
        """명세 1 `POST /todos` 요청 본문."""
        return {
            "request_id": request_id,
            "user_id": user_id,
            "kind": self.kind,
            "title": self.title,
            "date": self.date.isoformat() if self.date else None,
            "time": self.time.strftime("%H:%M") if self.time else None,
            "time_hint": None if self.time else self.time_hint,  # 둘 다 보내면 400
            "repeat": self.repeat.to_api() if self.repeat else None,
            "original_text": self.original_text,
            "source": {"conversation_id": conversation_id, "message_id": self.message_id},
        }
