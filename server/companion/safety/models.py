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
        return self.stage != REFUSED

    @property
    def severity(self) -> str:
        return fraud_severity(self.category, self.stage)

    @property
    def reason(self) -> str:
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
