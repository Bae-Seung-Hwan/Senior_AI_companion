"""관심사·모임 mock 테스트.  실행: python -m unittest discover tools/mocks"""

import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from _common import KST, ApiError
from meetup_mock import Store, make_handler

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)
SAID = "요즘 주말마다 친구들이랑 뒷산에 올라가요"


def item(name, text=SAID, **over):
    body = {"name": name, "original_text": text, "source": {"conversation_id": "c_456", "message_id": "m_812"}}
    body.update(over)
    return body


def req(*names, user_id="u_123", **over):
    body = {"request_id": str(uuid.uuid4()), "user_id": user_id, "interests": [item(n) for n in names]}
    body.update(over)
    return body


def q(**params):
    return {k: [str(v)] for k, v in params.items()}


def error_code(fn, *args):
    try:
        fn(*args)
    except ApiError as e:
        return e.status, e.code
    raise AssertionError("ApiError가 나야 합니다")


class InterestTest(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.store = Store(clock=lambda: self.clock[0])

    def test_명세_예시(self):
        # 서예는 이미 있고(직접 추가), 노래교실은 최근에 지움
        status, resp = self.store.add_interests(req("등산", "노래교실"))
        self.assertEqual(status, 200)
        self.assertEqual(resp["results"], [{"name": "등산", "status": "added"},
                                           {"name": "노래교실", "status": "recently_removed"}])
        self.assertEqual([(i["name"], i["source"]) for i in resp["interests"]],
                         [("서예", "manual"), ("등산", "voice")])

    def test_이미_있으면_already_exists(self):
        _, resp = self.store.add_interests(req("서예"))
        self.assertEqual(resp["results"], [{"name": "서예", "status": "already_exists"}])

    def test_한_요청_안에_같은_이름이_두_번(self):
        _, resp = self.store.add_interests(req("바둑", "바둑"))
        self.assertEqual([r["status"] for r in resp["results"]], ["added", "already_exists"])
        self.assertEqual(sum(i["name"] == "바둑" for i in resp["interests"]), 1)

    def test_앞뒤_공백은_지움(self):
        _, resp = self.store.add_interests(req("  등산 "))
        self.assertEqual(resp["results"][0]["name"], "등산")

    def test_글자가_다르면_다른_관심사(self):
        self.store.add_interests(req("걷기"))
        _, resp = self.store.add_interests(req("산책"))
        self.assertEqual(resp["results"][0]["status"], "added")

    def test_90일이_지나면_다시_저장(self):
        self.clock[0] = datetime(2026, 11, 21, 10, 0, tzinfo=KST) + timedelta(days=91)
        _, resp = self.store.add_interests(req("노래교실"))
        self.assertEqual(resp["results"][0]["status"], "added")

    def test_지운_뒤_대화로는_다시_안_넣음(self):
        self.store.add_interests(req("바둑"))
        self.store.remove_interest("u_123", {"name": "바둑"})
        _, resp = self.store.add_interests(req("바둑"))
        self.assertEqual(resp["results"][0]["status"], "recently_removed")
        self.assertNotIn("바둑", [i["name"] for i in resp["interests"]])

    def test_같은_request_id는_처음_결과(self):
        body = req("등산")
        first = self.store.add_interests(body)
        again = self.store.add_interests(body)
        self.assertEqual(again, first)  # 두 번째도 added (already_exists로 바뀌지 않음)

    def test_응답에_대화_원문은_없음(self):
        _, resp = self.store.add_interests(req("등산"))
        self.assertNotIn(SAID, json.dumps(resp, ensure_ascii=False))

    def test_오류_코드(self):
        self.assertEqual(error_code(self.store.add_interests, req("등산", user_id="u_unknown")),
                         (404, "USER_NOT_FOUND"))
        self.assertEqual(error_code(self.store.add_interests, req()), (400, "TOO_MANY_INTERESTS"))
        self.assertEqual(error_code(self.store.add_interests, req("a", "b", "c", "d", "e", "f")),
                         (400, "TOO_MANY_INTERESTS"))
        self.assertEqual(error_code(self.store.add_interests, req("  ")), (400, "INVALID_INTEREST_NAME"))
        self.assertEqual(error_code(self.store.add_interests, req("가" * 21)), (400, "INVALID_INTEREST_NAME"))
        bad = [
            {"request_id": "abc"},
            {"interests": [item("등산", text="")]},
            {"interests": [item("등산", source=None)]},
            {"interests": [item("등산", source={"conversation_id": "c_1"})]},
            {"interests": ["등산"]},
        ]
        for over in bad:
            with self.subTest(over=over):
                self.assertEqual(error_code(self.store.add_interests, req("등산", **over)), (400, "INVALID_REQUEST"))

    def test_오류면_하나도_저장하지_않음(self):
        body = req("등산", "가" * 21)
        with self.assertRaises(ApiError):
            self.store.add_interests(body)
        self.assertNotIn("등산", [i["name"] for i in self.store._public("u_123")])


class RecommendTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(clock=lambda: NOW)

    def ids(self, **params):
        _, resp = self.store.recommend(q(**params))
        return [m["meetup_id"] for m in resp["meetups"]]

    def test_관심사에_맞는_모임만_시작_순서대로(self):
        self.store.add_interests(req("등산"))
        _, resp = self.store.recommend(q(user_id="u_123"))
        self.assertEqual([m["meetup_id"] for m in resp["meetups"]], ["mt_12", "mt_15"])  # 11/28 등산, 12/1 서예
        first = resp["meetups"][0]
        self.assertEqual(first, {
            "meetup_id": "mt_12", "title": "토요일 아침 뒷산 걷기", "hobby": "등산",
            "place_name": "역삼1동 주민센터 앞", "region_name": "서울 강남구 역삼1동",
            "starts_at": "2026-11-28T09:00:00+09:00", "capacity": 8, "joined_count": 5,
            "match_reason": "관심사: 등산",
        })
        self.assertIsNone(resp["empty_reason"])

    def test_빠져야_하는_모임은_안_나옴(self):
        # 자리 없음, 정지된 주최자, 차단 관계, 다른 동네, 곧 시작, 너무 먼 날, 내가 연, 이미 참여, 모집 마감,
        # 탈퇴한 주최자
        self.store.add_interests(req("등산"))
        got = set(self.ids(user_id="u_123", limit=10))
        self.assertTrue(got.isdisjoint({f"mt_{n}" for n in range(20, 30)}))

    def test_빠지는_이유를_mock으로_볼_수_있음(self):
        _, resp = self.store.debug_meetups("u_123")
        reasons = {m["meetup_id"]: m["excluded_because"] for m in resp["meetups"]}
        self.assertEqual(reasons["mt_21"], "정지된 주최자")
        self.assertEqual(reasons["mt_22"], "차단 관계")
        self.assertEqual(reasons["mt_20"], "자리 없음")
        self.assertEqual(reasons["mt_29"], "탈퇴한 주최자")
        self.assertIsNone(reasons["mt_12"])

    def test_탈퇴한_주최자의_모임은_hobby로_찾아도_안_나옴(self):
        # mt_29는 등산·역삼1동·5일 뒤·자리 있음 → 주최자가 탈퇴한 것 말고는 추천 조건에 맞음
        reasons = {m["meetup_id"]: m["excluded_because"] for m in self.store.debug_meetups("u_new")[1]["meetups"]}
        self.assertEqual(reasons["mt_29"], "탈퇴한 주최자")
        self.assertEqual(self.ids(user_id="u_123", hobby="등산", limit=10), ["mt_12"])

    def test_빈_결과_이유의_우선순위(self):
        # no_region → no_interest → no_match: 동네가 없으면 관심사·hobby와 상관없이 no_region,
        # 관심사가 있는데 맞는 모임이 없을 때만 no_match
        self.assertEqual(self.store.recommend(q(user_id="u_no_region", hobby="등산"))[1]["empty_reason"],
                         "no_region")
        self.store.add_interests(req("탁구", user_id="u_no_region"))
        self.assertEqual(self.store.recommend(q(user_id="u_no_region"))[1]["empty_reason"], "no_region")
        self.store.add_interests(req("탁구", user_id="u_new"))
        self.assertEqual(self.store.recommend(q(user_id="u_new"))[1]["empty_reason"], "no_match")

    def test_주최자_정보와_설명은_넣지_않음(self):
        self.store.add_interests(req("등산"))
        _, resp = self.store.recommend(q(user_id="u_123"))
        text = json.dumps(resp, ensure_ascii=False)
        for word in ("host", "members", "description", "010-", "u_201", "region_code"):
            self.assertNotIn(word, text)

    def test_hobby로_찾기(self):
        _, resp = self.store.recommend(q(user_id="u_123", hobby="수중 걷기"))
        self.assertEqual([m["meetup_id"] for m in resp["meetups"]], ["mt_16"])
        self.assertEqual(resp["meetups"][0]["match_reason"], "대화에서 나온 활동: 수중 걷기")

    def test_limit(self):
        self.store.add_interests(req("등산", "바둑", "수중 걷기"))
        self.assertEqual(len(self.ids(user_id="u_123")), 3)      # 기본 3
        self.assertEqual(self.ids(user_id="u_123", limit=1), ["mt_17"])  # 가장 빠른 것

    def test_관심사를_지우면_추천에서도_빠짐(self):
        self.store.remove_interest("u_123", {"name": "서예"})
        _, resp = self.store.recommend(q(user_id="u_123"))
        self.assertEqual(resp["empty_reason"], "no_interest")

    def test_빈_결과의_이유(self):
        self.assertEqual(self.store.recommend(q(user_id="u_no_region"))[1]["empty_reason"], "no_region")
        self.assertEqual(self.store.recommend(q(user_id="u_new"))[1]["empty_reason"], "no_interest")
        _, resp = self.store.recommend(q(user_id="u_new", hobby="탁구"))
        self.assertEqual((resp["meetups"], resp["empty_reason"]), ([], "no_match"))

    def test_오류_코드(self):
        self.assertEqual(error_code(self.store.recommend, q(user_id="u_unknown")), (404, "USER_NOT_FOUND"))
        for params in ({}, {"user_id": "u_123", "limit": 0}, {"user_id": "u_123", "limit": 11},
                       {"user_id": "u_123", "limit": "세개"}, {"user_id": "u_123", "hobby": " "}):
            with self.subTest(params=params):
                self.assertEqual(error_code(self.store.recommend, q(**params)), (400, "INVALID_REQUEST"))


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

    def test_관심사_저장하면_추천에_나옴(self):
        _, resp = self.call("GET", "/meetups/recommendations?user_id=u_flow")
        self.assertEqual(resp["empty_reason"], "no_interest")
        status, _ = self.call("POST", "/interests", req("바둑", user_id="u_flow"))
        self.assertEqual(status, 200)
        _, resp = self.call("GET", "/meetups/recommendations?user_id=u_flow")
        self.assertEqual([m["meetup_id"] for m in resp["meetups"]], ["mt_17"])

    def test_hobby_쿼리_한글(self):
        status, resp = self.call("GET", "/meetups/recommendations?user_id=u_123&hobby=%EC%88%98%EC%A4%91%20%EA%B1%B7%EA%B8%B0")
        self.assertEqual((status, resp["meetups"][0]["meetup_id"]), (200, "mt_16"))

    def test_mock_지우기(self):
        self.call("POST", "/interests", req("탁구", user_id="u_rm"))
        status, resp = self.call("POST", "/mock/interests/u_rm/remove", {"name": "탁구"})
        self.assertEqual((status, resp["interests"]), (200, []))
        status, resp = self.call("POST", "/mock/interests/u_rm/remove", {"name": "탁구"})
        self.assertEqual((status, resp["error"]["code"]), (404, "NOT_FOUND"))
        _, listed = self.call("GET", "/mock/interests/u_rm")
        self.assertIsNotNone(listed["interests"][0]["removed_at"])

    def test_가짜_장애_헤더(self):
        status, resp = self.call("GET", "/meetups/recommendations?user_id=u_123", headers={"X-Mock-Fail": "503"})
        self.assertEqual((status, resp["error"]["code"]), (503, "MOCK_FAILURE"))

    def test_로그에_대화_원문을_남기지_않음(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.call("POST", "/interests", req("등산", user_id="u_log"))
        self.assertIn("/interests", out.getvalue())
        self.assertNotIn(SAID, out.getvalue())


if __name__ == "__main__":
    unittest.main()
