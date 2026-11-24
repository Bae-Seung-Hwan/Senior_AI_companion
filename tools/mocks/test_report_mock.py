"""하루 요약 mock 테스트.  실행: python -m unittest discover tools/mocks"""

import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer

from report_mock import KST, ApiError, Store, make_handler

NOW = datetime(2026, 11, 22, 21, 0, 5, tzinfo=KST)
HEALTH = "무릎이 조금 쑤신다고 하셔서 집에서 쉬셨어요."

PUSH_QUEUED = {"guardian_id": "u_789", "channel": "push", "status": "queued"}
PUSH_SENT = {"guardian_id": "u_789", "channel": "push", "status": "sent"}


def req(**over):
    body = {
        "request_id": str(uuid.uuid4()),
        "user_id": "u_123",
        "date": "2026-11-22",
        "brief": "오늘도 편안하게 잘 지내셨어요.",
        "sections": {
            "schedule": "오후에 치과에 다녀오셨고, 혈압약도 잘 챙겨 드셨어요.",
            "health": HEALTH,
            "mood": "손주 시험 이야기를 하시며 즐거워하셨어요.",
        },
        "mood_level": "good",
        "stats": {"conversation_count": 6, "completed_todos": 2},
        "generated_at": "2026-11-22T21:00:00+09:00",
    }
    body.update(over)
    return body


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(clock=lambda: NOW)

    def submit(self, **over):
        return self.store.submit(req(**over))

    def error_code(self, **over):
        with self.assertRaises(ApiError) as ctx:
            self.submit(**over)
        return ctx.exception.status, ctx.exception.code

    def test_기본은_아무것도_공유하지_않음(self):
        status, resp = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(resp, {"report_id": "r_001", "shared_sections": [], "deliveries": [PUSH_QUEUED]})
        view = self.store.list()[0]["guardian_view"]
        self.assertEqual(view["brief"], "오늘도 편안하게 잘 지내셨어요.")
        self.assertEqual(view["sections"], {})
        self.assertNotIn("mood_level", view)

    def test_모두_공유하는_사용자(self):
        _, resp = self.submit(user_id="u_share_all")
        self.assertEqual(resp["shared_sections"], ["schedule", "health", "mood"])
        view = self.store.list()[0]["guardian_view"]
        self.assertEqual(view["sections"]["health"], HEALTH)
        self.assertEqual(view["mood_level"], "good")

    def test_켠_항목만_전달(self):
        self.store.set_scope("u_123", {"schedule": True, "mood": True})
        _, resp = self.submit()
        self.assertEqual(resp["shared_sections"], ["schedule", "mood"])
        view = self.store.list()[0]["guardian_view"]
        self.assertEqual(set(view["sections"]), {"schedule", "mood"})
        self.assertEqual(view["mood_level"], "good")

    def test_기분을_안_켜면_mood_level도_안_보임(self):
        self.store.set_scope("u_123", {"schedule": True})
        self.submit()
        self.assertNotIn("mood_level", self.store.list()[0]["guardian_view"])

    def test_비어_있는_section은_공유_목록에서_뺌(self):
        _, resp = self.submit(user_id="u_share_all", sections={"schedule": None, "health": HEALTH})
        self.assertEqual(resp["shared_sections"], ["health"])

    def test_푸시_본문에는_brief만(self):
        self.submit(user_id="u_share_all")
        saved = self.store.list()[0]
        self.assertEqual(saved["push_body"], "오늘도 편안하게 잘 지내셨어요.")
        self.assertNotIn(HEALTH, saved["push_body"])

    def test_보호자_없으면_빈_deliveries(self):
        status, resp = self.submit(user_id="u_no_guardian")
        self.assertEqual((status, resp["deliveries"]), (202, []))

    def test_보내기_전에는_같은_날짜를_덮어씀(self):
        _, first = self.submit()
        _, second = self.submit(brief="오늘은 조용히 쉬셨어요.")
        self.assertEqual(first["report_id"], second["report_id"])
        reports = self.store.list()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["brief"], "오늘은 조용히 쉬셨어요.")

    def test_다른_날짜는_새_요약(self):
        _, first = self.submit()
        _, second = self.submit(date="2026-11-23")
        self.assertNotEqual(first["report_id"], second["report_id"])

    def test_같은_request_id는_새로_처리하지_않음(self):
        body = req()
        first = self.store.submit(body)
        again = self.store.submit({**body, "brief": "바뀐 요약"})
        self.assertEqual(again, first)
        self.assertEqual(self.store.list()[0]["brief"], "오늘도 편안하게 잘 지내셨어요.")

    def test_덮어쓴_뒤에도_이전_request_id를_기억(self):
        old = req()
        self.store.submit(old)
        self.submit(brief="새 요약")  # 다른 request_id로 덮어씀
        status, resp = self.store.submit({**old, "brief": "예전 요약 재시도"})
        self.assertEqual(status, 202)
        self.assertEqual(self.store.list()[0]["brief"], "새 요약")  # 예전 재시도가 덮어쓰지 않음
        self.assertEqual(len(self.store.list()[0]["request_ids"]), 2)
        self.assertEqual(resp["report_id"], "r_001")

    def test_보낸_뒤에는_409_ALREADY_SENT(self):
        old = req()
        _, first = self.store.submit(old)
        status, sent = self.store.send(first["report_id"])
        self.assertEqual((status, sent["deliveries"]), (200, [PUSH_SENT]))
        self.assertEqual(self.error_code(brief="늦게 온 요약"), (409, "ALREADY_SENT"))
        # 보낸 뒤에도 원래 request_id 재시도는 저장된 결과를 돌려줌 (409 아님)
        status, resp = self.store.submit(old)
        self.assertEqual((status, resp["deliveries"]), (202, [PUSH_SENT]))

    def test_공유_범위를_바꿔도_이미_받은_요약의_목록은_그대로(self):
        self.submit()
        self.store.set_scope("u_123", {"health": True})
        saved = self.store.list()[0]
        self.assertEqual(saved["shared_sections"], [])
        self.assertEqual(set(saved["guardian_view"]["sections"]), {"health"})  # 보호자 화면은 지금 동의 기준

    def test_오류_코드(self):
        self.assertEqual(self.error_code(user_id="u_unknown"), (404, "USER_NOT_FOUND"))
        bad_cases = [
            {"request_id": "abc"},
            {"user_id": ""},
            {"date": "11월 22일"},
            {"brief": " "},
            {"sections": "일정"},
            {"sections": {"health": 3}},
            {"mood_level": "great"},
            {"mood_level": None},
            {"stats": []},
            {"generated_at": "2026-11-22T21:00:00"},  # 시간대 없음
        ]
        for over in bad_cases:
            with self.subTest(over=over):
                self.assertEqual(self.error_code(**over), (400, "INVALID_REQUEST"))

    def test_mock_전용_오류(self):
        with self.assertRaises(ApiError) as ctx:
            self.store.send("r_999")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        with self.assertRaises(ApiError) as ctx:
            self.store.set_scope("u_123", {"location": True})
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_오류는_저장하지_않음(self):
        body = req(mood_level="great")
        with self.assertRaises(ApiError):
            self.store.submit(body)
        status, _ = self.store.submit({**body, "mood_level": "good"})
        self.assertEqual(status, 202)


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

    def test_첫_시연_흐름(self):
        # 어르신이 일정 공유를 켬 → A가 요약을 보냄 → 발송 → 보호자 화면에는 일정만
        status, _ = self.call("PUT", "/mock/share-scope/u_demo", {"schedule": True})
        self.assertEqual(status, 200)
        status, resp = self.call("POST", "/guardian-reports/daily", req(user_id="u_demo"))
        self.assertEqual((status, resp["shared_sections"]), (202, ["schedule"]))
        status, _ = self.call("POST", f"/mock/guardian-reports/{resp['report_id']}/send", {})
        self.assertEqual(status, 200)
        _, listed = self.call("GET", "/guardian-reports?user_id=u_demo")
        view = listed["reports"][0]["guardian_view"]
        self.assertEqual(set(view["sections"]), {"schedule"})
        status, resp = self.call("POST", "/guardian-reports/daily", req(user_id="u_demo"))
        self.assertEqual((status, resp["error"]["code"]), (409, "ALREADY_SENT"))

    def test_가짜_장애_헤더(self):
        status, resp = self.call("POST", "/guardian-reports/daily", req(), headers={"X-Mock-Fail": "503"})
        self.assertEqual((status, resp["error"]["code"]), (503, "MOCK_FAILURE"))

    def test_없는_경로(self):
        status, resp = self.call("POST", "/guardian-reports/weekly", req())
        self.assertEqual((status, resp["error"]["code"]), (404, "NOT_FOUND"))

    def test_로그에_건강_문장을_남기지_않음(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.call("POST", "/guardian-reports/daily", req(user_id="u_log"))
        self.assertIn("/guardian-reports/daily", out.getvalue())
        self.assertNotIn(HEALTH, out.getvalue())


if __name__ == "__main__":
    unittest.main()
