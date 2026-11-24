"""확인 질문에 대한 어르신의 짧은 대답을 읽는다 ("네", "아니요", "세 시 반", "아침에").

LLM을 다시 부르지 않고 규칙으로 처리한다. 대답이 짧고, 지연을 줄이고, 결과를 테스트로 고정하기 위해서다.
알아듣지 못하면 None을 돌려주고, 흐름(flow.py) 쪽에서 한 번 더 묻거나 넘어간다.
"""

from __future__ import annotations

import datetime as dt
import re

YES = "yes"
NO = "no"
UNKNOWN_TIME = "unknown"

# 거절을 먼저 본다 ("아니 네가 해 줘" 같은 말은 드묾. "괜찮아요"는 보통 사양하는 뜻)
_NO_PHRASES = ("아니", "아뇨", "안 해", "안해", "하지 마", "하지마", "됐어", "됐습니다", "됐다",
               "싫어", "필요 없", "필요없", "괜찮아", "괜찮습니다", "그만", "나중에", "취소")
_YES_PHRASES = ("그래", "좋아", "좋습니다", "좋지", "등록해", "해 줘", "해줘", "해 주", "해주",
                "부탁", "그렇게", "맞아", "맞습니다", "알려 줘", "알려줘", "알려 주", "알려주", "그럼요")
_YES_FIRST_WORDS = ("네", "예", "응", "어", "넹", "넵", "그럼")


def _normalize(text: str) -> str:
    return re.sub(r"[.,!?~…]+", " ", text).strip()


def parse_yes_no(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(p in t for p in _NO_PHRASES):
        return NO
    first = t.split()[0]
    if first in _YES_FIRST_WORDS or any(p in t for p in _YES_PHRASES):
        return YES
    return None


# --- 시각 ---

_NATIVE_HOURS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7,
                 "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12}
_SINO = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}

_HOUR = "|".join(sorted(_NATIVE_HOURS, key=len, reverse=True))
_TIME_RE = re.compile(
    rf"(?P<period>오전|오후|아침|낮|점심|저녁|밤|새벽)?\s*"
    rf"(?P<hour>\d{{1,2}}|{_HOUR})\s*시"
    rf"(?:\s*(?P<half>반)|\s*(?P<minute>\d{{1,2}}|[일이삼사오육칠팔구십]+)\s*분)?"
)

_HINTS = (
    ("bedtime", ("자기 전", "자기전", "잘 때", "잘때", "주무시기 전", "취침")),
    ("morning", ("아침",)),
    ("noon", ("점심", "낮에")),
    ("evening", ("저녁",)),
)
_UNKNOWN = ("몰라", "모르겠", "아직", "안 정", "안정해", "미정", "글쎄")


def _sino_number(s: str) -> int | None:
    """십, 이십, 삼십오 같은 한자어 수 (0~59)."""
    if s.isdigit():
        return int(s)
    if "십" in s:
        tens, _, ones = s.partition("십")
        value = (_SINO.get(tens, 0) if tens else 1) * 10
        if tens and tens not in _SINO:
            return None
        if ones:
            if ones not in _SINO:
                return None
            value += _SINO[ones]
        return value
    return _SINO.get(s)


def parse_time_answer(text: str) -> dt.time | str | None:
    """"몇 시로 알려 드릴까요?"에 대한 대답.

    돌려주는 값: dt.time (정확한 시각) / time_hint 문자열 / UNKNOWN_TIME ("몰라요") / None (알아듣지 못함)
    """
    t = _normalize(text)
    m = _TIME_RE.search(t)
    if m:
        raw_hour = m.group("hour")
        hour = int(raw_hour) if raw_hour.isdigit() else _NATIVE_HOURS[raw_hour]
        minute = 30 if m.group("half") else 0
        if m.group("minute"):
            parsed = _sino_number(m.group("minute"))
            if parsed is None or parsed > 59:
                return None
            minute = parsed
        period = m.group("period")
        if hour > 24:
            return None
        if hour == 24:
            hour = 0
        elif period in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        elif period in ("오전", "아침", "새벽", "밤") and hour == 12:  # 밤 12시 = 자정
            hour = 0
        elif period is None and 1 <= hour <= 6:
            hour += 12  # 오전·오후를 말하지 않은 1~6시는 오후로 봄. 확인 질문에서 "오후 3시"로 다시 읽어 드림
        return dt.time(hour, minute)

    for hint, words in _HINTS:
        if any(w in t for w in words):
            return hint
    if any(w in t for w in _UNKNOWN):
        return UNKNOWN_TIME
    return None
