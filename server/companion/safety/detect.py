"""사기 문장 감지 (#25): 규칙 문지기 → Claude 판단 → (LLM 실패 시) 규칙 안전망.

모델은 할 일 추출과 같은 Claude Haiku 5.5 (docs/research/voice-stack.md 잠정 결론). 대화 턴 안에서 돌기 때문에
지연이 중요하지만, 놓치면 돈이 나가는 판단이라 effort는 할 일 추출(low)보다 한 단계 높은 medium으로 둔다.
유형·단계만 모델에게 받고, severity와 보호자에게 보일 설명(reason)은 코드가 정한다 (models.py).
"""

from __future__ import annotations

import datetime as dt
import logging

from ..clock import KST
from ..llm import LLMCallError, LLMOutputError, call_json, voice_client
from . import rules
from .models import FRAUD, FRAUD_CATEGORIES, STAGES, Detection

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM_PROMPT = """어르신과 AI 말벗의 대화에서, 어르신이 **방금 한 말**이 사기 피해로 이어질 수 있는 상황인지 판단해 JSON으로만 답합니다.

사기로 보는 것 (is_fraud: true):
- family_impersonation: 가족·지인이라며 문자·메신저·전화로 급하게 돈이나 정보를 요구 ("아들이 폰이 고장 났다며 문자로 돈 보내래", "손주가 사고 났다고 합의금 보내래")
- agency_impersonation: 검찰·경찰·금감원·은행·국세청·택배를 사칭 ("검사라는데 내 통장이 범죄에 쓰였대", "안전한 계좌로 옮기래")
- investment: 고수익·원금 보장 투자, 코인, 주식 리딩방 ("넣으면 두 배 준대", "리딩방에서 코인 사래")
- loan: 저금리 대출을 해 준다며 먼저 수수료·보증금·기존 대출 상환금을 요구
- romance: 직접 만나 본 적 없는 사람(온라인·SNS·해외)이 돈이나 선물을 요구
- prize_refund: 당첨·환급·지원금을 준다며 수수료나 정보를 요구
- remote_control: 모르는 앱 설치, 링크 누르기, 인증번호·비밀번호·카드번호·신분증 사진을 요구
- other: 위에 없지만 모르는 사람이 돈이나 개인정보를 요구

사기로 보지 않는 것 (is_fraud: false):
- 가족이 직접 만나서 받는 용돈·생활비, 어르신이 스스로 주는 용돈 ("손주 용돈 줬어")
- 공과금·관리비·병원비 내기, 은행 창구에서 적금 들기처럼 어르신이 스스로 하는 평범한 돈 일
- 사기 뉴스나 남의 일 이야기 ("옆집 할머니가 당했대", "요즘 보이스피싱 많다며")
- 아는 사람끼리의 평범한 돈 이야기 ("친구한테 밥 샀어", "계모임 회비 냈어")

stage (is_fraud가 true일 때):
- refused: 어르신이 이미 사기인 줄 알고 무시·거절·차단함 ("이상해서 그냥 지웠어")
- received: 그런 연락·요구를 받았고 아직 따르지 않음
- about_to_act: 따르려고 함 ("이따 보내 주려고", "지금 은행 가는 중이야")
- already_acted: 이미 보냈거나, 알려 줬거나, 설치함
is_fraud가 false면 stage는 "none", category는 "none"입니다.

confidence: 확실하면 high, 그럴 수 있으면 medium, 근거가 약하면 low.
최근 대화는 맥락으로만 쓰고, 판단은 방금 한 말을 기준으로 합니다."""

FRAUD_SCHEMA = {
    "type": "object",
    "properties": {
        "is_fraud": {"type": "boolean"},
        "category": {"type": "string", "enum": [*FRAUD_CATEGORIES, "none"]},
        "stage": {"type": "string", "enum": [*STAGES, "none"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["is_fraud", "category", "stage", "confidence"],
    "additionalProperties": False,
}


class DetectionError(Exception):
    """LLM 판단 실패 (API 오류, 거절, 형식 오류). 감지기가 규칙 안전망으로 넘어간다."""


def build_user_message(utterance: str, recent: tuple[str, ...] | list[str] = ()) -> str:
    lines = []
    if recent:
        lines.append("최근 대화 (맥락):")
        lines.extend(f"- {r}" for r in recent)
    lines.append(f"어르신이 방금 한 말: {utterance}")
    return "\n".join(lines)


class ClaudeFraudClassifier:
    def __init__(self, client=None, model: str = DEFAULT_MODEL, effort: str = "medium", api_errors=None):
        # 대화 턴 안이라 짧게 기다린다. 늦으면 규칙 안전망으로 넘어감 (companion/llm.py)
        self.client, self.api_errors = voice_client(client, api_errors)
        self.model = model
        self.effort = effort

    def classify(self, utterance: str, recent: tuple[str, ...] | list[str] = ()) -> dict:
        try:
            raw = call_json(self.client, self.api_errors, model=self.model, system=SYSTEM_PROMPT,
                            content=build_user_message(utterance, recent), schema=FRAUD_SCHEMA,
                            effort=self.effort, max_tokens=1024)
        except (LLMCallError, LLMOutputError) as e:  # 원문은 로그에 남기지 않는다
            log.warning("사기 판단 실패: %s", e)
            raise DetectionError(str(e)) from e
        if not isinstance(raw.get("is_fraud"), bool):
            raise DetectionError("판단 결과가 형식에 맞지 않음")
        return raw


class FraudDetector:
    """detect()가 Detection을 돌려주면 사기 의심, None이면 아님."""

    def __init__(self, classifier, *, clock=lambda: dt.datetime.now(KST)):
        self.classifier = classifier
        self.clock = clock

    def detect(self, text: str, *, message_id: str | None = None,
               recent: tuple[str, ...] | list[str] = ()) -> Detection | None:
        if not rules.is_candidate(text, recent):
            return None  # 돈·정보·기관 이야기가 없으면 LLM을 부르지 않는다
        try:
            raw = self.classifier.classify(text, recent)
        except DetectionError:
            signal = rules.strong_signal(text, recent)
            if signal is None:
                return None
            log.warning("사기 판단 실패, 규칙만으로 알림: %s/%s", *signal)
            return self._detection(text, message_id, *signal, source="rules")

        if not raw["is_fraud"] or raw.get("confidence") == "low":
            return None
        category = raw.get("category") if raw.get("category") in FRAUD_CATEGORIES else "other"
        stage = raw.get("stage") if raw.get("stage") in STAGES else "received"
        return self._detection(text, message_id, category, stage, source="llm")

    def _detection(self, text, message_id, category, stage, *, source) -> Detection:
        return Detection(type=FRAUD, category=category, stage=stage, evidence_text=text,
                         detected_at=self.clock(), message_id=message_id, source=source)
