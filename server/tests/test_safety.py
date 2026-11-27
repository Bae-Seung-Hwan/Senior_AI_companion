import datetime as dt
import json
import unittest
from types import SimpleNamespace

from companion.clock import KST
from companion.safety import rules
from companion.safety.b_client import (
    ACCEPTED,
    ALREADY_RESPONDED,
    NOT_FOUND,
    REJECTED,
    UNAVAILABLE,
    USER_NOT_FOUND,
    AlertApi,
    AlertResult,
)
from companion.safety.detect import (
    ClaudeFraudClassifier,
    DetectionError,
    FraudDetector,
    build_user_message,
)
from companion.safety.eval import evaluate, load_cases
from companion.safety.flow import (
    DISMISSED,
    GUARDIAN_CALL,
    GUARDIAN_CHECK,
    SafetyConversation,
    parse_choice,
)
from companion.safety.models import (
    ABOUT_TO_ACT,
    ALREADY_ACTED,
    HIGH,
    MEDIUM,
    RECEIVED,
    REFUSED,
    Detection,
)

from tests.test_b_client import StubB, error

NOW = dt.datetime(2026, 11, 27, 14, 5, 12, tzinfo=KST)
CASES = load_cases()


def verdict(is_fraud=True, category="family_impersonation", stage="received", confidence="high"):
    return {"is_fraud": is_fraud, "category": category, "stage": stage, "confidence": confidence}


class FakeClassifier:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def classify(self, utterance, recent=()):
        self.calls.append((utterance, tuple(recent)))
        result = self.results.pop(0) if self.results else verdict(is_fraud=False, category="none", stage="none")
        if isinstance(result, Exception):
            raise result
        return result


def detector(*results):
    c = FakeClassifier(*results)
    return FraudDetector(c, clock=lambda: NOW), c


class GateTest(unittest.TestCase):
    """규칙 문지기: 사기 문장은 하나도 놓치면 안 된다 (여기서 놓치면 LLM이 보지도 못함)."""

    def test_사기_문장은_모두_통과(self):
        for case in CASES:
            if case["expect"] in ("alert", "refused"):
                self.assertTrue(rules.is_candidate(case["text"], case.get("recent", ())), case["id"])

    def test_돈_이야기_없는_말은_LLM을_부르지_않음(self):
        for text in ("오늘 날씨가 좋네", "무릎이 좀 쑤셔", "손주가 시험 잘 봤대", "내일 치과 가야 해"):
            self.assertFalse(rules.is_candidate(text), text)

    def test_흔한_말_안의_짧은_낱말에_걸리지_않음(self):
        # "기억"의 "억", "건강검사"의 "검사", "사진 보내"의 "보내", "경찰서", "택배" (#25 리뷰)
        for text in ("옛날 기억이 나네", "추억이 많은 곳이야", "내일 병원 검사 받아", "손주 선물 사러 갔어",
                     "딸한테 사진 보내 줬어", "택배가 와서 문 앞에 뒀대", "경찰서 앞에 빵집 생겼어",
                     "카드 게임 했어", "친구 찾아갔어"):
            self.assertFalse(rules.is_candidate(text), text)
        for text in ("3억을 넣으래", "오천만 원 보내래", "몇 억 벌 수 있대"):
            self.assertTrue(rules.is_candidate(text), text)

    def test_앞에서_돈_이야기가_나오고_지금_행동을_말하면_다시_봄(self):
        self.assertTrue(rules.is_candidate("그래서 지금 은행 가는 중이야", ("아들이 돈 보내 달래",)))
        self.assertFalse(rules.is_candidate("그래서 지금 은행 가는 중이야"))


class StrongSignalTest(unittest.TestCase):
    """LLM이 실패했을 때만 쓰는 안전망."""

    def test_요구와_압박이_함께면_알림(self):
        self.assertEqual(rules.strong_signal("아들이 폰이 고장 났다고 급하게 돈 보내 달래"),
                         ("family_impersonation", RECEIVED))
        self.assertEqual(rules.strong_signal("검사라는데 안전한 계좌로 옮기래")[0], "agency_impersonation")
        self.assertEqual(rules.strong_signal("은행 직원이라고 해서 인증번호 불러 줬어"),
                         ("agency_impersonation", ALREADY_ACTED))
        self.assertEqual(rules.strong_signal("원금 보장이라 해서 적금 깨서 보낼게"), ("investment", ABOUT_TO_ACT))

    def test_평범한_돈_이야기는_안전망에_안_걸림(self):
        for case in CASES:
            if case["expect"] == "none":
                self.assertIsNone(rules.strong_signal(case["text"]), case["id"])

    def test_택배_검사_같은_흔한_말은_안전망에_안_걸림(self):
        # LLM 없이 보호자에게 긴급 푸시가 가므로 좁아야 한다 (#25 리뷰)
        for text in ("딸이 택배 보냈대", "병원에서 검사 결과 보내 준대", "우체국에서 소포 보냈어",
                     "아들이 급하게 사진 보내 달래", "손주가 빨리 오라고 문자 보냈어"):
            self.assertIsNone(rules.strong_signal(text), text)


class SeverityTest(unittest.TestCase):
    def d(self, category, stage):
        return Detection("fraud", category, stage, "…", NOW)

    def test_따르려_하거나_이미_따랐으면_high(self):
        self.assertEqual(self.d("investment", ABOUT_TO_ACT).severity, HIGH)
        self.assertEqual(self.d("romance", ALREADY_ACTED).severity, HIGH)

    def test_받기만_했으면_유형에_따라(self):
        self.assertEqual(self.d("family_impersonation", RECEIVED).severity, HIGH)  # 그날 바로 돈이 나가는 유형
        self.assertEqual(self.d("investment", RECEIVED).severity, MEDIUM)

    def test_거절했으면_알리지_않음(self):
        self.assertFalse(self.d("agency_impersonation", REFUSED).should_alert)

    def test_명세_4_요청_본문(self):
        d = Detection("fraud", "family_impersonation", RECEIVED, "아들이 300만원 보내래요", NOW, message_id="m_801")
        self.assertEqual(d.to_alert_request(request_id="rid", user_id="u_123", conversation_id="c_456"), {
            "request_id": "rid", "user_id": "u_123", "type": "fraud", "severity": "high",
            "reason": "가족·지인을 사칭해 돈을 요구하는 연락 의심",
            "evidence": {"text": "아들이 300만원 보내래요", "conversation_id": "c_456", "message_id": "m_801"},
            "user_response": "pending", "detected_at": "2026-11-27T14:05:12+09:00",
        })
        self.assertIn("이미 따르셨다고", Detection("fraud", "other", ALREADY_ACTED, "…", NOW).reason)

    def test_감지_기록에는_원문이_없음(self):
        record = Detection("fraud", "investment", RECEIVED, "코인 두 배 준대", NOW).log_record()
        self.assertNotIn("코인 두 배 준대", json.dumps(record, ensure_ascii=False))


class DetectorTest(unittest.TestCase):
    def test_사기면_감지(self):
        det, c = detector(verdict(stage="about_to_act"))
        d = det.detect("아들이 돈 보내래서 보낼게", message_id="m_1", recent=("안녕",))
        self.assertEqual((d.category, d.stage, d.severity, d.source, d.message_id), (
            "family_impersonation", ABOUT_TO_ACT, HIGH, "llm", "m_1"))
        self.assertEqual(c.calls, [("아들이 돈 보내래서 보낼게", ("안녕",))])

    def test_문지기를_못_넘으면_LLM을_부르지_않음(self):
        det, c = detector()
        self.assertIsNone(det.detect("오늘 날씨가 좋네"))
        self.assertEqual(c.calls, [])

    def test_사기_아님_또는_근거_약함(self):
        det, _ = detector(verdict(is_fraud=False, category="none", stage="none"), verdict(confidence="low"))
        self.assertIsNone(det.detect("손주 용돈 줬어"))
        self.assertIsNone(det.detect("돈 이야기"))

    def test_LLM이_실패하면_규칙_안전망(self):
        det, _ = detector(DetectionError("down"), DetectionError("down"))
        d = det.detect("검사라는데 안전한 계좌로 옮기래")
        self.assertEqual((d.category, d.source), ("agency_impersonation", "rules"))
        self.assertIsNone(det.detect("관리비 자동이체 신청했어"))  # 안전망에 안 걸리면 알리지 않음

    def test_모르는_값은_안전한_쪽으로(self):
        det, _ = detector(verdict(category="weird", stage="weird"))
        d = det.detect("돈 보내래")
        self.assertEqual((d.category, d.stage), ("other", RECEIVED))


def reply(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ClassifierTest(unittest.TestCase):
    def classifier(self, *responses):
        messages = FakeMessages(*responses)
        return ClaudeFraudClassifier(client=SimpleNamespace(messages=messages), api_errors=(ConnectionError,)), messages

    def test_요청_형식(self):
        c, messages = self.classifier(reply(verdict()))
        self.assertEqual(c.classify("돈 보내래", ("아들이 문자 왔어",)), verdict())
        call = messages.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["output_config"]["effort"], "medium")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(call["messages"][0]["content"], build_user_message("돈 보내래", ("아들이 문자 왔어",)))

    def test_실패는_DetectionError(self):
        for r in (ConnectionError("down"), reply(verdict(), "refusal"), reply(verdict(), "max_tokens"),
                  reply("JSON 아님"), reply({"is_fraud": "yes"})):
            c, _ = self.classifier(r)
            with self.assertRaises(DetectionError):
                c.classify("돈 보내래")

    def test_프로그래밍_오류는_그대로_터짐(self):
        c, _ = self.classifier(TypeError("bad arg"))
        with self.assertRaises(TypeError):
            c.classify("돈 보내래")


class FakeAlertApi:
    def __init__(self, create=None, respond=None):
        self.create_results = list(create or [])
        self.respond_results = list(respond or [])
        self.created, self.responded = [], []

    def create(self, body):
        self.created.append(body)
        if self.create_results:
            return self.create_results.pop(0)
        return AlertResult(ACCEPTED, body={"alert_id": f"al_{len(self.created):03d}", "severity": body["severity"],
                                           "deliveries": [{"kind": "alert", "status": "sent"}]})

    def respond(self, alert_id, body):
        self.responded.append((alert_id, body))
        return self.respond_results.pop(0) if self.respond_results else AlertResult(ACCEPTED, body={})


def conversation(*verdicts, api=None):
    ids = iter(f"rid-{i}" for i in range(1, 100))
    det, _ = detector(*verdicts)
    return SafetyConversation(user_id="u_123", conversation_id="c_456", detector=det, api=api or FakeAlertApi(),
                              clock=lambda: NOW, new_request_id=lambda: next(ids))


SCAM = "아들이 폰이 고장 났다고 문자로 급하게 300만원 보내달래요"


class FlowTest(unittest.TestCase):
    def test_감지_즉시_알리고_경고(self):
        api = FakeAlertApi()
        c = conversation(verdict(), api=api)
        said = c.handle(SCAM, "m_801")
        self.assertTrue(said.startswith("잠깐만요. 가족이라며"))
        self.assertIn("보호자분께도 알려 드렸어요", said)
        self.assertEqual(len(api.created), 1)
        self.assertEqual((api.created[0]["user_response"], api.created[0]["evidence"]["message_id"]),
                         ("pending", "m_801"))

    def test_어르신_선택을_4_2로(self):
        api = FakeAlertApi()
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        self.assertEqual(c.handle("알았어, 딸한테 물어볼게"), "좋아요. 가족과 확인하실 때까지는 돈을 보내지 말아 주세요.")
        self.assertEqual(api.responded, [("al_001", {"request_id": "rid-2", "user_response": GUARDIAN_CHECK,
                                                     "responded_at": "2026-11-27T14:05:12+09:00"})])
        self.assertEqual(c.events[0]["user_response"], GUARDIAN_CHECK)

    def test_되물으면_한_번_다시_묻고_그다음엔_감지를_계속(self):
        api = FakeAlertApi()
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        self.assertEqual(c.handle("네?"), "가족에게 확인해 보실래요? 괜찮으시면 괜찮다고 말씀해 주세요.")
        self.assertIsNone(c.handle("손주가 왔어"))
        self.assertEqual(api.responded, [])
        c.expire()  # 정해진 시간 동안 대답 없음
        self.assertEqual(api.responded[0][1]["user_response"], "no_response")

    def test_화면_버튼으로_선택(self):
        api = FakeAlertApi()
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        self.assertEqual(c.respond(GUARDIAN_CALL), "보호자분께 전화 연결해 드릴게요.")
        self.assertIsNone(c.respond(DISMISSED))  # 기다리는 알림이 없음
        self.assertEqual(len(api.responded), 1)

    def test_같은_대화에서_같은_사기는_다시_알리지_않고_더_위급해지면_알림(self):
        api = FakeAlertApi()
        c = conversation(verdict(category="investment"), verdict(category="investment"),
                         verdict(category="investment", stage="already_acted"), api=api)
        c.handle("코인에 넣으면 두 배 준대")
        c.respond(GUARDIAN_CHECK)
        self.assertEqual(c.handle("코인 이야기 또 하면 두 배래"), "아까 말씀드린 것처럼, 돈을 보내기 전에 꼭 가족에게 먼저 확인해 주세요.")
        self.assertEqual(len(api.created), 1)
        self.assertIn("112", c.handle("그래서 아까 코인에 넣었어"))   # medium → high
        self.assertEqual([b["severity"] for b in api.created], [MEDIUM, HIGH])

    def test_거절했으면_칭찬만(self):
        api = FakeAlertApi()
        c = conversation(verdict(stage="refused"), api=api)
        self.assertEqual(c.handle("돈 보내라는 문자 와서 지웠어"), "잘하셨어요. 그런 연락은 답하지 않고 지우는 게 가장 좋아요.")
        self.assertEqual(api.created, [])
        self.assertEqual(c.events[0]["stage"], REFUSED)

    def test_B가_안_되면_경고는_하고_나중에_같은_request_id로(self):
        api = FakeAlertApi(create=[AlertResult(UNAVAILABLE)])
        c = conversation(verdict(), api=api)
        said = c.handle(SCAM)
        self.assertNotIn("보호자분께도 알려 드렸어요", said)   # 보내지 못했으면 알렸다고 하지 않음
        c.handle("확인할게")
        self.assertEqual(api.responded, [])                   # 4-1이 아직이라 4-2를 기다림
        self.assertEqual(c.flush(), 0)
        self.assertEqual([b["request_id"] for b in api.created], ["rid-1", "rid-1"])
        self.assertEqual(api.responded[0][0], "al_002")

    def test_보호자에게_못_갔으면_알렸다고_하지_않음(self):
        api = FakeAlertApi(create=[AlertResult(ACCEPTED, body={"alert_id": "al_1", "deliveries": []})])
        c = conversation(verdict(), api=api)
        self.assertNotIn("보호자분께도", c.handle(SCAM))

    def test_다른_말은_None(self):
        self.assertIsNone(conversation().handle("오늘 날씨 좋네"))


class ReviewFixFlowTest(unittest.TestCase):
    """코드 리뷰 (#25)에서 찾은 흐름 문제."""

    def test_대답하면서_더_위급해지면_새로_알리고_앞_알림엔_선택을_보냄(self):
        api = FakeAlertApi()
        c = conversation(verdict(category="investment"), verdict(category="investment", stage="about_to_act"),
                         api=api)
        c.handle("코인에 넣으면 두 배 준대")
        said = c.handle("알았어, 그래도 지금 은행 가서 보낼게")
        self.assertTrue(said.startswith("잠깐만요."))
        self.assertEqual([b["severity"] for b in api.created], [MEDIUM, HIGH])
        self.assertEqual([(i, b["user_response"]) for i, b in api.responded], [("al_001", GUARDIAN_CHECK)])
        self.assertIs(c.awaiting, c.alerts[1])

    def test_첫_대답에서_이미_따랐다고_하면_감지(self):
        api = FakeAlertApi()
        c = conversation(verdict(), verdict(category="remote_control", stage="already_acted"), api=api)
        c.handle(SCAM)
        self.assertIn("112", c.handle("벌써 인증번호 알려줬어"))
        self.assertEqual(len(api.created), 2)
        self.assertEqual(api.responded[0][1]["user_response"], "no_response")  # 앞 알림이 pending으로 남지 않음

    def test_대답이_아닌_말은_붙잡지_않음(self):
        api = FakeAlertApi()
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        self.assertIsNone(c.handle("내일 치과 예약해 줘"))  # 할 일 흐름이 받을 수 있게
        self.assertIsNotNone(c.awaiting)                   # 화면 버튼·expire()를 기다림
        self.assertEqual(c.handle("괜찮아"), "알겠어요. 그래도 돈을 보내기 전에는 한 번만 더 확인해 주세요.")

    def test_다른_유형의_사기는_따로_알림(self):
        api = FakeAlertApi()
        c = conversation(verdict(), verdict(category="remote_control"), api=api)
        c.handle(SCAM)
        c.respond(GUARDIAN_CHECK)
        self.assertTrue(c.handle("은행이라며 앱을 설치하래").startswith("잠깐만요."))
        self.assertEqual(len(api.created), 2)

    def test_다시_보내도_소용없는_실패는_다시_보내지_않음(self):
        for status in (USER_NOT_FOUND, REJECTED):
            api = FakeAlertApi(create=[AlertResult(status, error_code="X")])
            c = conversation(verdict(), api=api)
            c.handle(SCAM)
            c.respond(GUARDIAN_CHECK)
            self.assertEqual(c.flush(), 0, status)
            self.assertEqual((len(api.created), api.responded), (1, []), status)

    def test_선택_보내기가_거절되면_다시_보내지_않음(self):
        api = FakeAlertApi(respond=[AlertResult(NOT_FOUND)])
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        c.respond(DISMISSED)
        self.assertEqual(c.flush(), 0)
        self.assertEqual(len(api.responded), 1)

    def test_잠깐_안_되던_선택은_다시_보냄(self):
        api = FakeAlertApi(respond=[AlertResult(UNAVAILABLE)])
        c = conversation(verdict(), api=api)
        c.handle(SCAM)
        c.respond(GUARDIAN_CHECK)
        self.assertEqual(c.flush(), 0)
        self.assertEqual([b["request_id"] for _, b in api.responded], ["rid-2", "rid-2"])


class ChoiceTest(unittest.TestCase):
    def test_대답_읽기(self):
        cases = {
            "응 확인해 볼게": GUARDIAN_CHECK, "딸한테 물어봐야겠다": GUARDIAN_CHECK, "네": GUARDIAN_CHECK,
            "아니야, 확인해 볼게": GUARDIAN_CHECK,  # 섞이면 안전한 쪽
            "딸한테 전화 좀 걸어 줘": GUARDIAN_CALL,
            "괜찮아, 진짜 아들이야": DISMISSED, "사기 아니야": DISMISSED,
            "아니": DISMISSED, "싫어": DISMISSED, "아뇨 됐어요": DISMISSED,
            "나중에 확인할게": GUARDIAN_CHECK,
            "네?": None, "뭐라고?": None, "손주가 왔어": None,
        }
        for text, want in cases.items():
            self.assertEqual(parse_choice(text), want, text)


class AlertApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = StubB(path="/alerts", prefix=True)

    @classmethod
    def tearDownClass(cls):
        cls.b.server.shutdown()

    def setUp(self):
        self.b.responses.clear()
        self.b.received.clear()
        self.b.requests.clear()
        self.api = AlertApi(self.b.url, retry_delays=(0,), sleep=lambda s: None)

    def test_4_1과_4_2(self):
        body = {"alert_id": "al_001", "severity": "high",
                "deliveries": [{"kind": "alert", "status": "sent"}, {"kind": "re_alert", "status": "queued"}]}
        self.b.responses += [(202, body), (200, body)]
        created = self.api.create({"request_id": "rid"})
        self.assertEqual((created.status, created.alert_id, created.reached_guardian), (ACCEPTED, "al_001", True))
        self.assertEqual(self.api.respond("al_001", {"request_id": "rid-2"}).status, ACCEPTED)
        self.assertEqual(self.b.requests, [("POST", "/alerts"), ("PATCH", "/alerts/al_001")])

    def test_보호자_없음_또는_모두_실패(self):
        for deliveries in ([], [{"kind": "alert", "status": "failed"}, {"kind": "re_alert", "status": "queued"}]):
            self.b.responses.append((202, {"alert_id": "al_1", "deliveries": deliveries}))
            self.assertFalse(self.api.create({"request_id": "rid"}).reached_guardian)

    def test_오류(self):
        self.b.responses += [(409, error("ALREADY_RESPONDED")), (404, error("NOT_FOUND")),
                             (404, error("USER_NOT_FOUND")), (503, {}), (503, {})]
        self.assertEqual(self.api.respond("al_1", {}).status, ALREADY_RESPONDED)
        self.assertEqual(self.api.respond("al_x", {}).status, NOT_FOUND)
        self.assertEqual(self.api.create({}).status, USER_NOT_FOUND)
        self.assertEqual(self.api.create({"request_id": "rid"}).status, UNAVAILABLE)
        self.assertEqual(self.b.received[-2:], [{"request_id": "rid"}, {"request_id": "rid"}])  # 같은 본문으로 재시도


class EvalTest(unittest.TestCase):
    def test_정답대로_답하는_감지기면_100점(self):
        answers = {}
        for case in CASES:
            if case["expect"] == "none":
                answers[case["text"]] = verdict(is_fraud=False, category="none", stage="none")
            else:
                answers[case["text"]] = verdict(category=case["category"], stage=case["stage"])

        class Oracle:
            def classify(self, utterance, recent=()):
                return answers[utterance]

        report = evaluate(CASES, FraudDetector(Oracle(), clock=lambda: NOW))
        self.assertEqual((report["detection_rate"], report["false_positive_rate"], report["refused_ok"],
                          report["category_accuracy"], report["misses"]), (1.0, 0.0, 1.0, 1.0, []))


if __name__ == "__main__":
    unittest.main()
