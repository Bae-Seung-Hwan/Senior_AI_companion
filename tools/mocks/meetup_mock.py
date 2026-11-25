"""취미·관심사 반영(명세 2, #53) + 모임 목록 조회(명세 5, #57) mock

A가 B의 실제 서버를 기다리지 않고 "대화에서 관심사 추출 → 저장"(#23)과 "모임 추천"(#33)을 개발할 수 있게 하는
가짜 서버. docs/api-contracts.md 2·5번 (PR #70 초안) 형식을 따른다. 5번 추천은 2번으로 저장한 관심사를 쓰므로
한 서버에 같이 둔다. 데이터는 메모리에만 저장하고, 끄면 사라진다. 표준 라이브러리만 사용한다.

사용법:
    python tools/mocks/meetup_mock.py                 # http://127.0.0.1:8057
    python tools/mocks/meetup_mock.py --host 0.0.0.0  # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    POST /interests                               2. 관심사 반영
    GET  /meetups/recommendations?user_id=…       5. 모임 목록 조회 (limit, hobby 선택)
    GET  /mock/interests/{user_id}                관심사 보기, 지운 것 포함 (mock 전용)
    POST /mock/interests/{user_id}/remove         어르신이 화면에서 관심사를 지운 것처럼 (mock 전용)
    GET  /mock/meetups                            모임 데이터 전체와 추천에서 빠지는 이유 (mock 전용)
    GET  /health                                  살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도·"모임 없이 활동만 추천" 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 5번 시간 초과(2초) 처리 확인

테스트용 user_id (mock 전용):
    u_123           역삼1동. 관심사 서예(직접 추가), 노래교실은 최근에 지움 (명세 2 예시와 같음). u_666을 차단함
    u_no_region     동네를 아직 안 정함 → empty_reason "no_region"
    u_unknown       404 USER_NOT_FOUND
    그 밖의 사용자    역삼1동, 관심사 없음 → 관심사를 보내기 전에는 empty_reason "no_interest"
"""

import copy
import threading
from datetime import datetime, time, timedelta

from _common import (
    KST,
    ApiError,
    MockHandler,
    invalid,
    require_text,
    require_user,
    require_uuid,
    run,
)

MAX_INTERESTS_PER_REQUEST = 5   # 임시 (명세 2)
MAX_NAME_LENGTH = 20
RECENTLY_REMOVED_DAYS = 90      # 임시 (명세 2)
DEFAULT_LIMIT, MAX_LIMIT = 3, 10
WINDOW_START = timedelta(hours=1)   # 임시 (명세 5): 1시간 뒤 ~ 14일 안
WINDOW_END = timedelta(days=14)

REGIONS = {"1168064000": "서울 강남구 역삼1동", "1168065000": "서울 강남구 역삼2동"}
HOME_REGION = "1168064000"
NO_REGION_USER_ID = "u_no_region"
SUSPENDED_USERS = {"u_555"}          # 신고로 이용 정지된 주최자
DELETED_USERS = {"u_556"}            # 탈퇴한 주최자 (모임은 남아 있어도 추천하지 않음)
BLOCKS = {("u_123", "u_666")}        # (차단한 사람, 차단당한 사람)


def at(base, days, hh, mm=0):
    d = base.date() + timedelta(days=days)
    return datetime.combine(d, time(hh, mm), tzinfo=KST)


def seed_meetups(now):
    """추천 조건을 하나씩 확인할 수 있게 만든 모임들. 시각은 서버를 켠 날 기준."""
    rows = [
        # (id, 주최자, 취미, 제목, 장소, 동네, 시작, 정원, 참여자(주최자 제외), 상태)
        (12, "u_201", "등산", "토요일 아침 뒷산 걷기", "역삼1동 주민센터 앞", HOME_REGION, at(now, 4, 9), 8,
         ["u_202", "u_203", "u_204", "u_205"], "open"),
        (15, "u_206", "서예", "붓글씨 함께 써요", "역삼도서관 1층 문화교실", HOME_REGION, at(now, 7, 14), 10,
         ["u_207", "u_208"], "open"),
        (16, "u_209", "수중 걷기", "물속에서 천천히 걷기", "역삼문화센터 수영장", HOME_REGION, at(now, 5, 10), 6,
         ["u_210"], "open"),
        (17, "u_211", "바둑", "복지관 바둑 한 판", "역삼노인복지관 2층", HOME_REGION, at(now, 3, 15), 6, [], "open"),
        # ↓ 추천에서 빠져야 하는 모임들
        (20, "u_212", "서예", "자리가 다 찬 서예 모임", "역삼도서관", HOME_REGION, at(now, 6, 14), 3,
         ["u_213", "u_214"], "open"),
        (21, "u_555", "등산", "정지된 주최자의 모임", "역삼공원 입구", HOME_REGION, at(now, 4, 10), 8, [], "open"),
        (22, "u_215", "등산", "차단한 사람이 참여한 모임", "역삼공원 입구", HOME_REGION, at(now, 5, 9), 8,
         ["u_666"], "open"),
        (23, "u_216", "등산", "다른 동네 등산", "역삼2동 주민센터", "1168065000", at(now, 4, 9), 8, [], "open"),
        (24, "u_217", "등산", "곧 시작하는 모임", "역삼1동 주민센터 앞", HOME_REGION, now + timedelta(minutes=30), 8,
         [], "open"),
        (25, "u_218", "등산", "너무 먼 날 모임", "역삼1동 주민센터 앞", HOME_REGION, at(now, 20, 9), 8, [], "open"),
        (26, "u_123", "서예", "내가 연 모임", "역삼도서관", HOME_REGION, at(now, 8, 14), 6, [], "open"),
        (27, "u_219", "서예", "이미 참여한 모임", "역삼도서관", HOME_REGION, at(now, 9, 14), 6, ["u_123"], "open"),
        (28, "u_220", "등산", "모집 마감한 모임", "역삼공원 입구", HOME_REGION, at(now, 6, 9), 8, [], "closed"),
        (29, "u_556", "등산", "탈퇴한 주최자의 모임", "역삼공원 입구", HOME_REGION, at(now, 5, 10), 8, [], "open"),
    ]
    out = {}
    for mid, host, hobby, title, place, region, starts, cap, members, status in rows:
        out[f"mt_{mid}"] = {
            "meetup_id": f"mt_{mid}", "host_id": host, "hobby": hobby, "title": title, "place_name": place,
            "region_code": region, "starts_at": starts, "capacity": cap,
            "members": [host, *members],  # 주최자 포함 (명세 5)
            "status": status,
            "description": "연락처는 010-0000-0000으로 주세요",  # 응답에 넣지 않는 것 확인용
        }
    return out


def seed_interests():
    return {
        "u_123": [
            {"name": "서예", "source": "manual", "added_at": "2026-11-20T19:10:00+09:00", "removed_at": None},
            {"name": "노래교실", "source": "voice", "added_at": "2026-11-10T20:00:00+09:00",
             "removed_at": "2026-11-21T10:00:00+09:00"},
        ],
    }


def blocked_between(a, b):
    return (a, b) in BLOCKS or (b, a) in BLOCKS


class Store:
    def __init__(self, clock=lambda: datetime.now(KST)):
        self.clock = clock
        self.lock = threading.Lock()
        self.interests = seed_interests()   # user_id → [관심사], 지운 것도 남김
        self.requests = {}                  # request_id → 응답
        self.meetups = seed_meetups(clock())

    # --- 2. 관심사 ---

    def _active(self, user_id):
        return [i for i in self.interests.get(user_id, []) if i["removed_at"] is None]

    def _public(self, user_id):
        return [{"name": i["name"], "source": i["source"], "added_at": i["added_at"]} for i in self._active(user_id)]

    def _recently_removed(self, user_id, name, now):
        limit = timedelta(days=RECENTLY_REMOVED_DAYS)
        return any(i["name"] == name and i["removed_at"] is not None
                   and now - datetime.fromisoformat(i["removed_at"]) <= limit
                   for i in self.interests.get(user_id, []))

    def add_interests(self, body):
        """POST /interests. 같은 request_id면 처음 결과를 그대로 돌려준다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.requests:
                return 200, copy.deepcopy(self.requests[rid])

            if not isinstance(body, dict):
                raise invalid("요청 본문은 JSON 객체여야 합니다")
            require_uuid(rid)
            require_user(body.get("user_id"))
            items = body.get("interests")
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_INTERESTS_PER_REQUEST:
                raise ApiError(400, "TOO_MANY_INTERESTS", f"interests는 1~{MAX_INTERESTS_PER_REQUEST}개여야 합니다")
            names = []
            for item in items:
                if not isinstance(item, dict):
                    raise invalid("interests의 각 항목은 객체여야 합니다")
                name = item.get("name").strip() if isinstance(item.get("name"), str) else ""
                if not 1 <= len(name) <= MAX_NAME_LENGTH:
                    raise ApiError(400, "INVALID_INTEREST_NAME", f"name은 1~{MAX_NAME_LENGTH}자여야 합니다")
                require_text(item.get("original_text"), "original_text")
                source = item.get("source")
                if not isinstance(source, dict):
                    raise invalid("source가 필요합니다")
                require_text(source.get("conversation_id"), "source.conversation_id")
                require_text(source.get("message_id"), "source.message_id")
                names.append(name)

            user_id = body["user_id"]
            now = self.clock()
            results = []
            for name in names:  # 검사가 모두 끝난 뒤에만 저장 (일부만 저장되지 않게)
                if any(i["name"] == name for i in self._active(user_id)):
                    status = "already_exists"
                elif self._recently_removed(user_id, name, now):
                    status = "recently_removed"
                else:
                    self.interests.setdefault(user_id, []).append(
                        {"name": name, "source": "voice", "added_at": now.isoformat(), "removed_at": None})
                    status = "added"
                results.append({"name": name, "status": status})

            resp = {"user_id": user_id, "results": results, "interests": self._public(user_id)}
            self.requests[rid] = resp
            return 200, copy.deepcopy(resp)

    def remove_interest(self, user_id, body):
        """mock 전용: 어르신이 화면에서 지운 것처럼."""
        with self.lock:
            require_user(user_id)
            name = body.get("name").strip() if isinstance(body, dict) and isinstance(body.get("name"), str) else ""
            target = next((i for i in self._active(user_id) if i["name"] == name), None)
            if target is None:
                raise ApiError(404, "NOT_FOUND", "지금 관심사에 없는 이름입니다")
            target["removed_at"] = self.clock().isoformat()
            return 200, {"user_id": user_id, "interests": self._public(user_id)}

    def list_interests(self, user_id):
        with self.lock:
            require_user(user_id)
            return 200, {"user_id": user_id, "interests": copy.deepcopy(self.interests.get(user_id, []))}

    # --- 5. 모임 ---

    def _exclusion(self, m, user_id, region, now):
        """추천에서 빠지는 이유. 빠지지 않으면 None (명세 5 "B가 고르는 조건")."""
        if m["region_code"] != region:
            return "다른 동네"
        if m["status"] != "open":
            return "모집 중 아님"
        if not now + WINDOW_START <= m["starts_at"] <= now + WINDOW_END:
            return "기간 밖"
        if len(m["members"]) >= m["capacity"]:
            return "자리 없음"
        if m["host_id"] == user_id:
            return "내가 연 모임"
        if user_id in m["members"]:
            return "이미 참여"
        if m["host_id"] in SUSPENDED_USERS:
            return "정지된 주최자"
        if m["host_id"] in DELETED_USERS:
            return "탈퇴한 주최자"
        if any(blocked_between(user_id, p) for p in m["members"]):
            return "차단 관계"
        return None

    def recommend(self, query):
        with self.lock:
            user_id = query.get("user_id", [None])[0]
            require_user(user_id)
            raw_limit = query.get("limit", [str(DEFAULT_LIMIT)])[0]
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_LIMIT:
                raise invalid(f"limit은 1~{MAX_LIMIT}이어야 합니다")
            limit = int(raw_limit)
            hobby = query.get("hobby", [None])[0]
            if hobby is not None:
                hobby = hobby.strip()
                if not 1 <= len(hobby) <= MAX_NAME_LENGTH:
                    raise invalid(f"hobby는 1~{MAX_NAME_LENGTH}자여야 합니다")

            # 빈 결과의 이유는 no_region → no_interest → no_match 순서로 먼저 맞는 것 하나 (명세 5)
            def empty(reason):
                return 200, {"user_id": user_id, "meetups": [], "empty_reason": reason}

            if user_id == NO_REGION_USER_ID:
                return empty("no_region")
            if hobby:
                wanted, reason = {hobby}, f"대화에서 나온 활동: {hobby}"
            else:
                wanted = {i["name"] for i in self._active(user_id)}
                reason = None
                if not wanted:
                    return empty("no_interest")

            now = self.clock()
            picked = [m for m in self.meetups.values()
                      if m["hobby"] in wanted and self._exclusion(m, user_id, HOME_REGION, now) is None]
            picked.sort(key=lambda m: m["starts_at"])
            if not picked:
                return empty("no_match")
            return 200, {
                "user_id": user_id,
                "meetups": [{
                    "meetup_id": m["meetup_id"], "title": m["title"], "hobby": m["hobby"],
                    "place_name": m["place_name"], "region_name": REGIONS[m["region_code"]],
                    "starts_at": m["starts_at"].isoformat(), "capacity": m["capacity"],
                    "joined_count": len(m["members"]),
                    "match_reason": reason or f"관심사: {m['hobby']}",
                } for m in picked[:limit]],
                "empty_reason": None,
            }

    def debug_meetups(self, user_id="u_123"):
        """mock 전용: 모임 전체와, 그 사용자에게 추천에서 빠지는 이유."""
        with self.lock:
            now = self.clock()
            return 200, {"user_id": user_id, "meetups": [
                {**{k: v for k, v in m.items() if k != "starts_at"}, "starts_at": m["starts_at"].isoformat(),
                 "excluded_because": self._exclusion(m, user_id, HOME_REGION, now)}
                for m in self.meetups.values()]}


def make_handler(store):
    class Handler(MockHandler):
        log_name = "meetup-mock"

        def route(self, method, path, query, body):
            parts = path.strip("/").split("/")
            if method == "POST" and path == "/interests":
                return store.add_interests(body)
            if method == "GET" and path == "/meetups/recommendations":
                return store.recommend(query)
            if method == "GET" and len(parts) == 3 and parts[:2] == ["mock", "interests"]:
                return store.list_interests(parts[2])
            if method == "POST" and len(parts) == 4 and parts[:2] == ["mock", "interests"] and parts[3] == "remove":
                return store.remove_interest(parts[2], body)
            if method == "GET" and path == "/mock/meetups":
                return store.debug_meetups(query.get("user_id", ["u_123"])[0])
            return super().route(method, path, query, body)

    return Handler


def main():
    run(make_handler(Store()), "취미·관심사 반영(명세 2) + 모임 목록 조회(명세 5) mock", 8057, "관심사·모임 mock")


if __name__ == "__main__":
    main()
