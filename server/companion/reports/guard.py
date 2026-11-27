"""요약 문장 검사. LLM이 규칙을 어겨도 보호자에게 나가기 전에 규칙으로 한 번 더 막는다.

- brief는 공유 범위와 상관없이 항상 보호자에게 가고, 잠금 화면에 뜨는 푸시 본문에도 들어간다 (명세 3).
  그래서 LLM에게 맡기지 않고 대화 횟수로 **틀에 맞춰 만든다** (template_brief). 내용이 샐 길이 없고,
  대화 횟수는 stats.conversation_count로 어차피 항상 가는 정보라 새로 알려지는 것도 없다 (#73 리뷰, B 추천)
- 각 section은 동의한 보호자에게만 따로 간다 → 다른 항목 내용이 섞이면 안 됨
  (예: 기분만 공유했는데 mood에 "치과 가기 귀찮아하셨어요"가 있으면 일정이 새어 나감)
- 모든 문장: 의료 표현 금지 ("치매 증상" X → "같은 질문을 여러 번 하셨어요" O)

단어 목록은 완벽하지 않다. 걸리면 다시 쓰게 하고, 그래도 걸리면 안전한 쪽(빼기)으로 간다.
"""

from __future__ import annotations

# 짧은 말은 다른 낱말 안에 들어 있기 쉬워서("스마트폰"의 "마트", "붓글씨"의 "붓") 조사까지 붙여 좁힌다
SCHEDULE_WORDS = ("병원", "치과", "안과", "내과", "한의원", "약국", "약속", "예약", "일정", "다녀오", "다녀왔",
                  "만나", "모임", "복지관", "경로당", "교회", "성당", "은행", "외출", "산책", "시장에", "마트에",
                  "장 보", "장을 보")
HEALTH_WORDS = ("아프", "아파", "아픈", "쑤시", "쑤셔", "통증", "무릎", "허리", "다리가", "기침", "감기", "열이",
                "넘어지", "어지러", "피곤", "잠을 못", "잠을 설", "잠이 안", "못 주무", "잘 주무", "입맛",
                "끼니를 거르", "식사를 거르", "혈압을 재", "혈압이", "혈당", "부었", "부어", "붓기", "침침",
                "치료", "약")
# "기운이 없다", "걱정된다"는 몸 상태 이야기에도 자연스럽게 나와서 넣지 않는다.
# "화가"·"화나"는 "대화가"·"대화나" 안에 들어 있어서 "화가 나", "화나셨"처럼 좁힌다
MOOD_WORDS = ("기분", "즐거", "즐겁", "행복", "기쁘", "기뻐", "우울", "슬프", "슬퍼", "외로", "쓸쓸", "속상",
              "화가 나", "화가 났", "화를 내", "화를 냈", "화나셨", "화났", "화내셨", "처지", "웃으", "귀찮")

# 각 section에 있으면 안 되는 말
_NOT_ALLOWED = {
    # 복약은 일정에 들어간다 ("혈압약도 잘 챙겨 드셨어요") → "약"·"치료"는 허용
    "schedule": tuple(w for w in HEALTH_WORDS if w not in ("약", "치료")) + MOOD_WORDS,
    "health": SCHEDULE_WORDS + MOOD_WORDS,
    "mood": SCHEDULE_WORDS + HEALTH_WORDS,
}
_LABEL = {"schedule": "건강·기분", "health": "일정·기분", "mood": "일정·건강"}

# 검사 전에 지우는 말: 걸리는 낱말이 들어 있지만 그 뜻이 아닌 것
_NOT_A_MATCH = (
    "약간", "약해", "약하",          # "약"(복약)이 아님 ("마음이 약해지셨는지")
    "주무시기 전",                    # 복약 때를 말한 것 ("주무시기 전에 혈압약을 챙겨 드셨어요")
)

# 어느 문장에도 쓰지 않는 의료 표현 (기획서 "의료 표현 금지")
MEDICAL_BANNED = (
    "치매", "인지장애", "인지 장애", "인지 기능", "인지기능", "알츠하이머", "건망증", "기억력",
    "우울증", "불면증", "고혈압", "당뇨", "관절염", "파킨슨", "뇌졸중", "골다공증",
    "진단", "증상", "질환", "병명", "처방", "보청기", "난청", "의심됩니다",
    # 의료 조언
    "치료가 필요", "치료를 받", "가 보셔야", "가보셔야", "검사를 받", "검사가 필요",
)

_COUNT_KO = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열"}


def template_brief(conversation_count: int) -> str:
    """brief: 대화 횟수만 담은 한 문장. 일정·건강·기분 내용이 들어갈 수 없다."""
    if conversation_count <= 0:
        return "오늘 하루도 보내셨어요."  # 대화가 없는 날은 배치가 보내지 않지만, 혹시 몰라 내용 없는 문장으로
    times = f"{_COUNT_KO[conversation_count]} 번" if conversation_count in _COUNT_KO else f"{conversation_count}번"
    return f"오늘 AI와 {times} 이야기를 나누셨어요."


def _strip_non_matches(text: str) -> str:
    for phrase in _NOT_A_MATCH:
        text = text.replace(phrase, " ")
    return text


def mixed_words(name: str, text: str | None) -> list[str]:
    """그 칸에 섞이면 안 되는 다른 항목의 말."""
    if not text:
        return []
    text = _strip_non_matches(text)
    return [w for w in _NOT_ALLOWED[name] if w in text]


def medical_problems(text: str | None) -> list[str]:
    if not text:
        return []
    return [w for w in MEDICAL_BANNED if w in text]


def problems(name: str, text: str | None) -> list[str]:
    """다시 쓰게 할 때 알려 줄 문제 목록."""
    found = [f"{name}에 {_LABEL[name]} 내용 '{w}'" for w in mixed_words(name, text)]
    found += [f"{name}에 의료 표현 '{w}'" for w in medical_problems(text)]
    return found
