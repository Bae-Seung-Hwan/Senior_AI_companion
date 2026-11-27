"""안전 감지 대화 흐름: 사기(#25)·응급(#26) → B 긴급 알림(#28, 명세 4).

대화 하나에 SafetyConversation 하나. 어르신이 말할 때마다 할 일 흐름보다 **먼저** handle()을 부른다.
None이면 위험한 말이 아니므로 다음 처리(할 일, 일반 대화)로 넘어간다.

1. 감지기를 차례로 돌린다 (응급 먼저, 다음 사기). 의심되면 **곧바로** B에 `POST /alerts`(pending)를 보내고
   경고를 들려 드린다 (명세 4-1)
2. 어르신의 대답을 `PATCH /alerts/{id}`로 보낸다 (명세 4-2).
   - 사기: "가족에게 확인할게" / "전화 걸어 줘" / "괜찮아"
   - 응급: "119 불러 줘" / "딸한테 전화해 줘" / "괜찮아"
   화면 버튼(#27)은 respond()를, 정해진 시간 동안 대답이 없으면 expire()를 부른다 (no_response).
   전화를 걸어야 하는 선택이면 action에 남기고, 앱이 그 번호로 연결한다
3. 대답을 기다리는 동안에도 **감지는 계속**한다. 더 위급해지거나 다른 위험이 나오면 새로 알린다
4. B가 응답하지 않아도 경고는 그대로 들려 드리고, 보내지 못한 요청은 같은 request_id로 flush()에서 다시 보낸다

한 대화에서 **같은 종류·유형**으로 경고를 되풀이하지 않는다. 더 위급해졌을 때(medium → high)만 새로 알린다.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass, field

from ..clock import KST
from ..todos.parse import NO, YES, is_asking_back, parse_yes_no
from . import speech
from .b_client import ACCEPTED, ALREADY_RESPONDED, PERMANENT_FAILURES, AlertApi
from .models import EMERGENCY, HIGH, Detection

log = logging.getLogger(__name__)

# 명세 4 user_response
GUARDIAN_CHECK = "guardian_check_requested"
GUARDIAN_CALL = "guardian_call_requested"
CALL_119 = "call_119_requested"
DISMISSED = "dismissed"
NO_RESPONSE = "no_response"
# 명세 4에 없는 선택: "죽고 싶다" 뒤 109(자살예방 상담전화) 연결. 맞는 값이 없어서(call_119_requested로 보내면
# 보호자가 구급차를 부른 줄 안다) **4-2를 보내지 않는다.** B에는 high/pending으로 남아 보호자 재알림이 계속된다.
# 회의(#68)에서 명세에 값을 정하면 _TO_SPEC에 넣는다
HELPLINE_109 = "helpline_109"
CHOICES = (GUARDIAN_CHECK, GUARDIAN_CALL, CALL_119, DISMISSED, NO_RESPONSE, HELPLINE_109)
_TO_SPEC: dict[str, str] = {}
_NOT_IN_SPEC = {HELPLINE_109}

# 앱이 걸 전화 (action)
CALL_ACTIONS = {CALL_119: "call_119", GUARDIAN_CALL: "call_guardian", HELPLINE_109: "call_109"}

_CALL = ("전화해 줘", "전화해줘", "전화 걸어", "전화 좀 걸어", "연결해", "전화 좀 해 줘", "전화 좀 해줘")
_DISMISS = ("괜찮아", "괜찮습니다", "사기 아니", "사기가 아니", "진짜 아들", "진짜 딸", "진짜 손주", "진짜야",
            "내가 알아서", "필요 없", "필요없", "아니야", "아니에요", "아냐")
_CHECK = ("확인할게", "확인해 볼게", "확인해볼게", "확인해 봐야", "물어볼게", "물어봐야", "물어 볼게", "전화해 볼게",
          "전화해볼게", "연락해 볼게", "연락해볼게", "알았어", "알겠어", "알겠습니다", "그럴게", "그래야겠")
_119 = re.compile(r"119|구급차|응급실|병원(에|으로)? ?(데려|가야|좀)|앰뷸런스")
_109 = re.compile(r"109|상담 ?(해|받|전화)")
_GUARDIAN = re.compile(r"(딸|아들|보호자|가족|며느리|사위|남편|아내|자식|큰애|작은애)\S*\s*(한테|에게|께)?\s*"
                       r"\S*(전화|연락|불러|알려)")
_EMERGENCY_DISMISS = ("괜찮아", "괜찮습니다", "괜찮아졌", "필요 없", "필요없", "안 불러도", "됐어")
# "안 괜찮아"는 괜찮다는 뜻이 아니다 → 선택으로 보지 않고 다시 묻는다
_NOT_OK = re.compile(r"안 ?괜찮|괜찮지 ?(가 )?않|괜찮긴 ?뭐가|하나도 안")
# "119는 부르지 마", "아들한테 연락하지 말고": 그 부분은 요청이 아니다. 앞 낱말(누구에게)과 함께 지운다
_NEGATED_CALL = re.compile(r"(\S+\s*)?(부르|연락하|전화하|알리|걸)지\s*(마|말)\S*"
                           r"|(119|구급차|\S+(한테|에게|께))(는|은)?\s*말고")  # "119 말고 딸한테"

_RANK = {"medium": 1, HIGH: 2}
_SHORT_ANSWER = 10  # 글자 수

# 경고 끝에 무엇을 물었는지 → 그냥 "네"라고 했을 때의 뜻
ASKED_BOTH = "both"          # "119에 연결해 드릴까요, 보호자분께 전화해 드릴까요?" → "네"만으로는 모름
ASKED_119 = "119"            # "119에 연결해 드릴까요?"
ASKED_GUARDIAN = "guardian"  # "지금 보호자분께 전화해 드릴까요?" (죽고 싶다는 말 뒤)
ASKED_CHECK = "check"        # 사기: "가족에게 확인해 보실래요?"


def parse_choice(text: str) -> str | None:
    """사기 경고 뒤 어르신의 대답. 못 알아들으면 None ("네?"처럼 되묻는 말 포함)."""
    if is_asking_back(text):
        return None
    if any(p in text for p in _CALL):
        return GUARDIAN_CALL
    # "아니야, 확인해 볼게"처럼 섞이면 안전한 쪽(확인)으로 본다
    if any(p in text for p in _CHECK):
        return GUARDIAN_CHECK
    if any(p in text for p in _DISMISS):
        return DISMISSED
    # 그냥 "네"·"아니"는 짧은 대답일 때만 선택으로 본다. "내일 치과 예약해 줘"의 "해 줘"를 동의로 읽지 않게
    if len(text.strip()) > _SHORT_ANSWER:
        return None
    answer = parse_yes_no(text)
    if answer == YES:
        return GUARDIAN_CHECK
    if answer == NO:  # "아니", "싫어", "됐어"
        return DISMISSED
    return None


def parse_emergency_choice(text: str, asked: str = ASKED_BOTH) -> str | None:
    """응급 경고 뒤 어르신의 대답. 119와 보호자가 함께 나오면 119 (안전한 쪽)."""
    if is_asking_back(text) or _NOT_OK.search(text):
        return None
    if _NEGATED_CALL.search(text):
        text = _NEGATED_CALL.sub(" ", text)
        if not (_119.search(text) or _109.search(text) or _GUARDIAN.search(text) or any(p in text for p in _CALL)):
            return DISMISSED  # "119는 부르지 마", "아들한테 연락하지 마"
    if _119.search(text):
        return CALL_119
    if _109.search(text):
        return HELPLINE_109
    if _GUARDIAN.search(text) or any(p in text for p in _CALL):
        return GUARDIAN_CALL
    if any(p in text for p in _EMERGENCY_DISMISS):
        return DISMISSED
    if len(text.strip()) > _SHORT_ANSWER:
        return None
    answer = parse_yes_no(text)
    if answer == YES:
        return {ASKED_119: CALL_119, ASKED_GUARDIAN: GUARDIAN_CALL}.get(asked)  # 둘 다 물었으면 모름 → 다시 물음
    if answer == NO:
        return DISMISSED
    return None


@dataclass
class _Alert:
    detection: Detection
    post_body: dict
    asked: str = ASKED_BOTH
    alert_id: str | None = None
    post_gave_up: bool = False      # 다시 보내도 결과가 같은 4xx라 더 보내지 않음
    response_body: dict | None = None
    response_sent: bool = False     # 보냈거나, 다시 보내도 소용없어서 그만둠
    event: dict = field(default_factory=dict)

    @property
    def is_emergency(self) -> bool:
        return self.detection.type == EMERGENCY

    @property
    def done(self) -> bool:
        if self.post_gave_up:
            return True
        return self.alert_id is not None and (self.response_body is None or self.response_sent)


class SafetyConversation:
    def __init__(
        self,
        *,
        user_id: str,
        conversation_id: str,
        api: AlertApi,
        detector=None,
        detectors=(),
        clock=lambda: dt.datetime.now(KST),
        new_request_id=lambda: str(uuid.uuid4()),
    ):
        """detectors: 보통 [EmergencyDetector(...), FraudDetector(...)]. detector는 하나만 줄 때."""
        self.user_id = user_id
        self.conversation_id = conversation_id
        found = [d for d in (*detectors, detector) if d is not None]
        if not found:
            raise ValueError("감지기가 하나도 없습니다 (detectors=[EmergencyDetector(...), FraudDetector(...)])")
        # 응급 감지기를 항상 먼저 돌린다 (넘겨준 순서와 상관없이)
        self.detectors = sorted(found, key=lambda d: getattr(d, "type", None) != EMERGENCY)
        self.api = api
        self.clock = clock
        self.new_request_id = new_request_id
        self.alerts: list[_Alert] = []
        self.awaiting: _Alert | None = None  # 경고를 들려 드리고 대답을 기다리는 알림
        self.asked_again = False
        self.recent: list[str] = []
        # 앱이 걸어야 할 전화 ("call_119" / "call_guardian" / "call_109"). 앱이 읽고 None으로 돌린다
        self.action: str | None = None
        # 대화 기록에 남길 감지 기록 (#25 완료 조건). 발화 원문은 넣지 않는다
        self.events: list[dict] = []

    # --- 어르신의 말 ---

    def handle(self, text: str, message_id: str | None = None) -> str | None:
        try:
            choice = self._choice(text) if self.awaiting is not None else None
            # 대답을 기다리는 중에도 감지는 한다 ("알았어, 그래도 지금 보낼게", "아까보다 더 아파")
            # 응급 경고에 "119 불러 줘"처럼 분명히 고른 대답은 그 자체가 도움 요청이라 응급 감지를 다시 돌리지 않는다
            # (돌리면 "그 밖의 도움 요청"으로 새 알림이 또 나감). 사기 감지는 그대로 돌린다
            calling = choice in (CALL_119, GUARDIAN_CALL, HELPLINE_109)
            skip = {EMERGENCY} if self.awaiting is not None and self.awaiting.is_emergency and calling else set()
            kind, said = self._detect(text, message_id, choice, skip)
            if kind == "alert":
                return said  # 새 경고. 앞 알림에는 이 대답(또는 no_response)을 이미 보냄
            if self.awaiting is not None:
                if choice is not None:
                    return self.respond(choice)
                if said is None and not self.asked_again and self._should_ask_again(text):
                    self.asked_again = True
                    if not self.awaiting.is_emergency:
                        return speech.ask_choice()
                    if self.awaiting.asked == ASKED_GUARDIAN:
                        return speech.self_harm_reminder()
                    self.awaiting.asked = ASKED_119  # 다시 물을 때는 119만 묻는다 → 다음 "네"는 119
                    return speech.ask_emergency_choice()
                # 대답이 아닌 말은 붙잡지 않는다 → 할 일·일반 대화로. 화면 버튼이나 expire()를 기다림
            return said
        finally:
            self.recent = [*self.recent, text][-4:]

    def _choice(self, text: str) -> str | None:
        if self.awaiting.is_emergency:
            return parse_emergency_choice(text, self.awaiting.asked)
        return parse_choice(text)

    def _should_ask_again(self, text: str) -> bool:
        if is_asking_back(text):
            return True
        # 응급에서는 "네"처럼 짧지만 어느 쪽인지 모르는 대답도 한 번 더 묻는다
        return self.awaiting.is_emergency and len(text.strip()) <= _SHORT_ANSWER

    def _detect(self, text: str, message_id: str | None, choice: str | None,
                skip=frozenset()) -> tuple[str | None, str | None]:
        """(종류, 들려줄 말). 종류: alert(새로 알림) / quiet(알리지 않음) / reminder / None(위험 아님)."""
        detection = None
        for detector in self.detectors:
            if getattr(detector, "type", None) in skip:
                continue
            found = detector.detect(text, message_id=message_id, recent=tuple(self.recent))
            if found is None:
                continue
            if found.should_alert:
                detection = found
                break  # 알려야 할 것을 먼저 찾은 쪽 (응급 먼저)
            # "어제 넘어졌었는데, 아들이라며 돈 보내래"처럼 지난 응급 뒤에 지금의 사기가 있을 수 있어서 계속 본다
            detection = detection or found
        if detection is None:
            return None, None
        event = detection.log_record()
        self.events.append(event)
        emergency = detection.type == EMERGENCY
        if not detection.should_alert:
            return "quiet", speech.emergency_past() if emergency else speech.refused_well()
        if self._already_alerted(detection):
            event["alert"] = "skipped_same_conversation"
            if not emergency:
                return "reminder", speech.reminder()
            if detection.category == "self_harm":
                return "reminder", speech.self_harm_reminder()
            if self.awaiting is not None and self.awaiting.is_emergency:
                self.awaiting.asked = ASKED_119  # 다시 알림은 119만 묻는다
            return "reminder", speech.emergency_reminder()

        if self.awaiting is not None:
            # 더 위급해지거나 다른 위험이 나와서 새로 알림 → 앞 알림의 선택을 마저 보낸다 (pending으로 남지 않게)
            self.respond(choice or NO_RESPONSE)
        body = detection.to_alert_request(
            request_id=self.new_request_id(), user_id=self.user_id, conversation_id=self.conversation_id
        )
        alert = _Alert(detection=detection, post_body=body, event=event)
        self.alerts.append(alert)
        told_guardian = self._post(alert)
        if emergency:
            if detection.category == "self_harm":
                alert.asked = ASKED_GUARDIAN
            else:
                alert.asked = ASKED_BOTH if told_guardian else ASKED_119
            said = speech.emergency_warning(detection, told_guardian=told_guardian)
        else:
            alert.asked = ASKED_CHECK
            said = speech.warning(detection, told_guardian=told_guardian)
        self.awaiting = alert
        self.asked_again = False
        return "alert", said

    def _already_alerted(self, detection: Detection) -> bool:
        """같은 종류·유형으로 같거나 더 위급한 알림을 이미 보냈는지. 다른 위험은 따로 알린다."""
        same = [a for a in self.alerts
                if (a.detection.type, a.detection.category) == (detection.type, detection.category)]
        return any(_RANK[a.detection.severity] >= _RANK[detection.severity] for a in same)

    # --- 어르신의 선택 (말, 화면 버튼, 무응답) ---

    def respond(self, choice: str) -> str | None:
        """지금 기다리는 알림에 어르신의 선택을 기록한다 (명세 4-2). 화면 버튼(#27)도 이것을 부른다."""
        if choice not in CHOICES:
            raise ValueError(choice)
        alert, self.awaiting = self.awaiting, None
        if alert is None:
            return None
        alert.event["user_response"] = choice
        if choice in _NOT_IN_SPEC and choice not in _TO_SPEC:
            log.info("명세 4에 없는 선택이라 B에 보내지 않음: %s", choice)  # 알림은 high/pending으로 남음
        else:
            alert.response_body = {
                "request_id": self.new_request_id(),
                "user_response": _TO_SPEC.get(choice, choice),
                "responded_at": self.clock().isoformat(timespec="seconds"),
            }
            self._patch(alert)
        self.action = CALL_ACTIONS.get(choice, self.action)
        if choice == NO_RESPONSE:
            return None
        if alert.is_emergency:
            return {
                CALL_119: speech.after_119(),
                HELPLINE_109: speech.after_helpline(),
                GUARDIAN_CALL: speech.after_call(),
                GUARDIAN_CHECK: speech.after_call(),
                DISMISSED: speech.after_emergency_dismiss(),
            }[choice]
        return {
            GUARDIAN_CHECK: speech.after_check(),
            GUARDIAN_CALL: speech.after_call(),
            CALL_119: speech.after_119(),
            HELPLINE_109: speech.after_helpline(),
            DISMISSED: speech.after_dismiss(),
        }[choice]

    def expire(self) -> None:
        """정해진 시간 동안 대답이 없을 때 (시간은 화면·음성 연결 때 정함, 명세 4의 30초?).

        응급 + no_response면 B가 severity를 high로 올린다 (명세 4 severity 규칙).
        """
        if self.awaiting is not None:
            self.respond(NO_RESPONSE)

    # --- B 호출 ---

    def _post(self, alert: _Alert) -> bool:
        result = self.api.create(alert.post_body)
        if result.status == ACCEPTED and result.alert_id:
            alert.alert_id = result.alert_id
            alert.event.update(alert="sent", alert_id=result.alert_id, reached_guardian=result.reached_guardian)
            return result.reached_guardian
        alert.event["alert"] = result.status
        if result.status in PERMANENT_FAILURES:
            # 사용자 없음·형식 거절은 다시 보내도 같다. 원문(evidence.text)을 B에 되풀이해 보내지 않게 그만둔다
            alert.post_gave_up = True
            log.warning("긴급 알림 보내기 실패 (%s), 다시 보내지 않음", result.status)
        else:
            log.warning("긴급 알림 보내기 실패 (%s), 나중에 같은 request_id로 다시 보냄", result.status)
        return False

    def _patch(self, alert: _Alert):
        if alert.alert_id is None:
            return  # 4-1이 아직 안 갔음 → flush()에서 4-1 뒤에 보냄 (4-1을 그만뒀으면 보내지 않음)
        result = self.api.respond(alert.alert_id, alert.response_body)
        if result.status in (ACCEPTED, ALREADY_RESPONDED):
            alert.response_sent = True
        elif result.status in PERMANENT_FAILURES:
            alert.response_sent = True  # 다시 보내도 같음
            log.warning("어르신 선택 보내기 실패 (%s), 다시 보내지 않음", result.status)
        else:
            log.warning("어르신 선택 보내기 실패 (%s), 나중에 같은 request_id로 다시 보냄", result.status)

    def flush(self) -> int:
        """보내지 못한 요청을 같은 request_id로 다시 보낸다. 아직 못 보낸 알림 수를 돌려준다."""
        for alert in self.alerts:
            if alert.done:
                continue
            if alert.alert_id is None:
                self._post(alert)
            if alert.alert_id is not None and alert.response_body is not None and not alert.response_sent:
                self._patch(alert)
        return sum(not a.done for a in self.alerts)
