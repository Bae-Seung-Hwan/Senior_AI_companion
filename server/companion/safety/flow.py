"""사기 감지 대화 흐름 (#25, #28).

대화 하나에 SafetyConversation 하나. 어르신이 말할 때마다 할 일 흐름보다 **먼저** handle()을 부른다.
None이면 위험한 말이 아니므로 다음 처리(할 일, 일반 대화)로 넘어간다.

1. 사기가 의심되면 **곧바로** B에 `POST /alerts`(user_response: pending)를 보내고 경고를 들려 드린다 (명세 4-1)
2. 어르신의 대답("가족에게 확인할게", "괜찮아")을 `PATCH /alerts/{id}`로 보낸다 (명세 4-2).
   화면 버튼(#27)은 respond()를, 정해진 시간 동안 대답이 없으면 expire()를 부른다 (no_response)
3. 대답을 기다리는 동안에도 **감지는 계속**한다. "알았어, 그래도 지금 보낼게"처럼 더 위급해지면 새로 알린다
4. B가 응답하지 않아도 경고는 그대로 들려 드리고, 보내지 못한 요청은 같은 request_id로 flush()에서 다시 보낸다

한 대화에서 **같은 유형의 사기**로 경고를 되풀이하지 않는다. 더 위급해졌을 때(medium → high)만 새로 알린다.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from ..clock import KST
from ..todos.parse import NO, YES, is_asking_back, parse_yes_no
from . import speech
from .b_client import ACCEPTED, ALREADY_RESPONDED, PERMANENT_FAILURES, AlertApi
from .models import HIGH, Detection

log = logging.getLogger(__name__)

GUARDIAN_CHECK = "guardian_check_requested"
GUARDIAN_CALL = "guardian_call_requested"
DISMISSED = "dismissed"
NO_RESPONSE = "no_response"
CHOICES = (GUARDIAN_CHECK, GUARDIAN_CALL, DISMISSED, NO_RESPONSE)

_CALL = ("전화해 줘", "전화해줘", "전화 걸어", "전화 좀 걸어", "연결해", "전화 좀 해 줘", "전화 좀 해줘")
_DISMISS = ("괜찮아", "괜찮습니다", "사기 아니", "사기가 아니", "진짜 아들", "진짜 딸", "진짜 손주", "진짜야",
            "내가 알아서", "필요 없", "필요없", "아니야", "아니에요", "아냐")
_CHECK = ("확인할게", "확인해 볼게", "확인해볼게", "확인해 봐야", "물어볼게", "물어봐야", "물어 볼게", "전화해 볼게",
          "전화해볼게", "연락해 볼게", "연락해볼게", "알았어", "알겠어", "알겠습니다", "그럴게", "그래야겠")

_RANK = {"medium": 1, HIGH: 2}
_SHORT_ANSWER = 10  # 글자 수


def parse_choice(text: str) -> str | None:
    """경고 뒤 어르신의 대답. 못 알아들으면 None ("네?"처럼 되묻는 말 포함)."""
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


@dataclass
class _Alert:
    detection: Detection
    post_body: dict
    alert_id: str | None = None
    post_gave_up: bool = False      # 다시 보내도 결과가 같은 4xx라 더 보내지 않음
    response_body: dict | None = None
    response_sent: bool = False     # 보냈거나, 다시 보내도 소용없어서 그만둠
    event: dict = field(default_factory=dict)

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
        detector,
        api: AlertApi,
        clock=lambda: dt.datetime.now(KST),
        new_request_id=lambda: str(uuid.uuid4()),
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.detector = detector
        self.api = api
        self.clock = clock
        self.new_request_id = new_request_id
        self.alerts: list[_Alert] = []
        self.awaiting: _Alert | None = None  # 경고를 들려 드리고 대답을 기다리는 알림
        self.asked_again = False
        self.recent: list[str] = []
        # 대화 기록에 남길 감지 기록 (#25 완료 조건). 발화 원문은 넣지 않는다
        self.events: list[dict] = []

    # --- 어르신의 말 ---

    def handle(self, text: str, message_id: str | None = None) -> str | None:
        try:
            choice = parse_choice(text) if self.awaiting is not None else None
            # 대답을 기다리는 중에도 감지는 한다 ("알았어, 그래도 지금 은행 가서 보낼게", "벌써 인증번호 알려줬어")
            kind, said = self._detect(text, message_id, choice)
            if kind == "alert":
                return said  # 새 경고. 앞 알림에는 이 대답(또는 no_response)을 이미 보냄
            if self.awaiting is not None:
                if choice is not None:
                    return self.respond(choice)
                if is_asking_back(text) and not self.asked_again:
                    self.asked_again = True
                    return speech.ask_choice()
                # 대답이 아닌 말은 붙잡지 않는다 → 할 일·일반 대화로. 화면 버튼이나 expire()를 기다림
            return said
        finally:
            self.recent = [*self.recent, text][-4:]

    def _detect(self, text: str, message_id: str | None, choice: str | None) -> tuple[str | None, str | None]:
        """(종류, 들려줄 말). 종류: alert(새로 알림) / refused / reminder / None(사기 아님)."""
        detection = self.detector.detect(text, message_id=message_id, recent=tuple(self.recent))
        if detection is None:
            return None, None
        event = detection.log_record()
        self.events.append(event)
        if not detection.should_alert:
            return "refused", speech.refused_well()
        if self._already_alerted(detection):
            event["alert"] = "skipped_same_conversation"
            return "reminder", speech.reminder()

        if self.awaiting is not None:
            # 더 위급해져서 새로 알림 → 앞 알림의 선택을 마저 보낸다 (B에 pending으로 남지 않게)
            self.respond(choice or NO_RESPONSE)
        body = detection.to_alert_request(
            request_id=self.new_request_id(), user_id=self.user_id, conversation_id=self.conversation_id
        )
        alert = _Alert(detection=detection, post_body=body, event=event)
        self.alerts.append(alert)
        told_guardian = self._post(alert)
        self.awaiting = alert
        self.asked_again = False
        return "alert", speech.warning(detection, told_guardian=told_guardian)

    def _already_alerted(self, detection: Detection) -> bool:
        """같은 유형으로 같거나 더 위급한 알림을 이미 보냈는지. 다른 유형의 사기는 따로 알린다."""
        same = [a for a in self.alerts if a.detection.category == detection.category]
        return any(_RANK[a.detection.severity] >= _RANK[detection.severity] for a in same)

    # --- 어르신의 선택 (말, 화면 버튼, 무응답) ---

    def respond(self, choice: str) -> str | None:
        """지금 기다리는 알림에 어르신의 선택을 기록한다 (명세 4-2). 화면 버튼(#27)도 이것을 부른다."""
        if choice not in CHOICES:
            raise ValueError(choice)
        alert, self.awaiting = self.awaiting, None
        if alert is None:
            return None
        alert.response_body = {
            "request_id": self.new_request_id(),
            "user_response": choice,
            "responded_at": self.clock().isoformat(timespec="seconds"),
        }
        alert.event["user_response"] = choice
        self._patch(alert)
        return {
            GUARDIAN_CHECK: speech.after_check(),
            GUARDIAN_CALL: speech.after_call(),
            DISMISSED: speech.after_dismiss(),
            NO_RESPONSE: None,
        }[choice]

    def expire(self) -> None:
        """정해진 시간 동안 대답이 없을 때 (시간은 화면·음성 연결 때 정함, 명세 4의 30초?)."""
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
