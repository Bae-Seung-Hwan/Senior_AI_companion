"""사기(#25)·응급(#26) 문장 감지: 규칙 문지기 → Claude 판단 → (LLM 실패 시) 규칙 안전망.

모델은 할 일 추출과 같은 Claude Haiku 5.5 (docs/research/voice-stack.md 잠정 결론). 대화 턴 안에서 돌기 때문에
지연이 중요하지만, 놓치면 되돌릴 수 없는 판단이라 effort는 할 일 추출(low)보다 한 단계 높은 medium으로 둔다.
유형·단계만 모델에게 받고, severity와 보호자에게 보일 설명(reason)은 코드가 정한다 (models.py).
"""

from __future__ import annotations

import datetime as dt
import logging

from ..clock import KST
from ..llm import LLMCallError, LLMOutputError, call_json, voice_client
from . import emergency_rules, rules
from .models import (
    EMERGENCY,
    EMERGENCY_CATEGORIES,
    EMERGENCY_STAGES,
    FRAUD,
    FRAUD_CATEGORIES,
    STAGES,
    Detection,
)

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

EMERGENCY_PROMPT = """어르신과 AI 말벗의 대화에서, 어르신이 **방금 한 말**이 지금 바로 도움이 필요한 응급 상황인지 판단해 JSON으로만 답합니다.
보호자 알림과 119 연결 제안에 쓰입니다. 진단하지 않고, 어르신이 말한 상황만 봅니다.

응급으로 보는 것 (is_emergency: true):
- fall: 넘어지거나 미끄러짐 ("화장실에서 넘어져서 못 일어나겠어")
- chest: 가슴이 아프거나 조이거나 답답함 (몸의 증상일 때. "아들 생각에 가슴이 답답해"처럼 마음 이야기는 아님)
- breathing: 숨쉬기 힘듦, 숨이 참
- speech_or_weakness: 말이 어눌함, 혀가 꼬임, 몸 한쪽에 힘이 없거나 감각이 없음, 얼굴 한쪽이 처짐
- fainting: 쓰러짐, 정신을 잃을 것 같음, 심한 어지러움
- bleeding_injury: 크게 다침, 피가 멈추지 않음, 뼈가 부러진 것 같음
- choking: 목에 음식 등이 걸림
- fire_gas: 불, 연기, 타는 냄새, 가스 냄새 등 집 안의 위험
- other_person: 함께 있는 사람(배우자 등)이 쓰러지거나 숨을 안 쉬는 등 위급함
- self_harm: 죽고 싶다, 살기 싫다, 사라지고 싶다처럼 스스로를 해치려는 마음 (관용 표현이 아닐 때)
- other: 위에 없지만 지금 급히 도움이 필요함 ("살려 줘", "구급차 불러 줘")

응급으로 보지 않는 것 (is_emergency: false):
- 관용 표현 ("더워 죽겠어", "배불러 죽겠어", "웃겨 죽는 줄 알았어", "숨이 차게 웃었어")
- 평소의 가벼운 아픔·몸 이야기 ("무릎이 좀 쑤셔", "감기 기운이 있어", "허리가 뻐근해")
- 넘어질 뻔했지만 넘어지지 않음, 남의 일·뉴스·드라마 이야기, 멀리 있는 가족의 가벼운 일 ("손주가 넘어졌는데 괜찮대")
- 마음의 답답함·걱정·서운함 ("자식들이 연락이 없어서 가슴이 답답해"). 단 죽고 싶다는 말은 self_harm

stage (is_emergency가 true일 때):
- acute: 지금 일어나고 있고 심하거나, 심한지 알 수 없음
- mild: 지금이지만 어르신이 견딜 만하다고 함 ("넘어졌는데 크게 안 다친 것 같아")
- recent: 방금·오늘 있었다가 지금은 괜찮다고 함 ("방금 잠깐 정신을 잃었던 것 같아", "아까 말이 안 나오다가 돌아왔어")
- past: 오늘 이전의 일이거나 이미 진료를 받아서 지금은 괜찮다고 함 ("어제 넘어졌었는데 이제 괜찮아")
is_emergency가 false면 category와 stage는 "none"입니다.

confidence: 확실하면 high, 그럴 수 있으면 medium, 근거가 약하면 low.
헷갈리면 응급 쪽으로 봅니다 (놓치는 것이 더 위험합니다). 최근 대화는 맥락으로만 씁니다."""

EMERGENCY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_emergency": {"type": "boolean"},
        "category": {"type": "string", "enum": [*EMERGENCY_CATEGORIES, "none"]},
        "stage": {"type": "string", "enum": [*EMERGENCY_STAGES, "none"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["is_emergency", "category", "stage", "confidence"],
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
    system = SYSTEM_PROMPT
    schema = FRAUD_SCHEMA
    verdict_key = "is_fraud"
    label = "사기"

    def __init__(self, client=None, model: str = DEFAULT_MODEL, effort: str = "medium", api_errors=None):
        # 대화 턴 안이라 짧게 기다린다. 늦으면 규칙 안전망으로 넘어감 (companion/llm.py)
        self.client, self.api_errors = voice_client(client, api_errors)
        self.model = model
        self.effort = effort

    def classify(self, utterance: str, recent: tuple[str, ...] | list[str] = ()) -> dict:
        try:
            raw = call_json(self.client, self.api_errors, model=self.model, system=self.system,
                            content=build_user_message(utterance, recent), schema=self.schema,
                            effort=self.effort, max_tokens=1024)
        except (LLMCallError, LLMOutputError) as e:  # 원문은 로그에 남기지 않는다
            log.warning("%s 판단 실패: %s", self.label, e)
            raise DetectionError(str(e)) from e
        if not isinstance(raw.get(self.verdict_key), bool):
            raise DetectionError("판단 결과가 형식에 맞지 않음")
        return raw


class ClaudeEmergencyClassifier(ClaudeFraudClassifier):
    """응급 판단. 사기 판단과 같은 모델·호출 방식에 프롬프트와 스키마만 다르다."""

    system = EMERGENCY_PROMPT
    schema = EMERGENCY_SCHEMA
    verdict_key = "is_emergency"
    label = "응급"


class FraudDetector:
    """detect()가 Detection을 돌려주면 의심, None이면 아님. 응급 감지기(EmergencyDetector)도 같은 흐름이다."""

    type = FRAUD
    gate = rules
    verdict_key = "is_fraud"
    categories = FRAUD_CATEGORIES
    stages = STAGES
    unknown_stage = "received"  # 모르는 단계 값이 오면 안전한 쪽
    skip_low_confidence = True

    def __init__(self, classifier, *, clock=lambda: dt.datetime.now(KST)):
        self.classifier = classifier
        self.clock = clock

    def detect(self, text: str, *, message_id: str | None = None,
               recent: tuple[str, ...] | list[str] = ()) -> Detection | None:
        if not self.gate.is_candidate(text, recent):
            return None  # 관련 낱말이 없으면 LLM을 부르지 않는다
        try:
            raw = self.classifier.classify(text, recent)
        except DetectionError:
            signal = self.gate.strong_signal(text, recent)
            if signal is None:
                return None
            log.warning("%s 판단 실패, 규칙만으로 알림: %s/%s", self.type, *signal)
            return self._detection(text, message_id, *signal, source="rules")

        if not raw.get(self.verdict_key):
            return None
        if raw.get("confidence") == "low" and self.skip_low_confidence:
            return None
        category = raw.get("category") if raw.get("category") in self.categories else "other"
        stage = raw.get("stage") if raw.get("stage") in self.stages else self.unknown_stage
        return self._detection(text, message_id, category, stage, source="llm")

    def _detection(self, text, message_id, category, stage, *, source) -> Detection:
        return Detection(type=self.type, category=category, stage=stage, evidence_text=text,
                         detected_at=self.clock(), message_id=message_id, source=source)


class EmergencyDetector(FraudDetector):
    """응급 감지 (#26). 놓치면 안 되므로 LLM 확신이 낮아도(low) 알린다 (사기와 다름)."""

    type = EMERGENCY
    gate = emergency_rules
    verdict_key = "is_emergency"
    categories = EMERGENCY_CATEGORIES
    stages = EMERGENCY_STAGES
    unknown_stage = "acute"
    skip_low_confidence = False
