"""할 일 등록 mock 테스트.  실행: python -m unittest discover tools/mocks"""

import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer

from todo_mock import KST, ApiError, Store, make_handler

# 테스트 기준 시각: 2026-11-23(월) 10:00 KST
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=KST)


def req(**over):
    body = {
        "request_id": str(uuid.uuid4()),
        "user_id": "u_123",
        "kind": "appointment",
        "title": "치과 예약",
        "date": "2026-11-24",
        "time": "15:30",
        "time_hint": None,
        "repeat": None,
        "original_text": "내일 오후 세 시 반에 치과 예약 있어요",
        "source": {"conversation_id": "c_456", "message_id": "m_789"},
    }
    body.update(over)
    return body


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(clock=lambda: NOW)

    def create(self, **over):
        return self.store.create(req(**over))

    def error_code(self, **over):
        with self.assertRaises(ApiError) as ctx:
            self.create(**over)
        return ctx.exception.status, ctx.exception.code

    def test_정상_등록(self):
        status, resp = self.create()
        self.assertEqual(status, 201)
        self.assertEqual(resp, {"todo_id": "t_001", "title": "치과 예약",
                                "next_at": "2026-11-24T15:30:00+09:00", "reminder": True})

    def test_같은_request_id는_처음_응답_그대로(self):
        body = req()
        first = self.store.create(body)
        again = self.store.create({**body, "title": "바뀐 제목"})
        self.assertEqual(first[0], 201)
        self.assertEqual(again, (200, first[1]))
        self.assertEqual(len(self.store.list()), 1)

    def test_time_hint는_기본_시각으로(self):
        _, resp = self.create(time=None, time_hint="morning")
        self.assertEqual(resp["next_at"], "2026-11-24T08:00:00+09:00")

    def test_시각_미정이면_알림_없음(self):
        _, resp = self.create(time=None)
        self.assertEqual((resp["next_at"], resp["reminder"]), (None, False))

    def test_매일_복약은_오늘부터_다음_시각(self):
        # 오늘 08:00은 지났으니 내일 08:00
        _, resp = self.create(kind="medication", title="혈압약", date=None, time=None,
                              time_hint="morning", repeat={"freq": "daily"})
        self.assertEqual(resp["next_at"], "2026-11-24T08:00:00+09:00")
        _, resp = self.create(date=None, time="21:00", repeat={"freq": "daily"})
        self.assertEqual(resp["next_at"], "2026-11-23T21:00:00+09:00")

    def test_매주_반복은_다음_해당_요일(self):
        _, resp = self.create(date=None, time="09:00", repeat={"freq": "weekly", "weekdays": ["THU"]})
        self.assertEqual(resp["next_at"], "2026-11-26T09:00:00+09:00")

    def test_오류_코드(self):
        self.assertEqual(self.error_code(date="2026-11-22"), (400, "INVALID_DATE"))
        self.assertEqual(self.error_code(date="2026-11-23", time="09:00"), (400, "INVALID_DATE"))
        self.assertEqual(self.error_code(title="  "), (400, "MISSING_TITLE"))
        self.assertEqual(self.error_code(date=None), (400, "MISSING_SCHEDULE"))
        self.assertEqual(self.error_code(user_id="u_unknown"), (404, "USER_NOT_FOUND"))
        self.assertEqual(self.error_code(kind="party"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.error_code(time="3시"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.error_code(request_id="abc"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.error_code(repeat={"freq": "weekly"}), (400, "INVALID_REQUEST"))
        self.assertEqual(self.error_code(time_hint="morning"), (400, "INVALID_REQUEST"))  # time과 둘 다

    def test_같은_할_일은_409(self):
        _, first = self.create()
        with self.assertRaises(ApiError) as ctx:
            self.create(title=" 치과 예약 ")  # request_id만 다름
        err = ctx.exception
        self.assertEqual((err.status, err.code), (409, "DUPLICATE_TODO"))
        self.assertEqual(err.extra, {"existing_todo_id": first["todo_id"]})
        self.assertEqual(len(self.store.list()), 1)

    def test_중복_판단은_사용자_날짜_시각_제목(self):
        self.create()
        for over in ({"user_id": "u_456"}, {"date": "2026-11-25"}, {"time": "16:00"},
                     {"title": "안과 예약"}, {"time": None, "time_hint": "evening"}):
            self.assertEqual(self.create(**over)[0], 201, over)

    def test_time이_없으면_time_hint로_중복_판단(self):
        self.create(time=None, time_hint="morning")
        self.assertEqual(self.error_code(time=None, time_hint="morning"), (409, "DUPLICATE_TODO"))
        self.assertEqual(self.create(time=None, time_hint="noon")[0], 201)

    def test_중복이어도_같은_request_id면_200(self):
        body = req()
        _, first = self.store.create(body)
        self.assertEqual(self.store.create(body), (200, first))

    def test_잘못된_시각_형식(self):
        for value in ("+5:30", " 5:30", "5:30", "05:3", "١٢:٣٠", "24:00", 1530):
            self.assertEqual(self.error_code(time=value), (400, "INVALID_REQUEST"), value)

    def test_오류는_저장하지_않음(self):
        body = req(title="")
        with self.assertRaises(ApiError):
            self.store.create(body)
        status, _ = self.store.create({**body, "title": "치과"})
        self.assertEqual(status, 201)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(clock=lambda: NOW)))
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

    def test_등록_후_목록에_보임(self):
        status, resp = self.call("POST", "/todos", req(user_id="u_http"))
        self.assertEqual(status, 201)
        _, listed = self.call("GET", "/todos?user_id=u_http")
        self.assertEqual([t["todo_id"] for t in listed["todos"]], [resp["todo_id"]])

    def test_오류_형식(self):
        status, resp = self.call("POST", "/todos", req(date="2026-01-01"))
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "INVALID_DATE")
        self.assertIn("message", resp["error"])

    def test_중복_오류_본문(self):
        _, first = self.call("POST", "/todos", req(user_id="u_dup"))
        status, resp = self.call("POST", "/todos", req(user_id="u_dup"))
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "DUPLICATE_TODO")
        self.assertEqual(resp["error"]["existing_todo_id"], first["todo_id"])
        self.assertIn("message", resp["error"])

    def test_가짜_장애_헤더(self):
        status, resp = self.call("POST", "/todos", req(), headers={"X-Mock-Fail": "503"})
        self.assertEqual((status, resp["error"]["code"]), (503, "MOCK_FAILURE"))

    def test_잘못된_지연_헤더는_무시(self):
        for value in ("abc", "-1"):
            status, _ = self.call("POST", "/todos", req(title=f"지연 {value}"), headers={"X-Mock-Delay": value})
            self.assertEqual(status, 201)

    def test_JSON_아님(self):
        r = urllib.request.Request(self.base + "/todos", data=b"{", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(r)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
