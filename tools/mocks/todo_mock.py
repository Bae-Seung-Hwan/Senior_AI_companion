"""할 일 등록 API mock (명세 1, #45)

A가 B의 실제 서버를 기다리지 않고 "말로 할 일 등록" 흐름을 개발할 수 있게 하는 가짜 서버.
docs/api-contracts.md 1번 형식을 따른다. 데이터는 메모리에만 저장하고, 끄면 사라진다.
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

import re
import threading
from datetime import date, datetime, timedelta

from _common import KST, ApiError, MockHandler, invalid, require_user, require_uuid, run

KINDS = {"appointment", "medication", "task"}
TIME_HINTS = {"morning", "noon", "evening", "bedtime"}
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# time_hint → 기본 시각. 실제 값은 회의에서 정하고, 나중에는 사용자별로 바꿀 수 있게 한다 (#39, #43)
DEFAULT_HINT_TIMES = {"morning": "08:00", "noon": "12:00", "evening": "18:00", "bedtime": "21:00"}


def bad(code, message):
    return ApiError(400, code, message)


def parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise invalid(f"{field}는 YYYY-MM-DD 형식이어야 합니다") from None


def parse_hhmm(value, field):
    # int()는 "+5:30", " 5:30"도 받으므로 숫자 두 자리씩인지 먼저 거른다
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value, re.ASCII):
        raise bad("INVALID_REQUEST", f"{field}는 HH:MM 형식이어야 합니다")
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
        if len(hh) == 2 and len(mm) == 2 and 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (AttributeError, ValueError):
        pass
    raise invalid(f"{field}는 HH:MM 형식이어야 합니다")


def validate(body, now):
    """요청을 검사하고 (date, (시, 분) 또는 None, repeat) 을 돌려준다."""
    if not isinstance(body, dict):
        raise invalid("요청 본문은 JSON 객체여야 합니다")

    require_uuid(body.get("request_id"))
    require_user(body.get("user_id"))

    if body.get("kind") not in KINDS:
        raise invalid("kind는 appointment / medication / task 중 하나여야 합니다")

    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise bad("MISSING_TITLE", "title이 필요합니다")

    if not isinstance(body.get("original_text"), str) or not body["original_text"]:
        raise invalid("original_text가 필요합니다")
    if not isinstance(body.get("source"), dict):
        raise invalid("source가 필요합니다")

    repeat = body.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, dict) or repeat.get("freq") not in {"daily", "weekly"}:
            raise invalid('repeat.freq는 "daily" 또는 "weekly"여야 합니다')
        if repeat["freq"] == "weekly":
            days = repeat.get("weekdays")
            if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
                raise invalid("weekly 반복에는 weekdays(MON~SUN)가 필요합니다")

    the_date = None
    if body.get("date") is not None:
        the_date = parse_date(body["date"], "date")
    elif repeat is None:
        raise bad("MISSING_SCHEDULE", "반복이 아니면 date가 필요합니다")

    hint = body.get("time_hint")
    if hint is not None and hint not in TIME_HINTS:
        raise invalid("time_hint는 morning / noon / evening / bedtime 중 하나여야 합니다")

    if body.get("time") is not None and hint is not None:
        raise invalid("time과 time_hint는 둘 중 하나만 보내야 합니다")

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


def duplicate_key(body):
    """같은 사용자·날짜·시각·제목이면 중복 (409 DUPLICATE_TODO).
    time이 없으면 time_hint를 비교한다 (명세에 없는 임시 규칙, README 참고)."""
    return body["user_id"], body.get("date"), body.get("time"), body.get("time_hint"), body["title"].strip()


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
            title = body["title"].strip()
            for t in self.todos:
                if duplicate_key(t["request"]) == duplicate_key(body):
                    raise ApiError(409, "DUPLICATE_TODO", "같은 할 일이 이미 등록되어 있습니다",
                                   existing_todo_id=t["todo_id"])
            at = next_at(the_date, hm, repeat, now)
            resp = {
                "todo_id": f"t_{len(self.todos) + 1:03d}",
                "title": title,
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
    class Handler(MockHandler):
        log_name = "todo-mock"

        def route(self, method, path, query, body):
            if path != "/todos":
                return super().route(method, path, query, body)
            if method == "POST":
                return store.create(body)
            if method == "GET":
                user_id = query.get("user_id", [None])[0]
                return 200, {"todos": store.list(user_id)}
            return super().route(method, path, query, body)

    return Handler


def main():
    run(make_handler(Store()), "할 일 등록 API mock (명세 1)", 8045, "할 일 mock")


if __name__ == "__main__":
    main()
