"""긴급 알림 수신 API mock (명세 4, #49)

A가 B의 실제 서버를 기다리지 않고 "사기·응급 감지 → 보호자 알림" 흐름을 개발할 수 있게 하는 가짜 서버.
docs/api-contracts.md 4번 (PR #37 초안, 0b778ee) 형식을 따른다. 실제로 푸시·알림톡을 보내지는 않고,
보냈다고 가정한 결과(deliveries)만 돌려준다. 데이터는 메모리에만 저장하고, 끄면 사라진다.
표준 라이브러리만 사용하므로 설치할 것이 없다.

사용법:
    python tools/mocks/alert_mock.py                 # http://127.0.0.1:8049
    python tools/mocks/alert_mock.py --port 9000
    python tools/mocks/alert_mock.py --host 0.0.0.0  # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    POST  /alerts               4-1. 감지 즉시 긴급 알림 (보통 user_response: "pending")
    PATCH /alerts/{alert_id}    4-2. 어르신의 선택 전달 → severity 조정, 필요하면 추가 발송
    GET   /alerts?user_id=…     받은 알림 보기 (mock 전용, 확인용)
    GET   /health               살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도 정책 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 시간 초과 처리 확인

테스트용 user_id (mock 전용):
    u_unknown               404 USER_NOT_FOUND
    u_no_guardian           보호자가 없음 → deliveries: []
"""

import copy
import threading
from datetime import datetime

from _common import (
    KST,
    ApiError,
    MockHandler,
    invalid,
    not_found,
    parse_datetime,
    require_text,
    require_user,
    require_uuid,
    run,
)

TYPES = {"fraud", "emergency"}
SEVERITIES = {"high", "medium"}
# pending을 뺀 최종 선택
FINAL_RESPONSES = {
    "call_119_requested",
    "guardian_call_requested",
    "guardian_check_requested",
    "dismissed",
    "no_response",
}
USER_RESPONSES = FINAL_RESPONSES | {"pending"}

# 이 user_id로 보내면 보호자가 없는 것으로 보고 deliveries: [] 응답
NO_GUARDIAN_USER_ID = "u_no_guardian"
# 그 밖의 사용자는 보호자 한 명(u_789)이 연결되어 있다고 가정한다
DEFAULT_GUARDIAN_ID = "u_789"


def adjusted_severity(alert_type, severity, user_response):
    """명세 4 severity 규칙 (B가 적용).

    - 사기 + dismissed → medium으로 낮춤
    - 응급 + dismissed → 그대로 (안전 우선)
    - 응급 + no_response → high로 올림
    - 그 밖에는 그대로
    """
    if alert_type == "fraud" and user_response == "dismissed":
        return "medium"
    if alert_type == "emergency" and user_response == "no_response":
        return "high"
    return severity


def validate_post(body):
    if not isinstance(body, dict):
        raise invalid("요청 본문은 JSON 객체여야 합니다")
    require_uuid(body.get("request_id"))
    require_user(body.get("user_id"))
    if body.get("type") not in TYPES:
        raise invalid("type은 fraud / emergency 중 하나여야 합니다")
    if body.get("severity") not in SEVERITIES:
        raise invalid("severity는 high / medium 중 하나여야 합니다")
    require_text(body.get("reason"), "reason")
    evidence = body.get("evidence")
    if not isinstance(evidence, dict):
        raise invalid("evidence가 필요합니다")
    require_text(evidence.get("text"), "evidence.text")
    if body.get("user_response") not in USER_RESPONSES:
        raise invalid("user_response는 " + " / ".join(sorted(USER_RESPONSES)) + " 중 하나여야 합니다")
    parse_datetime(body.get("detected_at"), "detected_at")


def validate_patch(body):
    if not isinstance(body, dict):
        raise invalid("요청 본문은 JSON 객체여야 합니다")
    require_uuid(body.get("request_id"))
    if body.get("user_response") not in FINAL_RESPONSES:
        raise invalid("user_response는 " + " / ".join(sorted(FINAL_RESPONSES)) + " 중 하나여야 합니다 (pending 불가)")
    parse_datetime(body.get("responded_at"), "responded_at")


class Store:
    """메모리 저장소. 같은 request_id는 처음 응답을 그대로 돌려준다 (중복 발송하지 않음)."""

    def __init__(self, clock=lambda: datetime.now(KST)):
        self.clock = clock
        self.lock = threading.Lock()
        self.post_results = {}   # request_id → 응답
        self.patch_results = {}  # request_id → 응답
        self.alerts = {}         # alert_id → 알림 (등록 순서 유지)

    # --- 발송 (흉내) ---

    def _deliver(self, alert, channel):
        if alert["user_id"] == NO_GUARDIAN_USER_ID:
            return
        status = "sent" if channel == "push" else "queued"
        alert["deliveries"].append({"guardian_id": DEFAULT_GUARDIAN_ID, "channel": channel, "status": status})

    def _has_alimtalk(self, alert):
        return any(d["channel"] == "alimtalk" and d["status"] != "canceled" for d in alert["deliveries"])

    def _apply_severity(self, alert, severity):
        """severity를 바꾸고 발송을 맞춘다. 이미 보낸(sent) 것은 취소하지 않는다."""
        if severity == "high" and not self._has_alimtalk(alert):
            self._deliver(alert, "alimtalk")  # 올릴 때는 알림톡 추가
        if severity == "medium":
            for d in alert["deliveries"]:
                if d["channel"] == "alimtalk" and d["status"] == "queued":
                    d["status"] = "canceled"  # 낮출 때는 아직 안 보낸 알림톡만 취소
        alert["severity"] = severity

    @staticmethod
    def _response(alert):
        return copy.deepcopy({k: alert[k] for k in ("alert_id", "severity", "deliveries")})

    # --- API ---

    def create(self, body):
        """POST /alerts. (HTTP 상태, 응답 본문)을 돌려준다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.post_results:
                return 202, self.post_results[rid]

            validate_post(body)
            alert = {
                "alert_id": f"al_{len(self.alerts) + 1:03d}",
                "user_id": body["user_id"],
                "type": body["type"],
                "requested_severity": body["severity"],
                "severity": body["severity"],
                "user_response": body["user_response"],
                "deliveries": [],
                "received_at": self.clock().isoformat(),
                "responded_at": None,
            }
            # 처음부터 최종 선택을 알고 보냈으면 규칙을 먼저 적용 (이때는 4-2를 보내지 않음)
            if body["user_response"] != "pending":
                alert["responded_at"] = body["detected_at"]
                alert["severity"] = adjusted_severity(alert["type"], alert["severity"], body["user_response"])
            # 바로 발송: 푸시는 항상, high면 알림톡도
            self._deliver(alert, "push")
            if alert["severity"] == "high":
                self._deliver(alert, "alimtalk")

            self.alerts[alert["alert_id"]] = alert
            resp = self._response(alert)
            self.post_results[rid] = resp
            return 202, resp

    def respond(self, alert_id, body):
        """PATCH /alerts/{alert_id}. (HTTP 상태, 응답 본문)을 돌려준다."""
        with self.lock:
            alert = self.alerts.get(alert_id)
            if alert is None:
                raise not_found("없는 alert_id입니다")
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.patch_results:
                return 200, self.patch_results[rid]

            validate_patch(body)
            if alert["user_response"] != "pending":
                raise ApiError(409, "ALREADY_RESPONDED", "이미 최종 선택이 기록된 알림입니다")

            choice = body["user_response"]
            alert["user_response"] = choice
            alert["responded_at"] = body["responded_at"]
            self._apply_severity(alert, adjusted_severity(alert["type"], alert["severity"], choice))
            if choice == "dismissed":
                self._deliver(alert, "push")  # "어르신이 괜찮다고 응답하셨어요" 후속 푸시

            resp = self._response(alert)
            self.patch_results[rid] = resp
            return 200, resp

    def list(self, user_id=None):
        with self.lock:
            return [copy.deepcopy(a) for a in self.alerts.values() if user_id in (None, a["user_id"])]


def make_handler(store):
    class Handler(MockHandler):
        log_name = "alert-mock"
        allowed_methods = ("GET", "POST", "PATCH")

        def route(self, method, path, query, body):
            if path == "/alerts" and method == "POST":
                return store.create(body)
            if path == "/alerts" and method == "GET":
                user_id = query.get("user_id", [None])[0]
                return 200, {"alerts": store.list(user_id)}
            parts = path.strip("/").split("/")
            if method == "PATCH" and len(parts) == 2 and parts[0] == "alerts":
                return store.respond(parts[1], body)
            return super().route(method, path, query, body)

    return Handler


def main():
    run(make_handler(Store()), "긴급 알림 수신 API mock (명세 4)", 8049, "긴급 알림 mock")


if __name__ == "__main__":
    main()
