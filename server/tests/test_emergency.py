import datetime as dt
import unittest
from types import SimpleNamespace

from companion.clock import KST
from companion.safety import emergency_rules
from companion.safety.b_client import ACCEPTED, UNAVAILABLE, AlertResult
from companion.safety.detect import (
    EMERGENCY_SCHEMA,
    ClaudeEmergencyClassifier,
    DetectionError,
    EmergencyDetector,
    FraudDetector,
)
from companion.safety.eval import EMERGENCY_SAMPLES, evaluate, load_cases
from companion.safety.flow import (
    ASKED_119,
    ASKED_BOTH,
    ASKED_GUARDIAN,
    CALL_119,
    DISMISSED,
    GUARDIAN_CALL,
    HELPLINE_109,
    SafetyConversation,
    parse_emergency_choice,
)
from companion.safety.models import (
    ACUTE,
    EMERGENCY,
    EMERGENCY_CATEGORIES,
    HIGH,
    MEDIUM,
    MILD,
    PAST,
    RECENT,
    Detection,
)

from tests.test_safety import FakeAlertApi, FakeClassifier, FakeMessages, reply, verdict

NOW = dt.datetime(2026, 11, 27, 21, 10, tzinfo=KST)
CASES = load_cases(EMERGENCY_SAMPLES)


def ev(is_emergency=True, category="fall", stage="acute", confidence="high"):
    return {"is_emergency": is_emergency, "category": category, "stage": stage, "confidence": confidence}


NOT_EMERGENCY = ev(is_emergency=False, category="none", stage="none")


class FakeEmergencyClassifier(FakeClassifier):
    def classify(self, utterance, recent=()):
        if not self.results:  # 정해 둔 답이 다 떨어지면 응급 아님
            self.calls.append((utterance, tuple(recent)))
            return NOT_EMERGENCY
        return super().classify(utterance, recent)


def emergency_detector(*results):
    c = FakeEmergencyClassifier(*results)
    return EmergencyDetector(c, clock=lambda: NOW), c


class GateTest(unittest.TestCase):
    def test_응급_문장은_모두_통과(self):
        for case in CASES:
            if case["expect"] in ("alert", "past"):
                self.assertTrue(emergency_rules.is_candidate(case["text"]), case["id"])

    def test_몸_위험_이야기가_없으면_LLM을_부르지_않음(self):
        for text in ("오늘 날씨가 좋네", "손주가 시험 잘 봤대", "내일 치과 가야 해", "김치찌개 끓였어"):
            self.assertFalse(emergency_rules.is_candidate(text), text)


class StrongSignalTest(unittest.TestCase):
    """LLM이 실패했을 때만 쓰는 안전망: 지금 일어나는 분명한 위급만."""

    def test_분명한_위급(self):
        cases = {
            "화장실에서 넘어져서 못 일어나겠어.": "fall",
            "가슴이 너무 조이고 아파.": "chest",
            "숨이 막혀서 말을 잘 못 하겠어.": "breathing",
            "갑자기 말이 잘 안 나오고 혀가 꼬여.": "speech_or_weakness",
            "방금 잠깐 정신을 잃었던 것 같아.": "fainting",
            "칼에 베였는데 피가 안 멈춰.": "bleeding_injury",
            "부엌에서 불이 났어!": "fire_gas",
            "집에서 가스 냄새가 많이 나.": "fire_gas",
            "영감이 쓰러져서 말을 안 해.": "other_person",
            "이제 그만 살고 싶어.": "self_harm",
            "살려 줘, 구급차 좀 불러 줘.": "other",
        }
        for text, category in cases.items():
            self.assertEqual(emergency_rules.strong_signal(text), (category, ACUTE), text)

    def test_응급_아닌_말과_지난_일은_안전망에_안_걸림(self):
        for case in CASES:
            if case["expect"] in ("none", "past"):
                self.assertIsNone(emergency_rules.strong_signal(case["text"]), case["id"])


class SeverityTest(unittest.TestCase):
    def d(self, category, stage):
        return Detection(EMERGENCY, category, stage, "…", NOW)

    def test_지금_심하면_high(self):
        self.assertEqual(self.d("fall", ACUTE).severity, HIGH)

    def test_견딜_만해도_시간이_중요한_유형은_high(self):
        for category in ("chest", "breathing", "speech_or_weakness", "fainting", "choking", "fire_gas", "self_harm"):
            self.assertEqual(self.d(category, MILD).severity, HIGH, category)
        self.assertEqual(self.d("fall", MILD).severity, MEDIUM)
        self.assertEqual(self.d("bleeding_injury", MILD).severity, MEDIUM)

    def test_지난_일은_알리지_않지만_죽고_싶다는_말은_알림(self):
        self.assertFalse(self.d("fall", PAST).should_alert)
        self.assertFalse(self.d("fainting", PAST).should_alert)
        self.assertTrue(self.d("self_harm", PAST).should_alert)

    def test_방금_지나간_일은_시간이_중요한_유형만_알림(self):
        # "방금 잠깐 정신을 잃었어"는 지금 괜찮아도 알림, "아까 베였는데 이제 괜찮아"는 알리지 않음 (평가에서 찾음)
        self.assertEqual((self.d("fainting", RECENT).should_alert, self.d("fainting", RECENT).severity), (True, HIGH))
        self.assertTrue(self.d("speech_or_weakness", RECENT).should_alert)
        self.assertFalse(self.d("bleeding_injury", RECENT).should_alert)
        self.assertFalse(self.d("fall", RECENT).should_alert)
        self.assertIn("방금 있었고", self.d("fainting", RECENT).reason)

    def test_보호자용_설명은_진단처럼_들리지_않음(self):
        banned = ("뇌졸중", "심근경색", "심장마비", "치매", "진단", "증상", "의심", "질환")
        for category, reason in EMERGENCY_CATEGORIES.items():
            self.assertFalse([w for w in banned if w in reason], category)
        self.assertEqual(self.d("fall", MILD).reason, "넘어지셨다고 하심 (지금은 견딜 만하다고 하심)")

    def test_명세_4_요청_본문(self):
        body = self.d("fall", ACUTE).to_alert_request(request_id="rid", user_id="u_123", conversation_id="c_1")
        self.assertEqual((body["type"], body["severity"], body["reason"], body["user_response"]),
                         ("emergency", "high", "넘어지셨다고 하심", "pending"))


class DetectorTest(unittest.TestCase):
    def test_응급이면_감지(self):
        det, c = emergency_detector(ev(category="chest", stage="mild"))
        d = det.detect("가슴이 좀 뻐근해", message_id="m_1")
        self.assertEqual((d.type, d.category, d.stage, d.severity), (EMERGENCY, "chest", MILD, HIGH))

    def test_확신이_낮아도_알림(self):  # 사기와 다름: 놓치는 것이 더 위험
        det, _ = emergency_detector(ev(confidence="low"))
        self.assertIsNotNone(det.detect("넘어졌어"))

    def test_모르는_값은_안전한_쪽으로(self):
        det, _ = emergency_detector(ev(category="weird", stage="weird"))
        d = det.detect("아파")
        self.assertEqual((d.category, d.stage, d.severity), ("other", ACUTE, HIGH))

    def test_응급_아님(self):
        det, _ = emergency_detector(NOT_EMERGENCY)
        self.assertIsNone(det.detect("더워 죽겠어"))

    def test_LLM이_실패하면_규칙_안전망(self):
        det, _ = emergency_detector(DetectionError("down"), DetectionError("down"))
        d = det.detect("화장실에서 넘어져서 못 일어나겠어")
        self.assertEqual((d.category, d.stage, d.source), ("fall", ACUTE, "rules"))
        self.assertIsNone(det.detect("더워 죽겠어"))

    def test_분류기_요청_형식(self):
        messages = FakeMessages(reply(ev()))
        c = ClaudeEmergencyClassifier(client=SimpleNamespace(messages=messages), api_errors=(ConnectionError,))
        self.assertEqual(c.classify("넘어졌어"), ev())
        call = messages.calls[0]
        self.assertEqual((call["model"], call["output_config"]["format"]["schema"]),
                         ("claude-haiku-5-5", EMERGENCY_SCHEMA))
        bad = ClaudeEmergencyClassifier(client=SimpleNamespace(messages=FakeMessages(reply({"is_fraud": True}))),
                                        api_errors=(ConnectionError,))
        with self.assertRaises(DetectionError):
            bad.classify("넘어졌어")


def conversation(emergency=(), fraud=(), api=None):
    ids = iter(f"rid-{i}" for i in range(1, 100))
    e, _ = emergency_detector(*emergency)
    f = FraudDetector(FakeClassifier(*fraud), clock=lambda: NOW)
    return SafetyConversation(user_id="u_123", conversation_id="c_456", detectors=[e, f], api=api or FakeAlertApi(),
                              clock=lambda: NOW, new_request_id=lambda: next(ids))


FALL = "화장실에서 넘어져서 못 일어나겠어"


class FlowTest(unittest.TestCase):
    def test_응급_경고와_알림(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        said = c.handle(FALL, "m_9")
        self.assertEqual(said, "많이 다치셨어요? 무리해서 일어나지 마시고 그대로 계세요. 보호자분께도 알려 드렸어요. "
                               "119에 연결해 드릴까요, 보호자분께 전화해 드릴까요?")
        self.assertEqual((api.created[0]["type"], api.created[0]["severity"]), ("emergency", "high"))
        self.assertEqual(c.awaiting.asked, ASKED_BOTH)

    def test_119를_고르면_4_2와_전화(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("119 불러 줘"), "119에 연결해 드릴게요.")
        self.assertEqual(api.responded[0][1]["user_response"], CALL_119)
        self.assertEqual(c.action, "call_119")

    def test_보호자_전화(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("딸한테 전화해 줘"), "보호자분께 전화 연결해 드릴게요.")
        self.assertEqual((api.responded[0][1]["user_response"], c.action), (GUARDIAN_CALL, "call_guardian"))

    def test_둘_다_물었는데_네만_하면_다시_물음(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("네"), "119에 연결해 드릴까요? '119' 또는 '딸에게'처럼 말씀해 주세요.")
        self.assertEqual(api.responded, [])

    def test_보호자에게_못_갔으면_119에_직접_전화하도록(self):
        api = FakeAlertApi(create=[AlertResult(ACCEPTED, body={"alert_id": "al_1", "deliveries": []})])
        c = conversation([ev()], api=api)
        said = c.handle(FALL)
        self.assertIn("지금 119에 전화해 주세요", said)
        self.assertNotIn("보호자분께도 알려 드렸어요", said)
        self.assertEqual(c.awaiting.asked, ASKED_119)
        self.assertEqual(c.handle("네"), "119에 연결해 드릴게요.")  # 119만 물었으니 "네"는 119

    def test_B가_안_돼도_119_안내(self):
        api = FakeAlertApi(create=[AlertResult(UNAVAILABLE)])
        self.assertIn("119에 전화해 주세요", conversation([ev()], api=api).handle(FALL))

    def test_괜찮다고_하면_dismissed(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("괜찮아, 혼자 일어났어"), "알겠어요. 그래도 다시 아프시거나 이상하면 바로 말씀해 주세요.")
        self.assertEqual(api.responded[0][1]["user_response"], DISMISSED)  # 응급은 B가 severity를 그대로 둠

    def test_대답이_없으면_no_response(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        c.expire()
        self.assertEqual(api.responded[0][1]["user_response"], "no_response")  # B가 high로 올림

    def test_죽고_싶다는_말(self):
        api = FakeAlertApi()
        c = conversation([ev(category="self_harm")], api=api)
        said = c.handle("이제 그만 살고 싶어")
        self.assertIn("109에 전화하시면", said)
        self.assertIn("보호자분께도 알려 드렸어요", said)
        self.assertNotIn("119", said)
        self.assertEqual(c.awaiting.asked, ASKED_GUARDIAN)
        self.assertEqual(c.handle("응"), "보호자분께 전화 연결해 드릴게요.")

    def test_109를_고르면_연결하고_B에는_보내지_않음(self):
        # 명세 4에 109 값이 없어서 call_119_requested로 보내면 보호자가 구급차를 부른 줄 안다 (#26 리뷰)
        api = FakeAlertApi()
        c = conversation([ev(category="self_harm")], api=api)
        c.handle("다 귀찮고 죽고 싶다")
        self.assertEqual(c.handle("109 연결해 줘"), "109에 연결해 드릴게요.")
        self.assertEqual((api.responded, c.action, c.awaiting), ([], "call_109", None))
        self.assertEqual(c.events[0]["user_response"], HELPLINE_109)
        self.assertEqual(c.flush(), 0)  # 다시 보낼 것도 없음 (B에는 high/pending으로 남음)

    def test_지난_일은_알리지_않음(self):
        api = FakeAlertApi()
        c = conversation([ev(stage="past")], api=api)
        self.assertEqual(c.handle("어제 넘어졌었는데 이제 괜찮아"),
                         "그러셨군요. 지금은 괜찮으시다니 다행이에요. 혹시 다시 아프시거나 불편하면 바로 말씀해 주세요.")
        self.assertEqual(api.created, [])

    def test_같은_응급은_다시_알리지_않고_더_심해지면_알림(self):
        api = FakeAlertApi()
        c = conversation([ev(stage="mild"), ev(stage="mild"), ev(stage="acute")], api=api)
        c.handle("넘어졌는데 괜찮은 것 같아")
        c.respond(DISMISSED)
        self.assertEqual(c.handle("아직 좀 아파"), "아직 불편하시면 바로 도움을 받으세요. 119에 연결해 드릴까요?")
        c.handle("아까보다 더 아파서 못 일어나겠어")
        self.assertEqual([b["severity"] for b in api.created], [MEDIUM, HIGH])

    def test_응급_대답을_기다리는_중_사기가_나오면_따로_알림(self):
        api = FakeAlertApi()
        c = conversation([ev(), NOT_EMERGENCY], [verdict()], api=api)
        c.handle(FALL)
        said = c.handle("그런데 아들이 폰 고장 났다고 급하게 돈 보내 달래")
        self.assertTrue(said.startswith("잠깐만요. 가족이라며"))
        self.assertEqual([b["type"] for b in api.created], ["emergency", "fraud"])
        self.assertEqual(api.responded[0][1]["user_response"], "no_response")

    def test_응급을_먼저_본다(self):
        api = FakeAlertApi()
        c = conversation([ev()], [verdict()], api=api)
        c.handle("넘어져서 못 일어나겠는데 아들이 돈 보내 달래")
        self.assertEqual([b["type"] for b in api.created], ["emergency"])

    def test_응급이_아니면_사기를_봄(self):
        api = FakeAlertApi()
        c = conversation([], [verdict()], api=api)
        c.handle("아들이 폰 고장 났다고 급하게 돈 보내 달래")  # 응급 문지기를 못 넘음 → 사기 감지기로
        self.assertEqual([b["type"] for b in api.created], ["fraud"])


class ReviewFixTest(unittest.TestCase):
    """코드 리뷰 (#26)에서 찾은 문제."""

    def test_다시_물은_뒤의_네는_119(self):
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        c.handle("네")                                   # 둘 다 물어서 모름 → 119만 다시 물음
        self.assertEqual(c.handle("네"), "119에 연결해 드릴게요.")
        self.assertEqual((api.responded[0][1]["user_response"], c.action), (CALL_119, "call_119"))

    def test_다시_알림_뒤의_네도_119(self):
        api = FakeAlertApi()
        c = conversation([ev(), ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("아직도 넘어진 채로 있어"), "아직 불편하시면 바로 도움을 받으세요. 119에 연결해 드릴까요?")
        self.assertEqual(c.handle("응"), "119에 연결해 드릴게요.")

    def test_안_괜찮다는_말은_괜찮다가_아님(self):
        for text in ("하나도 안 괜찮아", "괜찮지 않아", "안괜찮아"):
            self.assertIsNone(parse_emergency_choice(text), text)
        api = FakeAlertApi()
        c = conversation([ev()], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("하나도 안 괜찮아"), "119에 연결해 드릴까요? '119' 또는 '딸에게'처럼 말씀해 주세요.")
        self.assertEqual(api.responded, [])

    def test_부르지_말라는_말은_요청이_아님(self):
        self.assertEqual(parse_emergency_choice("119는 부르지 마, 괜찮아"), DISMISSED)
        self.assertEqual(parse_emergency_choice("아들한테 연락하지 마"), DISMISSED)
        self.assertEqual(parse_emergency_choice("아들한테 연락하지 말고 119 불러"), CALL_119)
        self.assertEqual(parse_emergency_choice("119 말고 딸한테 전화해 줘"), GUARDIAN_CALL)

    def test_지난_응급이_있어도_같은_말의_사기를_봄(self):
        api = FakeAlertApi()
        c = conversation([ev(stage="past")], [verdict()], api=api)
        said = c.handle("어제 넘어졌었는데, 아들이라면서 돈 보내라고 문자 왔어")
        self.assertTrue(said.startswith("잠깐만요. 가족이라며"))
        self.assertEqual([b["type"] for b in api.created], ["fraud"])

    def test_119를_고른_대답으로_새_응급_알림을_만들지_않음(self):
        api = FakeAlertApi()
        c = conversation([ev(), ev(category="other")], api=api)
        c.handle(FALL)
        self.assertEqual(c.handle("119 불러 줘, 살려 줘"), "119에 연결해 드릴게요.")
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.responded[0][1]["user_response"], CALL_119)

    def test_짧은_말에_감지_응답이_있으면_그것을_말함(self):
        api = FakeAlertApi()
        c = conversation([ev(category="chest"), ev(stage="past")], api=api)
        c.handle("가슴이 너무 아파")
        self.assertEqual(c.handle("어제 넘어졌었어"),
                         "그러셨군요. 지금은 괜찮으시다니 다행이에요. 혹시 다시 아프시거나 불편하면 바로 말씀해 주세요.")

    def test_죽고_싶다는_말을_되풀이하면_109_안내(self):
        api = FakeAlertApi()
        c = conversation([ev(category="self_harm"), ev(category="self_harm")], api=api)
        c.handle("죽고 싶어")
        said = c.handle("정말 죽고 싶어")
        self.assertIn("109", said)
        self.assertNotIn("119", said)
        self.assertEqual(c.handle("응"), "보호자분께 전화 연결해 드릴게요.")

    def test_감지기가_없으면_만들_때_알림(self):
        with self.assertRaises(ValueError):
            SafetyConversation(user_id="u", conversation_id="c", api=FakeAlertApi())

    def test_응급_감지기를_항상_먼저(self):
        e, _ = emergency_detector()
        f = FraudDetector(FakeClassifier(), clock=lambda: NOW)
        c = SafetyConversation(user_id="u", conversation_id="c", api=FakeAlertApi(), detectors=[f, e])
        self.assertEqual([d.type for d in c.detectors], ["emergency", "fraud"])


class StrongSignalReviewTest(unittest.TestCase):
    def test_죽고_싶다는_말은_지난_일_낱말이_있어도(self):
        self.assertEqual(emergency_rules.strong_signal("어제부터 가슴이 너무 아파서 죽고 싶어"), ("self_harm", ACUTE))
        self.assertEqual(emergency_rules.strong_signal("예전에도 그랬지만 정말 죽고 싶어"), ("self_harm", ACUTE))

    def test_어제부터는_지금도_계속(self):
        self.assertEqual(emergency_rules.strong_signal("어제부터 가슴이 너무 아파"), ("chest", ACUTE))

    def test_자기_전에는_지난_일이_아님(self):
        self.assertEqual(emergency_rules.strong_signal("자기 전에 넘어져서 못 일어나겠어"), ("fall", ACUTE))


class ChoiceTest(unittest.TestCase):
    def test_대답_읽기(self):
        cases = {
            "119 불러 줘": CALL_119, "구급차 좀": CALL_119, "딸한테 전화하고 119도 불러": CALL_119,
            "아들한테 전화해 줘": GUARDIAN_CALL, "보호자한테 연락해 줘": GUARDIAN_CALL,
            "109 연결해 줘": HELPLINE_109, "상담 받고 싶어": HELPLINE_109,
            "괜찮아": DISMISSED, "안 불러도 돼": DISMISSED, "아니": DISMISSED,
            "네?": None, "뭐라고?": None,
        }
        for text, want in cases.items():
            self.assertEqual(parse_emergency_choice(text), want, text)

    def test_그냥_네는_물은_것에_따라(self):
        self.assertIsNone(parse_emergency_choice("네", ASKED_BOTH))
        self.assertEqual(parse_emergency_choice("네", ASKED_119), CALL_119)
        self.assertEqual(parse_emergency_choice("응", ASKED_GUARDIAN), GUARDIAN_CALL)


class EvalTest(unittest.TestCase):
    def test_정답대로_답하는_감지기면_100점(self):
        answers = {c["text"]: (NOT_EMERGENCY if c["expect"] == "none" else ev(category=c["category"], stage=c["stage"]))
                   for c in CASES}

        class Oracle:
            def classify(self, utterance, recent=()):
                return answers[utterance]

        report = evaluate(CASES, EmergencyDetector(Oracle(), clock=lambda: NOW))
        self.assertEqual((report["detection_rate"], report["false_positive_rate"], report["quiet_ok"],
                          report["category_accuracy"], report["misses"]), (1.0, 0.0, 1.0, 1.0, []))


if __name__ == "__main__":
    unittest.main()
