"""사용자·사용량 mock 테스트.  실행: python -m unittest discover tools/mocks"""

import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer

from _common import KST, ApiError
from user_mock import Store, make_handler

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)


def conv(user_id="u_123", started_at="2026-11-24T09:50:00+09:00", **over):
    body = {"request_id": str(uuid.uuid4()), "user_id": user_id,
            "conversation_id": f"c_{uuid.uuid4().hex[:6]}", "started_at": started_at}
    body.update(over)
    return body


def error_code(fn, *args):
    try:
        fn(*args)
    except ApiError as e:
        return e.status, e.code
    raise AssertionError("ApiError가 나야 합니다")


class UserTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(clock=lambda: NOW)

    def test_명세_예시와_같은_사용자(self):
        status, user = self.store.get_user("u_123")
        self.assertEqual(status, 200)
        self.assertEqual(user["display_name"], "김영자 님")
        self.assertEqual(user["guardians"][0]["guardian_id"], "u_789")  # 보호자도 u_
        self.assertEqual({k: v["granted"] for k, v in user["sharing"].items()},
                         {"schedule": True, "health": False, "mood": True})
        self.assertEqual(user["preferences"]["time_hints"]["morning"], "08:00")

    def test_개인정보는_넣지_않음(self):
        _, user = self.store.get_user("u_123")
        for field in ("name", "phone", "birth_year", "region_code", "region_name"):
            self.assertNotIn(field, user)

    def test_처음_보는_사용자는_공유_안_함(self):
        _, user = self.store.get_user("u_new")
        self.assertEqual(user["role"], "senior")
        self.assertIsNone(user["display_name"])
        self.assertEqual(user["sharing"]["health"], {"granted": False, "decided_at": None})

    def test_보호자_계정(self):
        _, user = self.store.get_user("u_789")
        self.assertEqual((user["role"], user["guardians"], user["sharing"], user["preferences"]),
                         ("guardian", [], None, None))

    def test_보호자_없는_사용자(self):
        _, user = self.store.get_user("u_no_guardian")
        self.assertEqual(user["guardians"], [])

    def test_mock으로_공유_범위와_등급_바꾸기(self):
        _, user = self.store.patch_user("u_123", {"sharing": {"health": True}, "plan": "basic"})
        self.assertEqual(user["sharing"]["health"], {"granted": True, "decided_at": NOW.isoformat()})
        self.assertEqual(user["plan"], "basic")
        self.assertEqual(user["updated_at"], NOW.isoformat())

    def test_오류_코드(self):
        self.assertEqual(error_code(self.store.get_user, "u_unknown"), (404, "USER_NOT_FOUND"))
        self.assertEqual(error_code(self.store.patch_user, "u_123", {"phone": "010"}), (400, "INVALID_REQUEST"))
        self.assertEqual(error_code(self.store.patch_user, "u_123", {"plan": "gold"}), (400, "INVALID_REQUEST"))
        self.assertEqual(error_code(self.store.patch_user, "u_789", {"sharing": {"mood": True}}),
                         (400, "INVALID_REQUEST"))


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(clock=lambda: NOW, free_limit=2)

    def test_처음에는_허용(self):
        status, usage = self.store.today("u_123")
        self.assertEqual(status, 200)
        self.assertEqual(usage, {"user_id": "u_123", "date": "2026-11-24", "limit": 2, "used": 0,
                                 "remaining": 2, "allowed": True, "resets_at": "2026-11-25T00:00:00+09:00"})

    def test_한도까지_쓰면_allowed_false(self):
        s1, r1 = self.store.record(conv())
        s2, r2 = self.store.record(conv())
        self.assertEqual((s1, r1["used"], r1["allowed"]), (201, 1, True))
        self.assertEqual((s2, r2["used"], r2["remaining"], r2["allowed"]), (201, 2, 0, False))
        self.assertFalse(self.store.today("u_123")[1]["allowed"])

    def test_한도를_넘어도_기록은_거절하지_않음(self):
        for _ in range(3):  # B 장애 때 허용한 대화를 나중에 기록하는 경우
            status, resp = self.store.record(conv())
        self.assertEqual((status, resp["used"], resp["remaining"], resp["allowed"]), (201, 3, 0, False))

    def test_같은_request_id는_두_번_세지_않음(self):
        body = conv()
        first = self.store.record(body)
        again = self.store.record({**body, "conversation_id": "c_other"})
        self.assertEqual(first[0], 201)
        self.assertEqual(again, (200, first[1]))
        self.assertEqual(self.store.today("u_123")[1]["used"], 1)

    def test_늦게_기록해도_대화한_날짜로(self):
        status, resp = self.store.record(conv(started_at="2026-11-23T23:50:00+09:00"))
        self.assertEqual((status, resp["date"], resp["used"]), (201, "2026-11-23", 1))
        self.assertEqual(resp["resets_at"], "2026-11-24T00:00:00+09:00")
        self.assertEqual(self.store.today("u_123")[1]["used"], 0)  # 오늘 횟수에는 안 들어감

    def test_다른_시간대_시각도_사용자_시간대로_날짜를_정함(self):
        # UTC 14:50 = 한국 23:50 (같은 날), UTC 15:10 = 한국 다음 날 00:10
        _, a = self.store.record(conv(started_at="2026-11-23T14:50:00+00:00"))
        _, b = self.store.record(conv(started_at="2026-11-23T15:10:00+00:00"))
        self.assertEqual((a["date"], b["date"]), ("2026-11-23", "2026-11-24"))

    def test_유료는_무제한(self):
        for _ in range(5):
            _, resp = self.store.record(conv(user_id="u_456"))
        self.assertEqual((resp["limit"], resp["remaining"], resp["used"], resp["allowed"]), (None, None, 5, True))

    def test_등급을_바꾸면_바로_반영(self):
        self.store.record(conv())
        self.store.record(conv())
        self.store.patch_user("u_123", {"plan": "premium"})
        self.assertTrue(self.store.today("u_123")[1]["allowed"])

    def test_사용자마다_따로_셈(self):
        self.store.record(conv(user_id="u_123"))
        self.assertEqual(self.store.today("u_no_guardian")[1]["used"], 0)

    def test_mock으로_오늘_횟수_비우기(self):
        self.store.record(conv())
        self.store.record(conv(started_at="2026-11-23T20:00:00+09:00"))
        _, usage = self.store.reset("u_123")
        self.assertEqual(usage["used"], 0)
        self.assertEqual(len(self.store.conversations), 1)  # 어제 기록은 남김

    def test_오류_코드(self):
        self.assertEqual(error_code(self.store.today, "u_unknown"), (404, "USER_NOT_FOUND"))
        self.assertEqual(error_code(self.store.record, conv(user_id="u_unknown")), (404, "USER_NOT_FOUND"))
        for over in ({"request_id": "abc"}, {"conversation_id": ""}, {"started_at": "2026-11-24T09:50:00"},
                     {"started_at": "아침"}, {"user_id": ""}):
            with self.subTest(over=over):
                self.assertEqual(error_code(self.store.record, conv(**over)), (400, "INVALID_REQUEST"))

    def test_오류는_기록하지_않음(self):
        body = conv(started_at="아침")
        with self.assertRaises(ApiError):
            self.store.record(body)
        status, _ = self.store.record({**body, "started_at": "2026-11-24T09:50:00+09:00"})
        self.assertEqual(status, 201)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(clock=lambda: NOW, free_limit=1)))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_대화_흐름(self):
        # 호칭 조회 → 시작 전 확인 → 기록 → 다음 대화는 막힘
        _, user = self.call("GET", "/users/u_http")
        self.assertEqual(user["plan"], "free")
        _, usage = self.call("GET", "/usage/u_http/today")
        self.assertTrue(usage["allowed"])
        status, usage = self.call("POST", "/usage/conversations", conv(user_id="u_http"))
        self.assertEqual((status, usage["allowed"]), (201, False))
        _, usage = self.call("GET", "/usage/u_http/today")
        self.assertFalse(usage["allowed"])

    def test_mock_PATCH(self):
        status, user = self.call("PATCH", "/mock/users/u_patch", {"display_name": "이순자 님"})
        self.assertEqual((status, user["display_name"]), (200, "이순자 님"))

    def test_없는_경로와_사용자(self):
        status, resp = self.call("GET", "/users/u_unknown")
        self.assertEqual((status, resp["error"]["code"]), (404, "USER_NOT_FOUND"))
        status, resp = self.call("GET", "/usage/u_123")
        self.assertEqual((status, resp["error"]["code"]), (404, "NOT_FOUND"))

    def test_가짜_장애_헤더(self):
        status, resp = self.call("POST", "/usage/conversations", conv(), headers={"X-Mock-Fail": "503"})
        self.assertEqual((status, resp["error"]["code"]), (503, "MOCK_FAILURE"))
        # 6-1(GET)도 장애를 흉내 낼 수 있어야 A가 "B 장애 시 대화 허용"을 확인할 수 있음
        status, resp = self.call("GET", "/usage/u_123/today", headers={"X-Mock-Fail": "500"})
        self.assertEqual((status, resp["error"]["code"]), (500, "MOCK_FAILURE"))
        status, _ = self.call("GET", "/health", headers={"X-Mock-Fail": "500"})
        self.assertEqual(status, 200)  # /health는 그대로


if __name__ == "__main__":
    unittest.main()
