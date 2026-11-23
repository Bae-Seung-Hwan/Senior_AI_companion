"""긴급 알림 수신 API mock (명세 4, #49)

A가 B의 실제 서버를 기다리지 않고 "사기·응급 감지 → 보호자 알림" 흐름을 개발할 수 있게 하는 가짜 서버.
docs/api-contracts.md 4번 (PR #37 초안) 형식을 따른다. 실제로 푸시·알림톡을 보내지는 않고,
보냈다고 가정한 결과(deliveries)만 돌려준다. 데이터는 메모리에만 저장하고, 끄면 사라진다.
표준 라이브러리만 사용하므로 설치할 것이 없다.

사용법:
    python tools/mocks/alert_mock.py                 # http://127.0.0.1:8049
    python tools/mocks/alert_mock.py --port 9000
    python tools/mocks/alert_mock.py --host 0.0.0.0  # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    POST /alerts            긴급 알림 요청 (명세 4)
    GET  /alerts?user_id=…  받은 알림 보기 (mock 전용, 확인용)
    GET  /health            살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도 정책 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 시간 초과 처리 확인

테스트용 user_id (mock 전용):
    u_unknown               404 USER_NOT_FOUND
    u_no_guardian           보호자가 없음 → deliveries: []
"""

import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

TYPES = {"fraud", "emergency"}
SEVERITIES = {"high", "medium"}
USER_RESPONSES = {
    "call_119_requested",
    "guardian_call_requested",
    "guardian_check_requested",
    "dismissed",
    "no_response",
}

# 이 user_id로 보내면 404 (사용자 없음) 응답
UNKNOWN_USER_ID = "u_unknown"
# 이 user_id로 보내면 보호자가 없는 것으로 보고 deliveries: [] 응답
NO_GUARDIAN_USER_ID = "u_no_guardian"
# 그 밖의 사용자는 보호자 한 명(g_01)이 연결되어 있다고 가정한다
DEFAULT_GUARDIAN_ID = "g_01"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def bad(message):
    return ApiError(400, "INVALID_REQUEST", message)


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise bad(f"{field}가 필요합니다")


def parse_detected_at(value):
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise bad("detected_at은 ISO 8601 시각이어야 합니다") from None
    if at.tzinfo is None:
        raise bad("detected_at에는 시간대가 있어야 합니다 (예: +09:00)")
    return at


def validate(body):
    """요청을 검사한다. 오류면 ApiError를 던진다."""
    if not isinstance(body, dict):
        raise bad("요청 본문은 JSON 객체여야 합니다")

    try:
        uuid.UUID(str(body.get("request_id")))
    except ValueError:
        raise bad("request_id는 UUID여야 합니다") from None

    require_text(body.get("user_id"), "user_id")
    if body["user_id"] == UNKNOWN_USER_ID:
        raise ApiError(404, "USER_NOT_FOUND", "사용자를 찾을 수 없습니다")

    if body.get("type") not in TYPES:
        raise bad("type은 fraud / emergency 중 하나여야 합니다")
    if body.get("severity") not in SEVERITIES:
        raise bad("severity는 high / medium 중 하나여야 합니다")
    require_text(body.get("reason"), "reason")

    evidence = body.get("evidence")
    if not isinstance(evidence, dict):
        raise bad("evidence가 필요합니다")
    require_text(evidence.get("text"), "evidence.text")

    if body.get("user_response") not in USER_RESPONSES:
        raise bad("user_response는 " + " / ".join(sorted(USER_RESPONSES)) + " 중 하나여야 합니다")

    parse_detected_at(body.get("detected_at"))


def effective_severity(body):
    """명세: "괜찮아요"(dismissed)를 골라도 알림은 보내고, severity만 medium으로 낮춘다."""
    return "medium" if body["user_response"] == "dismissed" else body["severity"]


def deliveries_for(user_id, severity):
    """보냈다고 가정한 발송 결과. high는 앱 푸시 + 알림톡, medium은 앱 푸시만."""
    if user_id == NO_GUARDIAN_USER_ID:
        return []
    out = [{"guardian_id": DEFAULT_GUARDIAN_ID, "channel": "push", "status": "sent"}]
    if severity == "high":
        out.append({"guardian_id": DEFAULT_GUARDIAN_ID, "channel": "alimtalk", "status": "queued"})
    return out


class Store:
    """메모리 저장소. 같은 request_id는 처음 응답을 그대로 돌려준다 (중복 발송하지 않음)."""

    def __init__(self, clock=lambda: datetime.now(KST)):
        self.clock = clock
        self.lock = threading.Lock()
        self.by_request_id = {}
        self.alerts = []

    def create(self, body):
        """(HTTP 상태, 응답 본문) 을 돌려준다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.by_request_id:
                return 202, self.by_request_id[rid]

            validate(body)
            severity = effective_severity(body)
            resp = {
                "alert_id": f"al_{len(self.alerts) + 1:03d}",
                "deliveries": deliveries_for(body["user_id"], severity),
            }
            self.by_request_id[rid] = resp
            self.alerts.append({
                **resp,
                "user_id": body["user_id"],
                "type": body["type"],
                "severity": severity,  # dismissed면 낮춘 값
                "requested_severity": body["severity"],
                "user_response": body["user_response"],
                "received_at": self.clock().isoformat(),
                "request": body,
            })
            return 202, resp

    def list(self, user_id=None):
        with self.lock:
            return [a for a in self.alerts if user_id in (None, a["user_id"])]


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, err):
            self._send(err.status, {"error": {"code": err.code, "message": err.message}})

        def _mock_headers(self):
            delay = self.headers.get("X-Mock-Delay")
            if delay:
                time.sleep(min(float(delay), 30))
            fail = self.headers.get("X-Mock-Fail")
            if fail:
                status = int(fail) if fail.isdigit() and 500 <= int(fail) < 600 else 500
                self._error(ApiError(status, "MOCK_FAILURE", "X-Mock-Fail 헤더로 만든 가짜 장애입니다"))
                return True
            return False

        def do_OPTIONS(self):  # 웹에서 호출할 때 CORS preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Mock-Fail, X-Mock-Delay")
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if url.path == "/alerts":
                user_id = parse_qs(url.query).get("user_id", [None])[0]
                return self._send(200, {"alerts": store.list(user_id)})
            self._error(ApiError(404, "NOT_FOUND", "없는 경로입니다"))

        def do_POST(self):
            if urlparse(self.path).path != "/alerts":
                return self._error(ApiError(404, "NOT_FOUND", "없는 경로입니다"))
            if self._mock_headers():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._error(bad("JSON 형식이 아닙니다"))
            try:
                status, resp = store.create(body)
            except ApiError as err:
                return self._error(err)
            self._send(status, resp)

        def log_message(self, fmt, *args):
            # 감지 근거 발화(evidence.text)는 로그에 남기지 않는다 (민감정보 규칙). 경로와 상태만 출력
            print(f"[alert-mock] {self.command} {self.path} → {args[1] if len(args) > 1 else ''}")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="긴급 알림 수신 API mock (명세 4)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8049)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Store()))
    print(f"긴급 알림 mock 실행 중: http://{args.host}:{args.port}  (끄려면 Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
