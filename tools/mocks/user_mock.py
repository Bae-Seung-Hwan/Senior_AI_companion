"""사용자·공유 범위 조회(명세 7, #42) + 대화 사용량(명세 6, #58) mock

A가 B의 실제 서버를 기다리지 않고 대화 호칭·공유 범위 안내(7)와 무료 등급 대화 제한(6)을 개발할 수 있게 하는
가짜 서버. docs/api-contracts.md 6·7번 (PR #67 초안) 형식을 따른다. 6번의 한도는 7번의 등급(plan)으로 정해지므로
한 서버에 같이 둔다. 데이터는 메모리에만 저장하고, 끄면 사라진다. 표준 라이브러리만 사용한다.

사용법:
    python tools/mocks/user_mock.py                   # http://127.0.0.1:8042
    python tools/mocks/user_mock.py --free-limit 2    # 무료 한도를 2회로 (한도 초과 흐름을 빨리 볼 때)
    python tools/mocks/user_mock.py --host 0.0.0.0    # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    GET   /users/{user_id}               7. 사용자·공유 범위 조회
    GET   /usage/{user_id}/today         6-1. 대화 시작 전 확인
    POST  /usage/conversations           6-2. 대화 1회 기록
    PATCH /mock/users/{user_id}          사용자 정보 바꾸기 (mock 전용: plan, display_name, area_name, sharing)
    POST  /mock/usage/{user_id}/reset    오늘 횟수 비우기 (mock 전용)
    GET   /health                        살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도·"B 장애 시 대화 허용" 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 6-1 시간 초과(2초) 처리 확인

테스트용 user_id (mock 전용):
    u_123           어르신, 무료, 호칭 "김영자 님", 지역 "서울 강남구", 보호자 u_789(딸), 일정·기분 공유 (명세 예시와 같음)
    u_456           어르신, premium (무제한), 지역 "부산 해운대구"
    u_no_guardian   어르신, 무료, 보호자 없음
    u_789           보호자 계정 (sharing은 null, preferences는 보호자 자신의 방해 금지 시간)
    u_unknown       404 USER_NOT_FOUND
    그 밖의 사용자    어르신, 무료, 호칭·지역 없음, 보호자 u_789, 공유 안 함

Windows에는 시간대 데이터(tzdata)가 없을 수 있어서, 그때 Asia/Seoul은 고정 +09:00으로 대신 쓴다 (tz() 참고).
"""

import copy
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _common import (
    KST,
    MockHandler,
    invalid,
    parse_datetime,
    require_text,
    require_user,
    require_uuid,
    run,
)

PLANS = {"free", "basic", "premium"}
SECTIONS = ("schedule", "health", "mood")
PATCHABLE = {"plan", "display_name", "area_name", "sharing"}
DEFAULT_FREE_LIMIT = 10  # 임시값 (#67 회의에서 정할 것)
GUARDIAN_ID = "u_789"
DEFAULT_TIMEZONE = "Asia/Seoul"
SEED_DECIDED_AT = "2026-11-20T19:05:00+09:00"
DEFAULT_TTS_SPEED = 1.0  # DB user_preferences.tts_speed 기본값
GUARDIAN_QUIET_HOURS = {"start": "22:00", "end": "07:00"}  # 보호자 기본값 (임시, 회의에서 정할 것)


def tz(name):
    """시간대. Windows에 tzdata가 없으면 ZoneInfo가 실패하므로 Asia/Seoul은 고정 KST로 대신 쓴다.

    한국은 서머타임이 없어서 고정 +09:00이어도 결과가 같다. 다른 시간대는 그대로 오류를 낸다.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == DEFAULT_TIMEZONE:
            return KST
        raise


def _sharing(granted=(), decided_at=SEED_DECIDED_AT):
    return {n: {"granted": n in granted, "decided_at": decided_at if n in granted else None} for n in SECTIONS}


def _senior(user_id, **over):
    user = {
        "user_id": user_id,
        "role": "senior",
        "display_name": None,
        "timezone": DEFAULT_TIMEZONE,
        "area_name": None,  # 날씨용 시·구 (예: "서울 강남구"). 행정동은 넣지 않음. null이면 지역 미설정
        "plan": "free",
        "guardians": [{"guardian_id": GUARDIAN_ID, "relation": "딸", "linked_at": "2026-11-20T19:02:00+09:00"}],
        "sharing": _sharing(),
        "preferences": {
            "time_hints": {"morning": "08:00", "noon": "12:00", "evening": "18:00", "bedtime": "21:00"},
            "quiet_hours": {"start": "22:00", "end": "07:00"},
            "report_send_time": "21:00",
            "tts_speed": DEFAULT_TTS_SPEED,
        },
        "updated_at": "2026-11-20T19:05:00+09:00",
    }
    user.update(over)
    return user


def seed_users():
    return {
        "u_123": _senior("u_123", display_name="김영자 님", area_name="서울 강남구",
                         sharing={**_sharing(("schedule",)),
                                  "health": {"granted": False, "decided_at": SEED_DECIDED_AT},
                                  "mood": {"granted": True, "decided_at": "2026-11-21T09:30:00+09:00"}},
                         updated_at="2026-11-21T09:30:00+09:00"),
        "u_456": _senior("u_456", display_name="박정호 님", area_name="부산 해운대구", plan="premium"),
        "u_no_guardian": _senior("u_no_guardian", guardians=[]),
        GUARDIAN_ID: {
            "user_id": GUARDIAN_ID, "role": "guardian", "display_name": None, "timezone": DEFAULT_TIMEZONE,
            "area_name": None, "plan": "free", "guardians": [], "sharing": None,
            # 보호자 자신의 설정. 어르신 quiet_hours(할 일 알림)와 달리 하루 요약·medium 알림을 아침까지 미루는 기준
            "preferences": {"quiet_hours": dict(GUARDIAN_QUIET_HOURS)},
            "updated_at": "2026-11-20T19:02:00+09:00",
        },
    }


class Store:
    def __init__(self, clock=lambda: datetime.now(KST), free_limit=DEFAULT_FREE_LIMIT):
        self.clock = clock
        self.free_limit = free_limit
        self.lock = threading.Lock()
        self.users = seed_users()
        self.conversations = {}  # request_id → {"user_id", "date", "response"}

    # --- 7. 사용자 ---

    def _user(self, user_id):
        require_user(user_id)
        if user_id not in self.users:
            self.users[user_id] = _senior(user_id)
        return self.users[user_id]

    def get_user(self, user_id):
        with self.lock:
            return 200, copy.deepcopy(self._user(user_id))

    def patch_user(self, user_id, body):
        """mock 전용: plan, display_name, area_name, sharing({schedule: true, …})만 바꾼다."""
        with self.lock:
            user = self._user(user_id)
            if not isinstance(body, dict) or not body or set(body) - PATCHABLE:
                raise invalid("plan / display_name / area_name / sharing 만 바꿀 수 있습니다")
            if "plan" in body and body["plan"] not in PLANS:
                raise invalid("plan은 free / basic / premium 중 하나여야 합니다")
            for field in ("display_name", "area_name"):
                if field in body and body[field] is not None:
                    require_text(body[field], field)
            sharing = body.get("sharing")
            if sharing is not None:
                if user["role"] != "senior":
                    raise invalid("보호자 계정에는 공유 범위가 없습니다")
                if not isinstance(sharing, dict) or any(k not in SECTIONS or not isinstance(v, bool)
                                                        for k, v in sharing.items()):
                    raise invalid("sharing은 schedule / health / mood 를 true·false로 보내세요")
            now = self.clock().isoformat()
            if "plan" in body:
                user["plan"] = body["plan"]
            for field in ("display_name", "area_name"):
                if field in body:
                    user[field] = body[field]
            for name, granted in (sharing or {}).items():
                user["sharing"][name] = {"granted": granted, "decided_at": now}
            user["updated_at"] = now
            return 200, copy.deepcopy(user)

    # --- 6. 사용량 ---

    def _limit(self, user):
        return self.free_limit if user["plan"] == "free" else None  # basic·premium은 무제한

    def _usage(self, user, day):
        used = sum(1 for c in self.conversations.values() if c["user_id"] == user["user_id"] and c["date"] == day)
        limit = self._limit(user)
        resets_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz(user["timezone"]))
        return {
            "user_id": user["user_id"],
            "date": day.isoformat(),
            "limit": limit,
            "used": used,
            "remaining": None if limit is None else max(limit - used, 0),
            "allowed": limit is None or used < limit,
            "resets_at": resets_at.isoformat(),
        }

    def today(self, user_id):
        with self.lock:
            user = self._user(user_id)
            day = self.clock().astimezone(tz(user["timezone"])).date()
            return 200, self._usage(user, day)

    def record(self, body):
        """6-2. 같은 request_id면 횟수를 늘리지 않고 처음 결과를 돌려준다. 한도를 넘어도 거절하지 않는다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.conversations:
                return 200, copy.deepcopy(self.conversations[rid]["response"])

            if not isinstance(body, dict):
                raise invalid("요청 본문은 JSON 객체여야 합니다")
            require_uuid(rid)
            user = self._user(body.get("user_id"))
            require_text(body.get("conversation_id"), "conversation_id")
            started_at = parse_datetime(body.get("started_at"), "started_at")

            day = started_at.astimezone(tz(user["timezone"])).date()  # 늦게 기록해도 대화한 날짜로
            self.conversations[rid] = {"user_id": user["user_id"], "date": day,
                                       "conversation_id": body["conversation_id"]}
            resp = self._usage(user, day)
            self.conversations[rid]["response"] = resp
            return 201, copy.deepcopy(resp)

    def reset(self, user_id):
        """mock 전용: 오늘 기록을 지운다."""
        with self.lock:
            user = self._user(user_id)
            day = self.clock().astimezone(tz(user["timezone"])).date()
            self.conversations = {k: v for k, v in self.conversations.items()
                                  if not (v["user_id"] == user_id and v["date"] == day)}
            return 200, self._usage(user, day)


def make_handler(store):
    class Handler(MockHandler):
        log_name = "user-mock"
        allowed_methods = ("GET", "POST", "PATCH")

        def route(self, method, path, query, body):
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 2 and parts[0] == "users":
                return store.get_user(parts[1])
            if method == "GET" and len(parts) == 3 and parts[0] == "usage" and parts[2] == "today":
                return store.today(parts[1])
            if method == "POST" and path == "/usage/conversations":
                return store.record(body)
            if method == "PATCH" and len(parts) == 3 and parts[:2] == ["mock", "users"]:
                return store.patch_user(parts[2], body)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["mock", "usage"] and parts[3] == "reset":
                return store.reset(parts[2])
            return super().route(method, path, query, body)

    return Handler


def add_arguments(ap):
    ap.add_argument("--free-limit", type=int, default=DEFAULT_FREE_LIMIT,
                    help=f"무료 등급 하루 대화 한도 (기본 {DEFAULT_FREE_LIMIT}, 임시값)")


def main():
    run(None, "사용자·공유 범위(명세 7) + 대화 사용량(명세 6) mock", 8042, "사용자·사용량 mock",
        add_arguments=add_arguments,
        handler_factory=lambda args: make_handler(Store(free_limit=args.free_limit)))


if __name__ == "__main__":
    main()
