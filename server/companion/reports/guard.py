"""요약 문장 검사. LLM이 규칙을 어겨도 보호자에게 나가기 전에 규칙으로 한 번 더 막는다.

- brief는 공유 범위와 상관없이 항상 보호자에게 간다 → 일정·건강·기분 내용이 있으면 안 됨 (명세 3)
- 각 section은 동의한 보호자에게만 따로 간다 → 다른 항목 내용이 섞이면 안 됨
  (예: 기분만 공유했는데 mood에 "치과 가기 귀찮아하셨어요"가 있으면 일정이 새어 나감)
- 모든 문장: 의료 표현 금지 ("치매 증상" X → "같은 질문을 여러 번 하셨어요" O)

단어 목록은 완벽하지 않다. 걸리면 다시 쓰게 하고, 그래도 걸리면 안전한 쪽(빼기)으로 간다.
"""

from __future__ import annotations

SCHEDULE_WORDS = ("병원", "치과", "약속", "예약", "일정", "다녀오", "다녀왔", "만나", "모임", "복지관", "은행",
                  "외출", "시장", "마트")
HEALTH_WORDS = ("아프", "아파", "아픈", "쑤시", "쑤셔", "통증", "무릎", "허리", "기침", "열이", "넘어지", "어지러",
                "치료", "약")
MOOD_WORDS = ("기분", "즐거", "즐겁", "행복", "기쁘", "기뻐", "우울", "슬프", "슬퍼", "외로", "속상", "화가", "걱정",
              "웃으", "귀찮", "기운")

# 각 칸에 있으면 안 되는 말
_NOT_ALLOWED = {
    "brief": SCHEDULE_WORDS + HEALTH_WORDS + MOOD_WORDS,
    # 복약은 일정에 들어간다 ("혈압약도 잘 챙겨 드셨어요") → "약"·"치료"는 허용
    "schedule": tuple(w for w in HEALTH_WORDS if w not in ("약", "치료")) + MOOD_WORDS,
    "health": SCHEDULE_WORDS + MOOD_WORDS,
    "mood": SCHEDULE_WORDS + HEALTH_WORDS,
}
_LABEL = {"brief": "일정·건강·기분", "schedule": "건강·기분", "health": "일정·기분", "mood": "일정·건강"}

# 어느 문장에도 쓰지 않는 의료 표현 (기획서 "의료 표현 금지")
MEDICAL_BANNED = (
    "치매", "인지장애", "인지 장애", "알츠하이머", "우울증", "불면증", "진단", "증상", "질환", "병명",
    "처방", "보청기", "난청", "의심됩니다",
)

SAFE_BRIEF = "오늘도 이야기 나누며 하루를 보내셨어요."


def mixed_words(name: str, text: str | None) -> list[str]:
    """그 칸에 섞이면 안 되는 다른 항목의 말."""
    if not text:
        return []
    text = text.replace("약간", "")  # "약"(복약)으로 잘못 걸리지 않게
    return [w for w in _NOT_ALLOWED[name] if w in text]


def brief_problems(brief: str) -> list[str]:
    return mixed_words("brief", brief)


def medical_problems(text: str | None) -> list[str]:
    if not text:
        return []
    return [w for w in MEDICAL_BANNED if w in text]


def problems(name: str, text: str | None) -> list[str]:
    """다시 쓰게 할 때 알려 줄 문제 목록."""
    found = [f"{name}에 {_LABEL[name]} 내용 '{w}'" for w in mixed_words(name, text)]
    found += [f"{name}에 의료 표현 '{w}'" for w in medical_problems(text)]
    return found
