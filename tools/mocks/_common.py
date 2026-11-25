"""mock 서버 공통 코드 (오류 형식, 응답 보내기, 테스트용 헤더, CORS, 실행).

각 mock은 MockHandler를 상속해서 route()만 채운다. 표준 라이브러리만 사용한다.
"""

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

# 이 user_id로 보내면 404 (사용자 없음) 응답. 모든 mock 공통
UNKNOWN_USER_ID = "u_unknown"

MAX_MOCK_DELAY = 30  # X-Mock-Delay 최대 초


class ApiError(Exception):
    """명세 공통 에러 응답: {"error": {"code": …, "message": …}}"""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.extra = extra  # 에러 본문에 더 넣을 필드 (예: existing_todo_id)


def invalid(message):
    return ApiError(400, "INVALID_REQUEST", message)


def not_found(message="없는 경로입니다"):
    return ApiError(404, "NOT_FOUND", message)


def require_uuid(value, field="request_id"):
    try:
        uuid.UUID(str(value))
    except ValueError:
        raise invalid(f"{field}는 UUID여야 합니다") from None


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise invalid(f"{field}가 필요합니다")


def require_user(user_id):
    require_text(user_id, "user_id")
    if user_id == UNKNOWN_USER_ID:
        raise ApiError(404, "USER_NOT_FOUND", "사용자를 찾을 수 없습니다")


def parse_datetime(value, field):
    """ISO 8601 시각. 공통 규칙에 따라 시간대가 꼭 있어야 한다."""
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise invalid(f"{field}은 ISO 8601 시각이어야 합니다") from None
    if at.tzinfo is None:
        raise invalid(f"{field}에는 시간대가 있어야 합니다 (예: +09:00)")
    return at


class MockHandler(BaseHTTPRequestHandler):
    """공통 HTTP 처리. 하위 클래스는 log_name, allowed_methods, route()를 정한다.

    route(method, path, query, body)는 (HTTP 상태, 응답 본문)을 돌려주거나 ApiError를 던진다.
    GET이면 body는 None이다.
    """

    log_name = "mock"
    allowed_methods = ("GET", "POST")

    def route(self, method, path, query, body):
        raise not_found()

    # --- 응답 ---

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message, **getattr(err, "extra", {})}})

    # --- 테스트용 헤더 (mock 전용, 명세에는 없음) ---

    def _mock_headers(self):
        """X-Mock-Delay로 늦추고, X-Mock-Fail이면 5xx를 보낸 뒤 True를 돌려준다."""
        try:
            delay = float(self.headers.get("X-Mock-Delay") or 0)
        except ValueError:
            delay = 0  # 숫자가 아니면 무시 (예외로 연결이 끊기지 않게)
        if delay > 0:
            time.sleep(min(delay, MAX_MOCK_DELAY))
        fail = self.headers.get("X-Mock-Fail")
        if fail:
            status = int(fail) if fail.isdigit() and 500 <= int(fail) < 600 else 500
            self._error(ApiError(status, "MOCK_FAILURE", "X-Mock-Fail 헤더로 만든 가짜 장애입니다"))
            return True
        return False

    # --- 요청 처리 ---

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            raise invalid("JSON 형식이 아닙니다") from None

    def _handle(self, method):
        url = urlparse(self.path)
        try:
            if method == "GET" and url.path == "/health":
                return self._send(200, {"ok": True})
            if self._mock_headers():  # GET도 (명세 6-1 실패·시간 초과 확인)
                return
            body = self._read_json() if method != "GET" else None
            status, resp = self.route(method, url.path, parse_qs(url.query), body)
        except ApiError as err:
            return self._error(err)
        self._send(status, resp)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):  # 웹에서 호출할 때 CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", ", ".join((*self.allowed_methods, "OPTIONS")))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Mock-Fail, X-Mock-Delay")
        self.end_headers()

    def log_message(self, fmt, *args):
        # 대화 원문(original_text, evidence.text)은 로그에 남기지 않는다 (민감정보 규칙). 경로와 상태만 출력
        print(f"[{self.log_name}] {self.command} {self.path} → {args[1] if len(args) > 1 else ''}")


def run(handler, description, default_port, label, *, add_arguments=None, handler_factory=None):
    """mock 서버 실행.

    handler: Handler 클래스. 옵션이 필요 없는 mock은 이것만 준다.
    add_arguments(ap): --host·--port 말고 옵션을 더 붙이는 함수 (선택)
    handler_factory(args): 옵션 값으로 Handler 클래스를 만드는 함수. 이걸 쓰면 handler는 None으로 준다.
    """
    if (handler is None) == (handler_factory is None):
        raise TypeError("handler(클래스)와 handler_factory(args → 클래스) 중 하나만 주세요")
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port)
    if add_arguments:
        add_arguments(ap)
    args = ap.parse_args()
    handler_cls = handler_factory(args) if handler_factory else handler
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"{label} 실행 중: http://{args.host}:{args.port}  (끄려면 Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
