"""어르신에게 들려줄 말 (TTS로 읽힘). 짧고, 존댓말, 숫자는 TTS가 읽기 쉬운 형태로."""

from __future__ import annotations

import datetime as dt

from .models import KST, WEEKDAYS, TodoDraft

_WEEKDAY_KO = dict(zip(WEEKDAYS, "월화수목금토일", strict=True))
_HINT_KO = {"morning": "아침", "noon": "점심", "evening": "저녁", "bedtime": "주무시기 전"}


def has_final_consonant(word: str) -> bool:
    """마지막 글자에 받침이 있는지. 한글이 아니면 받침 없음으로 본다."""
    if not word:
        return False
    code = ord(word[-1]) - 0xAC00
    return 0 <= code < 11172 and code % 28 != 0


def topic(word: str) -> str:
    """'치과 예약은', '혈압약은', '수영은' / '관리비 내기는'."""
    return word + ("은" if has_final_consonant(word) else "는")


def date_phrase(d: dt.date, today: dt.date) -> str:
    days = (d - today).days
    if days == 0:
        return "오늘"
    if days == 1:
        return "내일"
    if days == 2:
        return "모레"
    weekday = "월화수목금토일"[d.weekday()]
    if d.year != today.year:
        return f"{d.year}년 {d.month}월 {d.day}일 {weekday}요일"
    return f"{d.month}월 {d.day}일 {weekday}요일"


def time_phrase(t: dt.time) -> str:
    h, m = t.hour, t.minute
    if h == 0:
        head = "밤 12시"
    elif h <= 5:
        head = f"새벽 {h}시"  # "오전 3시"보다 어르신께 자연스러움
    elif h < 12:
        head = f"오전 {h}시"
    elif h == 12:
        head = "낮 12시"
    else:
        head = f"오후 {h - 12}시"
    if m == 0:
        return head
    if m == 30:
        return f"{head} 반"
    return f"{head} {m}분"


def repeat_phrase(draft: TodoDraft) -> str:
    if draft.repeat is None:
        return ""
    if draft.repeat.freq == "daily":
        return "매일"
    return "매주 " + "·".join(_WEEKDAY_KO[d] for d in draft.repeat.weekdays) + "요일"


def describe(draft: TodoDraft, today: dt.date) -> str:
    """'내일 오후 3시 반 치과 예약', '매일 아침 혈압약', '매주 월·목요일 오전 9시 수영'."""
    parts = []
    if draft.repeat is not None:
        parts.append(repeat_phrase(draft))
    elif draft.date is not None:
        parts.append(date_phrase(draft.date, today))
    if draft.time is not None:
        parts.append(time_phrase(draft.time))
    elif draft.time_hint is not None:
        parts.append(_HINT_KO[draft.time_hint])
    parts.append(draft.title)
    return " ".join(parts)


def confirm_question(draft: TodoDraft, today: dt.date) -> str:
    return f"{describe(draft, today)}, 등록해 드릴까요?"


def ask_time_question(draft: TodoDraft) -> str:
    return f"{topic(draft.title)} 몇 시로 알려 드릴까요?"


def registered(draft: TodoDraft, today: dt.date, next_at: str | None) -> str:
    """B 응답(명세 1)을 받은 뒤 확인 멘트."""
    text = f"{describe(draft, today)}, 등록해 두었어요."
    if next_at is None:
        # 명세 1에 수정 API가 없어서 "시간이 정해지면 말씀해 주세요"라고 하면 할 일이 두 개가 됨 (#68 안건)
        return f"{text} 시간을 몰라서 알림은 따로 드리지 않을게요."
    if draft.time is None:
        # time_hint는 B가 사용자별 기본 시각으로 바꿈 → 실제로 알릴 시각을 알려 드림
        at = dt.datetime.fromisoformat(next_at)
        if at.tzinfo is not None:
            at = at.astimezone(KST)  # B가 다른 시간대로 보내도 한국 시간으로 읽어 드림
        text += f" {time_phrase(at.time())}에 알려 드릴게요."
    return text


def declined() -> str:
    return "알겠어요, 등록하지 않을게요."


def ask_again(draft: TodoDraft, today: dt.date) -> str:
    """못 알아들었거나 되물으셨을 때. 내용을 못 들으셨을 수 있으니 다시 읽어 드린다."""
    return f"{describe(draft, today)}, 등록해 드릴까요? 네, 아니요로 말씀해 주세요."


def already_registered(draft: TodoDraft) -> str:
    return f"{topic(draft.title)} 이미 등록돼 있어요."


def invalid_date() -> str:
    return "이미 지난 날짜나 시각이라 등록하지 못했어요. 언제인지 다시 말씀해 주세요."


def try_again() -> str:
    return "제가 잘 못 알아들었어요. 다시 말씀해 주세요."


def unavailable() -> str:
    return "지금은 등록이 잘 안 되네요. 조금 있다가 다시 말씀해 주세요."
