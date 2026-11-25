"""긴급 알림 mock 테스트.  실행: python -m unittest discover tools/mocks"""

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

from alert_mock import KST, ApiError, Store, make_handler

NOW = datetime(2026, 11, 22, 14, 5, 12, tzinfo=KST)  # 명세 4-1 예시와 같은 시각
EVIDENCE = "아들이 휴대폰이 고장 났다고 문자로 급하게 300만원만 보내달래요."

# 명세 공통 규칙 "보호자 발송 기록 (deliveries)" 형식
_PUSH = {"guardian_id": "u_789", "channel": "push", "status": "sent", "scheduled_at": None}
URGENT = {**_PUSH, "kind": "alert", "priority": "urgent"}  # high: 긴급 채널
NORMAL = {**_PUSH, "kind": "alert", "priority": "normal"}  # medium: 일반 알림
RE_ALERT = {**_PUSH, "kind": "re_alert", "priority": "urgent", "status": "queued",
            "scheduled_at": "2026-11-22T14:10:12+09:00"}  # NOW + 5분
RE_ALERT_CANCELED = {**RE_ALERT, "status": "canceled"}
FOLLOW_UP = {**_PUSH, "kind": "follow_up", "priority": "normal"}
DELIVERY_KEYS = {"guardian_id", "channel", "kind", "priority", "status", "scheduled_at"}


def req(**over):
    body = {
        "request_id": str(uuid.uuid4()),
        "user_id": "u_123",
        "type": "fraud",
        "severity": "high",
        "reason": "가족을 사칭해 급하게 송금을 요구하는 메신저 피싱 의심",
        "evidence": {"text": EVIDENCE, "conversation_id": "c_456", "message_id": "m_801"},
        "user_response": "pending",
        "detected_at": "2026-11-22T14:05:12+09:00",
    }
    body.update(over)
    return body


def patch(user_response, **over):
    body = {
        "request_id": str(uuid.uuid4()),
        "user_response": user_response,
        "responded_at": "2026-11-22T14:05:40+09:00",
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

    def respond(self, alert_id, user_response, **over):
        return self.store.respond(alert_id, patch(user_response, **over))

    def patch_error(self, alert_id, user_response, **over):
        with self.assertRaises(ApiError) as ctx:
            self.respond(alert_id, user_response, **over)
        return ctx.exception.status, ctx.exception.code

    def test_high는_긴급_푸시와_재알림(self):
        status, resp = self.create()
        self.assertEqual(status, 202)
        # 명세 4-1 응답 예시와 같음
        self.assertEqual(resp, {"alert_id": "al_001", "severity": "high", "deliveries": [URGENT, RE_ALERT]})

    def test_medium은_일반_푸시만(self):
        _, resp = self.create(type="emergency", severity="medium")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("medium", [NORMAL]))

    def test_deliveries는_공통_형식(self):
        _, a = self.create(type="fraud", severity="high")
        self.respond(a["alert_id"], "dismissed")
        _, b = self.create(type="emergency", severity="medium")
        self.respond(b["alert_id"], "no_response")
        self.create(user_id="u_push_fail")
        rows = [d for alert in self.store.list() for d in alert["deliveries"]]
        self.assertTrue(rows)
        for d in rows:
            with self.subTest(d=d):
                self.assertEqual(set(d), DELIVERY_KEYS)
                self.assertEqual(d["channel"], "push")  # MVP는 앱 푸시만 (알림톡 없음)
                self.assertIn(d["kind"], {"alert", "re_alert", "follow_up"})
                self.assertIn(d["priority"], {"urgent", "normal"})
                self.assertIn(d["status"], {"queued", "sent", "failed", "scheduled", "canceled"})
                # scheduled_at은 scheduled와 아직 안 보낸 re_alert에만
                has_time = d["status"] == "scheduled" or (d["kind"] == "re_alert" and d["status"] != "sent")
                self.assertEqual(d["scheduled_at"] is not None, has_time)
                if d["status"] == "canceled":
                    self.assertEqual(d["kind"], "re_alert")

    # --- 4-2 PATCH와 severity 규칙 ---

    def test_사기_dismissed면_medium으로_낮추고_재알림_취소(self):
        _, first = self.create(type="fraud", severity="high")
        status, resp = self.respond(first["alert_id"], "dismissed")
        self.assertEqual(status, 200)
        # 명세 4-2 응답 예시와 같음: 보낸 긴급 푸시는 그대로, 아직 안 보낸 재알림만 취소, 괜찮다는 후속 푸시
        self.assertEqual(resp, {"alert_id": first["alert_id"], "severity": "medium",
                                "deliveries": [URGENT, RE_ALERT_CANCELED, FOLLOW_UP]})

    def test_응급_dismissed는_그대로(self):
        _, first = self.create(type="emergency", severity="high")
        _, resp = self.respond(first["alert_id"], "dismissed")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("high", [URGENT, RE_ALERT, FOLLOW_UP]))

    def test_응급_no_response면_high로_올리고_긴급_푸시_다시(self):
        _, first = self.create(type="emergency", severity="medium")
        _, resp = self.respond(first["alert_id"], "no_response")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("high", [NORMAL, URGENT, RE_ALERT]))

    def test_사기_no_response는_그대로(self):
        _, first = self.create(type="fraud", severity="medium")
        _, resp = self.respond(first["alert_id"], "no_response")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("medium", [NORMAL]))

    def test_그_밖의_선택은_그대로(self):
        for choice in ("call_119_requested", "guardian_call_requested", "guardian_check_requested"):
            with self.subTest(choice=choice):
                _, first = self.create()
                _, resp = self.respond(first["alert_id"], choice)
                self.assertEqual(resp, first)

    def test_POST에서_바로_최종_선택을_보내면_규칙_적용(self):
        _, resp = self.create(type="fraud", severity="high", user_response="dismissed")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("medium", [NORMAL]))
        _, resp = self.create(type="emergency", severity="medium", user_response="no_response")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("high", [URGENT, RE_ALERT]))
        # 이미 최종 선택이 있으니 PATCH는 409
        self.assertEqual(self.patch_error(resp["alert_id"], "dismissed"), (409, "ALREADY_RESPONDED"))

    def test_선택은_한_번만(self):
        _, first = self.create()
        self.respond(first["alert_id"], "guardian_check_requested")
        self.assertEqual(self.patch_error(first["alert_id"], "dismissed"), (409, "ALREADY_RESPONDED"))

    def test_PATCH_같은_request_id는_처음_응답_그대로(self):
        _, first = self.create()
        body = patch("dismissed")
        once = self.store.respond(first["alert_id"], body)
        again = self.store.respond(first["alert_id"], body)
        self.assertEqual(again, once)
        self.assertEqual(len(self.store.list()[0]["deliveries"]), 3)  # 후속 푸시를 두 번 보내지 않음

    def test_PATCH_다른_알림에_쓴_request_id는_400(self):
        # 리뷰에서 재현한 버그: 같은 request_id로 다른 알림에 PATCH하면 앞 알림 응답이 오고 뒤 알림은 안 바뀌었음
        _, a = self.create(type="fraud", severity="high")
        _, b = self.create(type="emergency", severity="medium")
        body = patch("dismissed")
        self.store.respond(a["alert_id"], body)
        with self.assertRaises(ApiError) as ctx:
            self.store.respond(b["alert_id"], {**body, "user_response": "no_response"})
        self.assertEqual((ctx.exception.status, ctx.exception.code), (400, "INVALID_REQUEST"))
        b_now = self.store.list()[1]
        self.assertEqual((b_now["user_response"], b_now["severity"]), ("pending", "medium"))
        # 새 request_id로 보내면 정상 처리
        _, resp = self.respond(b["alert_id"], "no_response")
        self.assertEqual((resp["alert_id"], resp["severity"]), (b["alert_id"], "high"))
        # 처음 알림의 재요청은 여전히 처음 응답 그대로
        self.assertEqual(self.store.respond(a["alert_id"], body)[1]["alert_id"], a["alert_id"])

    def test_PATCH_오류_코드(self):
        _, first = self.create()
        aid = first["alert_id"]
        self.assertEqual(self.patch_error("al_999", "dismissed"), (404, "NOT_FOUND"))
        self.assertEqual(self.patch_error(aid, "pending"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.patch_error(aid, "ok"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.patch_error(aid, "dismissed", request_id="abc"), (400, "INVALID_REQUEST"))
        self.assertEqual(self.patch_error(aid, "dismissed", responded_at="2026-11-22T14:05:40"),
                         (400, "INVALID_REQUEST"))
        # 오류는 기록하지 않으므로 이후 정상 선택은 됨
        status, _ = self.respond(aid, "dismissed")
        self.assertEqual(status, 200)

    def test_보호자가_열어_보면_재알림_취소(self):
        _, first = self.create(type="emergency", severity="high")
        status, resp = self.store.open(first["alert_id"])
        self.assertEqual((status, resp["deliveries"]), (200, [URGENT, RE_ALERT_CANCELED]))
        # 다시 열어도 그대로, 이후 선택도 정상 처리
        self.assertEqual(self.store.open(first["alert_id"])[1], resp)
        _, resp = self.respond(first["alert_id"], "dismissed")
        self.assertEqual(resp["deliveries"], [URGENT, RE_ALERT_CANCELED, FOLLOW_UP])
        with self.assertRaises(ApiError) as ctx:
            self.store.open("al_999")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (404, "NOT_FOUND"))

    def test_푸시_발송이_모두_실패하면_alert가_전부_failed(self):
        # A는 kind: "alert"인 줄이 전부 failed면 어르신에게 119 직접 전화를 안내한다
        _, resp = self.create(user_id="u_push_fail", type="emergency", severity="medium")
        self.assertEqual(resp["deliveries"], [{**NORMAL, "status": "failed"}])
        _, resp = self.respond(resp["alert_id"], "no_response")
        self.assertEqual(resp["severity"], "high")
        alerts = [d for d in resp["deliveries"] if d["kind"] == "alert"]
        self.assertEqual(alerts, [{**NORMAL, "status": "failed"}, {**URGENT, "status": "failed"}])
        self.assertEqual(alerts, resp["deliveries"])  # 실패한 긴급 푸시에는 재알림을 걸지 않음

    def test_보호자_없으면_PATCH도_빈_deliveries(self):
        _, first = self.create(user_id="u_no_guardian", type="emergency", severity="medium")
        _, resp = self.respond(first["alert_id"], "no_response")
        self.assertEqual((resp["severity"], resp["deliveries"]), ("high", []))

    def test_같은_request_id는_처음_응답_그대로(self):
        body = req()
        first = self.store.create(body)
        again = self.store.create({**body, "severity": "medium"})
        self.assertEqual(again, first)
        self.assertEqual(len(self.store.list()), 1)

    def test_보호자_없으면_빈_deliveries(self):
        status, resp = self.create(user_id="u_no_guardian")
        self.assertEqual((status, resp["deliveries"]), (202, []))

    def test_오류_코드(self):
        self.assertEqual(self.error_code(user_id="u_unknown"), (404, "USER_NOT_FOUND"))
        bad_cases = [
            {"request_id": "abc"},
            {"user_id": ""},
            {"type": "health"},
            {"severity": "low"},
            {"reason": " "},
            {"evidence": None},
            {"evidence": {"conversation_id": "c_456"}},
            {"user_response": "ok"},
            {"user_response": None},
            {"detected_at": "어제"},
            {"detected_at": "2026-11-22T14:05:12"},  # 시간대 없음
        ]
        for over in bad_cases:
            with self.subTest(over=over):
                self.assertEqual(self.error_code(**over), (400, "INVALID_REQUEST"))

    def test_오류는_저장하지_않음(self):
        body = req(type="")
        with self.assertRaises(ApiError):
            self.store.create(body)
        status, _ = self.store.create({**body, "type": "fraud"})
        self.assertEqual(status, 202)
        self.assertEqual(len(self.store.list()), 1)


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

    def test_알림_후_목록에_보임(self):
        status, resp = self.call("POST", "/alerts", req(user_id="u_http"))
        self.assertEqual(status, 202)
        _, listed = self.call("GET", "/alerts?user_id=u_http")
        self.assertEqual([a["alert_id"] for a in listed["alerts"]], [resp["alert_id"]])

    def test_오류_형식(self):
        status, resp = self.call("POST", "/alerts", req(user_id="u_unknown"))
        self.assertEqual(status, 404)
        self.assertEqual(resp["error"]["code"], "USER_NOT_FOUND")
        self.assertIn("message", resp["error"])

    def test_가짜_장애_헤더(self):
        status, resp = self.call("POST", "/alerts", req(), headers={"X-Mock-Fail": "503"})
        self.assertEqual((status, resp["error"]["code"]), (503, "MOCK_FAILURE"))

    def test_PATCH로_선택_전달(self):
        _, first = self.call("POST", "/alerts", req(type="emergency", severity="medium"))
        status, resp = self.call("PATCH", f"/alerts/{first['alert_id']}", patch("no_response"))
        self.assertEqual((status, resp["severity"]), (200, "high"))
        status, resp = self.call("PATCH", f"/alerts/{first['alert_id']}", patch("dismissed"))
        self.assertEqual((status, resp["error"]["code"]), (409, "ALREADY_RESPONDED"))
        status, resp = self.call("PATCH", "/alerts/al_999", patch("dismissed"))
        self.assertEqual((status, resp["error"]["code"]), (404, "NOT_FOUND"))

    def test_보호자_열어_봄_엔드포인트(self):
        _, first = self.call("POST", "/alerts", req())
        status, resp = self.call("POST", f"/alerts/{first['alert_id']}/opened")
        self.assertEqual((status, [d["status"] for d in resp["deliveries"]]), (200, ["sent", "canceled"]))
        status, resp = self.call("POST", "/alerts/al_999/opened")
        self.assertEqual((status, resp["error"]["code"]), (404, "NOT_FOUND"))

    def test_잘못된_지연_헤더는_무시(self):
        for value in ("abc", "-1"):
            status, _ = self.call("POST", "/alerts", req(), headers={"X-Mock-Delay": value})
            self.assertEqual(status, 202)

    def test_로그에_근거_발화를_남기지_않음(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.call("POST", "/alerts", req())
            self.call("POST", "/alerts", req(severity="low"))
        self.assertIn("/alerts", out.getvalue())
        self.assertNotIn(EVIDENCE, out.getvalue())


if __name__ == "__main__":
    unittest.main()
