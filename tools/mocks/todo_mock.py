"""할 일 등록 API mock (명세 1, #45)

A가 B의 실제 서버를 기다리지 않고 "말로 할 일 등록" 흐름을 개발할 수 있게 하는 가짜 서버.
docs/api-contracts.md 1번 (PR #37 초안) 형식을 따른다. 데이터는 메모리에만 저장하고, 끄면 사라진다.
표준 라이브러리만 사용하므로 설치할 것이 없다.

사용법:
    python tools/mocks/todo_mock.py                 # http://127.0.0.1:8045
    python tools/mocks/todo_mock.py --port 9000
    python tools/mocks/todo_mock.py --host 0.0.0.0  # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    POST /todos             할 일 등록 (명세 1)
    GET  /todos?user_id=…   등록된 할 일 보기 (mock 전용, 확인용)
    GET  /health            살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도 정책 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 시간 초과 처리 확인
"""

import argparse
import json
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

KINDS = {"appointment", "medication", "task"}
TIME_HINTS = {"morning", "noon", "evening", "bedtime"}
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# time_hint → 기본 시각. 실제 값은 회의에서 정하고, 나중에는 사용자별로 바꿀 수 있게 한다 (#39, #43)
DEFAULT_HINT_TIMES = {"morning": "08:00", "noon": "12:00", "evening": "18:00", "bedtime": "21:00"}

# 이 user_id로 보내면 404 (사용자 없음) 응답
UNKNOWN_USER_ID = "u_unknown"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def bad(code, message):
    return ApiError(400, code, message)


def parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise bad("INVALID_REQUEST", f"{field}는 YYYY-MM-DD 형식이어야 합니다") from None


def parse_hhmm(value, field):
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
        if len(hh) == 2 and len(mm) == 2 and 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (AttributeError, ValueError):
        pass
    raise bad("INVALID_REQUEST", f"{field}는 HH:MM 형식이어야 합니다")


def validate(body, now):
    """요청을 검사하고 (date, (시, 분) 또는 None, repeat) 을 돌려준다."""
    if not isinstance(body, dict):
        raise bad("INVALID_REQUEST", "요청 본문은 JSON 객체여야 합니다")

    try:
        uuid.UUID(str(body.get("request_id")))
    except ValueError:
        raise bad("INVALID_REQUEST", "request_id는 UUID여야 합니다") from None

    if not isinstance(body.get("user_id"), str) or not body["user_id"]:
        raise bad("INVALID_REQUEST", "user_id가 필요합니다")
    if body["user_id"] == UNKNOWN_USER_ID:
        raise ApiError(404, "USER_NOT_FOUND", "사용자를 찾을 수 없습니다")

    if body.get("kind") not in KINDS:
        raise bad("INVALID_REQUEST", "kind는 appointment / medication / task 중 하나여야 합니다")

    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise bad("MISSING_TITLE", "title이 필요합니다")

    if not isinstance(body.get("original_text"), str) or not body["original_text"]:
        raise bad("INVALID_REQUEST", "original_text가 필요합니다")
    if not isinstance(body.get("source"), dict):
        raise bad("INVALID_REQUEST", "source가 필요합니다")

    repeat = body.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, dict) or repeat.get("freq") not in {"daily", "weekly"}:
            raise bad("INVALID_REQUEST", 'repeat.freq는 "daily" 또는 "weekly"여야 합니다')
        if repeat["freq"] == "weekly":
            days = repeat.get("weekdays")
            if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
                raise bad("INVALID_REQUEST", "weekly 반복에는 weekdays(MON~SUN)가 필요합니다")

    the_date = None
    if body.get("date") is not None:
        the_date = parse_date(body["date"], "date")
    elif repeat is None:
        raise bad("MISSING_SCHEDULE", "반복이 아니면 date가 필요합니다")

    hint = body.get("time_hint")
    if hint is not None and hint not in TIME_HINTS:
        raise bad("INVALID_REQUEST", "time_hint는 morning / noon / evening / bedtime 중 하나여야 합니다")

    hm = None
    if body.get("time") is not None:
        hm = parse_hhmm(body["time"], "time")
    elif hint is not None:
        hm = parse_hhmm(DEFAULT_HINT_TIMES[hint], "time_hint")

    today = now.date()
    if the_date is not None and the_date < today:
        raise bad("INVALID_DATE", "date는 오늘 이후여야 합니다")
    if repeat is None and hm is not None:
        at = datetime(the_date.year, the_date.month, the_date.day, *hm, tzinfo=KST)
        if at < now:
            raise bad("INVALID_DATE", "이미 지난 시각입니다")

    return the_date, hm, repeat


def next_at(the_date, hm, repeat, now):
    """다음 알림 시각. 시각을 모르면 None (알림 없음)."""
    if hm is None:
        return None
    start = the_date or now.date()
    if repeat is None:
        return datetime(start.year, start.month, start.day, *hm, tzinfo=KST)
    days = set(repeat.get("weekdays") or WEEKDAYS)  # daily = 모든 요일
    for offset in range(8):
        d = max(start, now.date()) + timedelta(days=offset)
        at = datetime(d.year, d.month, d.day, *hm, tzinfo=KST)
        if WEEKDAYS[d.weekday()] in days and at >= now:
            return at
    return None  # 도달하지 않음


class Store:
    """메모리 저장소. 같은 request_id는 처음 응답을 그대로 돌려준다."""

    def __init__(self, clock=lambda: datetime.now(KST)):
        self.clock = clock
        self.lock = threading.Lock()
        self.by_request_id = {}
        self.todos = []

    def create(self, body):
        """(HTTP 상태, 응답 본문) 을 돌려준다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.by_request_id:
                return 200, self.by_request_id[rid]

            now = self.clock()
            the_date, hm, repeat = validate(body, now)
            at = next_at(the_date, hm, repeat, now)
            resp = {
                "todo_id": f"t_{len(self.todos) + 1:03d}",
                "title": body["title"].strip(),
                "next_at": at.isoformat() if at else None,
                "reminder": at is not None,
            }
            self.by_request_id[rid] = resp
            self.todos.append({**resp, "user_id": body["user_id"], "kind": body["kind"], "request": body})
            return 201, resp

    def list(self, user_id=None):
        with self.lock:
            return [t for t in self.todos if user_id in (None, t["user_id"])]


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
            if url.path == "/todos":
                user_id = parse_qs(url.query).get("user_id", [None])[0]
                return self._send(200, {"todos": store.list(user_id)})
            self._error(ApiError(404, "NOT_FOUND", "없는 경로입니다"))

        def do_POST(self):
            if urlparse(self.path).path != "/todos":
                return self._error(ApiError(404, "NOT_FOUND", "없는 경로입니다"))
            if self._mock_headers():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._error(bad("INVALID_REQUEST", "JSON 형식이 아닙니다"))
            try:
                status, resp = store.create(body)
            except ApiError as err:
                return self._error(err)
            self._send(status, resp)

        def log_message(self, fmt, *args):
            # 대화 원문(original_text)은 로그에 남기지 않는다 (민감정보 규칙). 경로와 상태만 출력
            print(f"[todo-mock] {self.command} {self.path} → {args[1] if len(args) > 1 else ''}")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="할 일 등록 API mock (명세 1)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8045)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Store()))
    print(f"할 일 mock 실행 중: http://{args.host}:{args.port}  (끄려면 Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
