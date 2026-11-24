import datetime as dt
import unittest
from dataclasses import replace

from companion.todos.b_client import (
    CREATED,
    DUPLICATE,
    REJECTED,
    UNAVAILABLE,
    RegisterResult,
)
from companion.todos.extract import ExtractionError
from companion.todos.flow import KST, TodoConversation
from companion.todos.models import Repeat, TodoDraft

NOW = dt.datetime(2026, 11, 24, 10, 0, tzinfo=KST)

DENTIST = TodoDraft("appointment", "치과 예약", "내일 오후 세 시 반에 치과 예약 있어요",
                    date=dt.date(2026, 11, 25), time=dt.time(15, 30), message_id="m_1")
PILL = TodoDraft("medication", "혈압약", "혈압약은 매일 아침 먹어야 해요",
                 time_hint="morning", repeat=Repeat("daily"), message_id="m_1")
BANK = TodoDraft("task", "은행 가기", "모레 은행 가야 해", date=dt.date(2026, 11, 26), message_id="m_1")


class FakeExtractor:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def extract(self, text, today, *, message_id=None, recent=()):
        self.calls.append(text)
        result = self.results.pop(0) if self.results else []
        if isinstance(result, Exception):
            raise result
        return result


class FakeApi:
    def __init__(self, *results):
        self.results = list(results)
        self.bodies = []

    def register(self, body):
        self.bodies.append(body)
        if self.results:
            return self.results.pop(0)
        return RegisterResult(CREATED, todo={"todo_id": "t_001", "title": body["title"],
                                             "next_at": "2026-11-25T15:30:00+09:00", "reminder": True})


def conversation(extractor, api=None):
    ids = iter(f"rid-{i}" for i in range(1, 100))
    return TodoConversation(user_id="u_123", conversation_id="c_456", extractor=extractor,
                            api=api or FakeApi(), clock=lambda: NOW, new_request_id=lambda: next(ids))


class FlowTest(unittest.TestCase):
    def test_확인_후_등록(self):
        api = FakeApi()
        c = conversation(FakeExtractor([DENTIST]), api)
        self.assertEqual(c.handle("내일 오후 세 시 반에 치과 예약 있어요", "m_1"),
                         "내일 오후 3시 반 치과 예약, 등록해 드릴까요?")
        self.assertEqual(api.bodies, [])  # 동의 전에는 호출하지 않음
        self.assertEqual(c.handle("네"), "내일 오후 3시 반 치과 예약, 등록해 두었어요.")
        self.assertEqual(api.bodies, [{
            "request_id": "rid-1", "user_id": "u_123", "kind": "appointment", "title": "치과 예약",
            "date": "2026-11-25", "time": "15:30", "time_hint": None, "repeat": None,
            "original_text": "내일 오후 세 시 반에 치과 예약 있어요",
            "source": {"conversation_id": "c_456", "message_id": "m_1"},
        }])

    def test_거절하면_등록하지_않음(self):
        api = FakeApi()
        c = conversation(FakeExtractor([DENTIST]), api)
        c.handle("…")
        self.assertEqual(c.handle("아니 됐어요"), "알겠어요, 등록하지 않을게요.")
        self.assertEqual(api.bodies, [])

    def test_할_일이_없으면_None(self):
        c = conversation(FakeExtractor([]))
        self.assertIsNone(c.handle("날씨가 좋네요"))

    def test_추출_오류면_None(self):
        c = conversation(FakeExtractor(ExtractionError("APIConnectionError")))
        self.assertIsNone(c.handle("내일 치과"))

    def test_시각을_모르면_한_번_묻기(self):
        api = FakeApi()
        c = conversation(FakeExtractor([BANK]), api)
        self.assertEqual(c.handle("모레 은행 가야 해"), "은행 가기는 몇 시로 알려 드릴까요?")
        self.assertEqual(c.handle("오전 열 시"), "모레 오전 10시 은행 가기, 등록해 드릴까요?")
        c.handle("응")
        self.assertEqual((api.bodies[0]["time"], api.bodies[0]["time_hint"]), ("10:00", None))

    def test_시각을_몰라도_시각_없이_등록(self):
        api = FakeApi(RegisterResult(CREATED, todo={"todo_id": "t_1", "title": "은행 가기",
                                                    "next_at": None, "reminder": False}))
        c = conversation(FakeExtractor([BANK]), api)
        c.handle("…")
        self.assertEqual(c.handle("아직 몰라요"), "모레 은행 가기, 등록해 드릴까요?")
        self.assertEqual(c.handle("네"), "은행 가기, 등록해 두었어요. 시간이 정해지면 말씀해 주세요.")
        self.assertEqual((api.bodies[0]["time"], api.bodies[0]["time_hint"]), (None, None))

    def test_대략적인_때로_대답(self):
        api = FakeApi()
        c = conversation(FakeExtractor([BANK]), api)
        c.handle("…")
        self.assertEqual(c.handle("점심 먹고"), "모레 점심 은행 가기, 등록해 드릴까요?")
        c.handle("네")
        self.assertEqual(api.bodies[0]["time_hint"], "noon")

    def test_여러_개면_하나씩(self):
        api = FakeApi()
        c = conversation(FakeExtractor([DENTIST, PILL]), api)
        self.assertEqual(c.handle("…"), "내일 오후 3시 반 치과 예약, 등록해 드릴까요?")
        self.assertEqual(c.handle("네"),
                         "내일 오후 3시 반 치과 예약, 등록해 두었어요. 매일 아침 혈압약, 등록해 드릴까요?")
        self.assertEqual(c.handle("아니요"), "알겠어요, 등록하지 않을게요.")
        self.assertEqual([b["title"] for b in api.bodies], ["치과 예약"])

    def test_알아듣지_못하면_한_번_더_묻고_그래도_모르면_접음(self):
        api = FakeApi()
        ex = FakeExtractor([DENTIST], [])
        c = conversation(ex, api)
        c.handle("…")
        self.assertEqual(c.handle("음..."), "등록해 드릴까요? 네, 아니요로 말씀해 주세요.")
        self.assertIsNone(c.handle("손주가 왔어요"))  # 다른 이야기 → 일반 대화로
        self.assertEqual(ex.calls[-1], "손주가 왔어요")
        self.assertEqual(api.bodies, [])

    def test_같은_대화에서_같은_할_일은_다시_제안하지_않음(self):
        c = conversation(FakeExtractor([DENTIST], [DENTIST]))
        c.handle("…")
        c.handle("아니요")
        self.assertIsNone(c.handle("치과 가기 싫다"))

    def test_B_결과별_멘트(self):
        cases = [
            (RegisterResult(DUPLICATE, error_code="DUPLICATE_TODO", existing_todo_id="t_1"),
             "치과 예약은 이미 등록돼 있어요."),
            (RegisterResult(REJECTED, error_code="INVALID_DATE"),
             "이미 지난 날짜나 시각이라 등록하지 못했어요. 언제인지 다시 말씀해 주세요."),
            (RegisterResult(REJECTED, error_code="INVALID_REQUEST"), "제가 잘 못 알아들었어요. 다시 말씀해 주세요."),
            (RegisterResult(UNAVAILABLE), "지금은 등록이 잘 안 되네요. 조금 있다가 다시 말씀해 주세요."),
        ]
        for result, expected in cases:
            c = conversation(FakeExtractor([DENTIST]), FakeApi(result))
            c.handle("…")
            self.assertEqual(c.handle("네"), expected)

    def test_반복_복약은_date_없이_time_hint로(self):
        api = FakeApi(RegisterResult(CREATED, todo={"todo_id": "t_2", "title": "혈압약",
                                                    "next_at": "2026-11-25T08:00:00+09:00", "reminder": True}))
        c = conversation(FakeExtractor([PILL]), api)
        self.assertEqual(c.handle("…"), "매일 아침 혈압약, 등록해 드릴까요?")
        self.assertEqual(c.handle("네 해 주세요"), "매일 아침 혈압약, 등록해 두었어요. 오전 8시에 알려 드릴게요.")
        body = api.bodies[0]
        self.assertEqual((body["date"], body["time"], body["time_hint"], body["repeat"]),
                         (None, None, "morning", {"freq": "daily"}))


if __name__ == "__main__":
    unittest.main()


class ReviewFixTest(unittest.TestCase):
    """코드 리뷰에서 찾은 문제 (#22)."""

    def test_등록에_실패하면_다시_말할_때_다시_제안(self):
        api = FakeApi(RegisterResult(UNAVAILABLE))
        c = conversation(FakeExtractor([DENTIST], [DENTIST]), api)
        c.handle("…")
        self.assertEqual(c.handle("네"), "지금은 등록이 잘 안 되네요. 조금 있다가 다시 말씀해 주세요.")
        self.assertEqual(c.handle("내일 세 시 반 치과 예약 있어요"), "내일 오후 3시 반 치과 예약, 등록해 드릴까요?")
        self.assertEqual(c.handle("네"), "내일 오후 3시 반 치과 예약, 등록해 두었어요.")
        self.assertEqual(len(api.bodies), 2)
        self.assertNotEqual(api.bodies[0]["request_id"], api.bodies[1]["request_id"])  # 새 등록 시도

    def test_날짜가_지나서_실패하면_고친_날짜로_다시_제안(self):
        fixed = replace(DENTIST, date=dt.date(2026, 11, 26))
        c = conversation(FakeExtractor([DENTIST], [fixed]),
                         FakeApi(RegisterResult(REJECTED, error_code="INVALID_DATE")))
        c.handle("…")
        c.handle("네")
        self.assertEqual(c.handle("모레요"), "모레 오후 3시 반 치과 예약, 등록해 드릴까요?")

    def test_제목이_같아도_날짜가_다르면_다른_할_일(self):
        api = FakeApi()
        next_week = replace(DENTIST, date=dt.date(2026, 12, 1))
        c = conversation(FakeExtractor([DENTIST], [next_week]), api)
        c.handle("…")
        c.handle("네")
        self.assertEqual(c.handle("다음 주 화요일에도 치과 예약 있어"),
                         "12월 1일 화요일 오후 3시 반 치과 예약, 등록해 드릴까요?")

    def test_등록한_할_일은_다시_말해도_제안하지_않음(self):
        c = conversation(FakeExtractor([BANK], [BANK]))
        c.handle("모레 은행 가야 해")
        c.handle("오전 열 시")
        c.handle("네")
        self.assertIsNone(c.handle("모레 은행 가는 거 잊으면 안 돼"))  # 시각을 채우기 전 모양으로 다시 나와도

    def test_확인_중에_말한_새_할_일은_뒤에_묻기(self):
        api = FakeApi()
        c = conversation(FakeExtractor([DENTIST], [BANK]), api)
        c.handle("…")
        self.assertEqual(c.handle("아 그리고 모레 은행 가야 해"), "등록해 드릴까요? 네, 아니요로 말씀해 주세요.")
        self.assertEqual(c.handle("네"),
                         "내일 오후 3시 반 치과 예약, 등록해 두었어요. 은행 가기는 몇 시로 알려 드릴까요?")

    def test_두_번_못_알아들어도_새_할_일은_남김(self):
        c = conversation(FakeExtractor([DENTIST], [], [BANK]))
        c.handle("…")
        c.handle("음...")
        self.assertEqual(c.handle("모레 은행 가야 해"), "은행 가기는 몇 시로 알려 드릴까요?")

    def test_거절한_뒤_그래도는_동의로_보지_않음(self):
        api = FakeApi()
        c = conversation(FakeExtractor([DENTIST]), api)
        c.handle("…")
        self.assertEqual(c.handle("그래도 안 할래요"), "알겠어요, 등록하지 않을게요.")
        self.assertEqual(api.bodies, [])
