"""사기 감지 때 어르신에게 들려줄 말 (TTS로 읽힘).

탓하거나 겁주지 않고, 짧게, 할 일 하나(돈 보내기 전에 가족에게 확인)만 분명하게 말한다.
보호자에게 알렸으면 숨기지 않고 알려 드린다 (몰래 알리는 느낌을 피함).
"""

from __future__ import annotations

from .models import ALREADY_ACTED, Detection

_WHY = {
    "family_impersonation": "가족이라며 문자나 전화로 급하게 돈을 보내 달라는 건 사기인 경우가 많아요.",
    "agency_impersonation": "검찰이나 경찰, 은행은 전화로 돈을 옮기라고 하지 않아요.",
    "investment": "큰 수익을 약속하는 투자는 사기인 경우가 많아요.",
    "loan": "대출해 준다며 먼저 돈을 내라는 건 사기인 경우가 많아요.",
    "romance": "직접 만나 보지 못한 사람이 돈을 부탁하면 사기일 수 있어요.",
    "prize_refund": "당첨이나 환급을 해 준다며 돈이나 정보를 달라는 건 사기인 경우가 많아요.",
    "remote_control": "모르는 앱을 깔거나 인증번호를 알려 주면 돈이 빠져나갈 수 있어요.",
    "other": "모르는 사람이 돈이나 개인정보를 달라고 하면 사기일 수 있어요.",
}


def warning(detection: Detection, *, told_guardian: bool) -> str:
    parts = ["잠깐만요.", _WHY.get(detection.category, _WHY["other"])]
    if detection.stage == ALREADY_ACTED:
        parts.append("이미 보내셨거나 알려 주셨다면, 지금 바로 112나 거래하신 은행에 전화하시면 막을 수 있을 때가 있어요.")
    else:
        parts.append("돈을 보내거나 번호를 알려 주기 전에 꼭 가족에게 먼저 확인해 주세요.")
    if told_guardian:
        parts.append("혹시 몰라서 보호자분께도 알려 드렸어요.")
    parts.append("가족에게 확인해 보실래요?")
    return " ".join(parts)


def refused_well() -> str:
    return "잘하셨어요. 그런 연락은 답하지 않고 지우는 게 가장 좋아요."


def reminder() -> str:
    """같은 대화에서 이미 경고한 뒤 다시 나왔을 때. 같은 경고를 반복하지 않는다."""
    return "아까 말씀드린 것처럼, 돈을 보내기 전에 꼭 가족에게 먼저 확인해 주세요."


def after_check() -> str:
    return "좋아요. 가족과 확인하실 때까지는 돈을 보내지 말아 주세요."


def after_call() -> str:
    return "보호자분께 전화 연결해 드릴게요."


def after_dismiss() -> str:
    return "알겠어요. 그래도 돈을 보내기 전에는 한 번만 더 확인해 주세요."


def ask_choice() -> str:
    return "가족에게 확인해 보실래요? 괜찮으시면 괜찮다고 말씀해 주세요."


# --- 응급 (#26) ---
# 진단하지 않고, 지금 할 일 하나와 연결 제안만 한다. 응급 처치는 누구나 할 수 있는 것만 말한다

_EMERGENCY_FIRST = {
    "fall": "많이 다치셨어요? 무리해서 일어나지 마시고 그대로 계세요.",
    "chest": "가슴이 그렇게 불편하시면 지금 바로 도움을 받는 게 좋겠어요.",
    "breathing": "숨쉬기 힘드시면 지금 바로 도움을 받는 게 좋겠어요.",
    "speech_or_weakness": "말이나 몸이 평소와 다르시면 지금 바로 도움을 받는 게 좋겠어요.",
    "fainting": "편하게 앉거나 누우세요. 지금 바로 도움을 받는 게 좋겠어요.",
    "bleeding_injury": "다친 곳을 깨끗한 천으로 꾹 눌러 주세요.",
    "choking": "지금 바로 도움을 받아야 해요.",
    "fire_gas": "지금 바로 밖으로 나가 주세요. 가스 냄새가 나면 불을 켜지 마세요.",
    "other_person": "지금 바로 도움을 받아야 해요.",
    "other": "괜찮으세요? 지금 바로 도움을 받는 게 좋겠어요.",
}


def emergency_warning(detection: Detection, *, told_guardian: bool) -> str:
    if detection.category == "self_harm":
        return self_harm_support(told_guardian=told_guardian)
    parts = [_EMERGENCY_FIRST.get(detection.category, _EMERGENCY_FIRST["other"])]
    if told_guardian:
        parts.append("보호자분께도 알려 드렸어요.")
        parts.append("119에 연결해 드릴까요, 보호자분께 전화해 드릴까요?")
    else:
        # 보호자가 없거나 발송이 모두 실패했으면 119에 직접 전화하도록 다시 안내한다 (명세 4-1)
        parts.append("보호자분께는 바로 연락이 닿지 않았어요. 많이 불편하시면 지금 119에 전화해 주세요.")
        parts.append("119에 연결해 드릴까요?")
    return " ".join(parts)


def self_harm_support(*, told_guardian: bool) -> str:
    parts = ["많이 힘드시군요. 말씀해 주셔서 고마워요. 혼자 견디지 않으셔도 돼요.",
             "109에 전화하시면 언제든 이야기를 들어 줘요."]
    if told_guardian:
        parts.append("보호자분께도 알려 드렸어요.")
    parts.append("지금 보호자분께 전화해 드릴까요?")
    return " ".join(parts)


def emergency_past() -> str:
    """이미 지난 일이라 알리지 않을 때."""
    return "그러셨군요. 지금은 괜찮으시다니 다행이에요. 혹시 다시 아프시거나 불편하면 바로 말씀해 주세요."


def emergency_reminder() -> str:
    return "아직 불편하시면 바로 도움을 받으세요. 119에 연결해 드릴까요?"


def self_harm_reminder() -> str:
    return "아직 많이 힘드시군요. 109에 전화하시면 언제든 이야기를 들어 줘요. 지금 보호자분께 전화해 드릴까요?"


def after_119() -> str:
    return "119에 연결해 드릴게요."


def after_helpline() -> str:
    return "109에 연결해 드릴게요."


def after_emergency_dismiss() -> str:
    return "알겠어요. 그래도 다시 아프시거나 이상하면 바로 말씀해 주세요."


def ask_emergency_choice() -> str:
    return "119에 연결해 드릴까요? '119' 또는 '딸에게'처럼 말씀해 주세요."
