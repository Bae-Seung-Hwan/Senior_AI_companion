"""감지 결과와 명세 4 요청 본문."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

FRAUD = "fraud"
EMERGENCY = "emergency"
HIGH = "high"
MEDIUM = "medium"

# 사기 유형 → 보호자에게 보여 줄 한 줄 설명 (명세 4 reason). LLM이 지어낸 글을 그대로 보내지 않으려고 틀로 만든다
FRAUD_CATEGORIES = {
    "family_impersonation": "가족·지인을 사칭해 돈을 요구하는 연락",
    "agency_impersonation": "검찰·경찰·금융기관을 사칭한 연락",
    "investment": "고수익을 약속하는 투자 권유",
    "loan": "대출을 해 준다며 돈이나 정보를 요구하는 연락",
    "romance": "직접 만나지 않은 사람의 돈 요구",
    "prize_refund": "당첨·환급을 미끼로 돈이나 정보를 요구하는 연락",
    "remote_control": "앱 설치·인증번호·비밀번호를 요구하는 연락",
    "other": "돈이나 개인정보를 요구하는 수상한 연락",
}

# 어르신이 어디까지 갔는지
REFUSED = "refused"              # 이미 사기인 줄 알고 무시·거절함 → 알림 없이 "잘하셨어요"
RECEIVED = "received"            # 연락·요구를 받았고 아직 하지 않음
ABOUT_TO_ACT = "about_to_act"    # 보내거나 알려 주려고 함
ALREADY_ACTED = "already_acted"  # 이미 보냈거나 알려 줬거나 설치함
STAGES = (REFUSED, RECEIVED, ABOUT_TO_ACT, ALREADY_ACTED)

_STAGE_NOTE = {
    RECEIVED: "",
    ABOUT_TO_ACT: " (어르신이 따르려고 하심)",
    ALREADY_ACTED: " (어르신이 이미 따르셨다고 하심)",
}

# 받기만 했어도 바로 급한 유형: 시간을 몰아붙여 그날 바로 돈이 나가는 경우가 많다
_URGENT_CATEGORIES = {"family_impersonation", "agency_impersonation", "remote_control"}


def fraud_severity(category: str, stage: str) -> str:
    """감지 결과만 보고 정하는 severity (명세 4: 어르신 선택에 따른 조정은 B가 함)."""
    if stage in (ABOUT_TO_ACT, ALREADY_ACTED):
        return HIGH
    return HIGH if category in _URGENT_CATEGORIES else MEDIUM


# --- 응급 (#26) ---

# 응급 유형 → 보호자에게 보여 줄 한 줄 설명. **진단처럼 들리지 않게** 어르신이 말한 사실만 적는다
# ("뇌졸중 의심" X → "말이 어눌하거나 몸 한쪽에 힘이 없다고 하심")
EMERGENCY_CATEGORIES = {
    "fall": "넘어지셨다고 하심",
    "chest": "가슴이 아프거나 조이거나 답답하다고 하심",
    "breathing": "숨쉬기 힘들다고 하심",
    "speech_or_weakness": "말이 어눌하거나 몸 한쪽에 힘이 없다고 하심",
    "fainting": "쓰러지거나 정신을 잃을 것 같다고 하심",
    "bleeding_injury": "크게 다치거나 피가 난다고 하심",
    "choking": "목에 무언가 걸렸다고 하심",
    "fire_gas": "불이나 연기, 가스 냄새 등 집 안의 위험을 말씀하심",
    "other_person": "함께 있는 분이 위급하다고 하심",
    "self_harm": "죽고 싶다는 등 마음이 많이 힘들다고 하심",
    "other": "급히 도움이 필요하다고 하심",
}

ACUTE = "acute"     # 지금 일어나고 있고 심함 (못 일어남, 많이 아픔, 숨이 참)
MILD = "mild"       # 지금이지만 견딜 만하다고 함 ("넘어졌는데 괜찮아")
RECENT = "recent"   # 방금·오늘 있었다가 지금은 괜찮다고 함 ("방금 잠깐 정신을 잃었어", "아까 말이 안 나왔는데 지금은 괜찮아")
PAST = "past"       # 오늘 이전의 일이거나 이미 진료를 받음 ("어제 넘어졌는데 이제 괜찮아") → 알리지 않음
EMERGENCY_STAGES = (ACUTE, MILD, RECENT, PAST)

_EMERGENCY_STAGE_NOTE = {ACUTE: "", MILD: " (지금은 견딜 만하다고 하심)", RECENT: " (방금 있었고 지금은 괜찮다고 하심)",
                         PAST: ""}

# 견딜 만하다고 하거나 방금 지나갔다고 해도 미루면 안 되는 유형: 본인이 괜찮다고 해도 시간이 중요하다
# (방금 잠깐 정신을 잃었거나 말이 안 나왔다가 돌아온 것도 바로 도움을 받아야 하는 경우가 많다)
_ALWAYS_HIGH = {"chest", "breathing", "speech_or_weakness", "fainting", "choking", "fire_gas", "other_person",
                "self_harm"}


def emergency_severity(category: str, stage: str) -> str:
    if stage == ACUTE or category in _ALWAYS_HIGH:
        return HIGH
    return MEDIUM


@dataclass(frozen=True)
class Detection:
    type: str
    category: str
    stage: str
    evidence_text: str          # [민감] 감지 근거 발화. 로그에 남기지 않는다
    detected_at: dt.datetime
    message_id: str | None = None
    source: str = "llm"         # llm / rules (LLM이 실패해서 규칙만으로 판단함)

    @property
    def should_alert(self) -> bool:
        if self.type == EMERGENCY:
            # "죽고 싶다"는 지난 일처럼 말해도 알린다
            if self.category == "self_harm":
                return True
            if self.stage == PAST:
                return False
            if self.stage == RECENT:  # 방금 지나간 일: 시간이 중요한 유형만 (가볍게 베인 것은 알리지 않음)
                return self.category in _ALWAYS_HIGH
            return True
        return self.stage != REFUSED

    @property
    def severity(self) -> str:
        if self.type == EMERGENCY:
            return emergency_severity(self.category, self.stage)
        return fraud_severity(self.category, self.stage)

    @property
    def reason(self) -> str:
        if self.type == EMERGENCY:
            text = EMERGENCY_CATEGORIES.get(self.category, EMERGENCY_CATEGORIES["other"])
            return text + _EMERGENCY_STAGE_NOTE.get(self.stage, "")
        return FRAUD_CATEGORIES.get(self.category, FRAUD_CATEGORIES["other"]) + " 의심" + _STAGE_NOTE.get(self.stage, "")

    def to_alert_request(self, *, request_id: str, user_id: str, conversation_id: str) -> dict:
        """명세 4-1 `POST /alerts` 요청 본문. 감지 즉시 보내므로 user_response는 pending."""
        return {
            "request_id": request_id,
            "user_id": user_id,
            "type": self.type,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": {
                "text": self.evidence_text,
                "conversation_id": conversation_id,
                "message_id": self.message_id,
            },
            "user_response": "pending",
            "detected_at": self.detected_at.isoformat(timespec="seconds"),
        }

    def log_record(self) -> dict:
        """대화 기록에 남길 감지 기록. 발화 원문은 넣지 않는다 (원문은 대화 기록 쪽에 이미 있음)."""
        return {
            "type": self.type,
            "category": self.category,
            "stage": self.stage,
            "severity": self.severity if self.should_alert else None,
            "source": self.source,
            "message_id": self.message_id,
            "detected_at": self.detected_at.isoformat(timespec="seconds"),
        }
