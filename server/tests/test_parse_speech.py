import datetime as dt
import unittest

from companion.todos import speech
from companion.todos.models import Repeat, TodoDraft
from companion.todos.parse import NO, UNKNOWN_TIME, YES, parse_time_answer, parse_yes_no

TODAY = dt.date(2026, 11, 24)  # 화요일


class YesNoTest(unittest.TestCase):
    def test_yes(self):
        for text in ("네", "네, 해 주세요", "응", "어 그래", "예.", "좋아요", "등록해 줘", "그렇게 해 주세요", "부탁해요"):
            self.assertEqual(parse_yes_no(text), YES, text)

    def test_no(self):
        for text in ("아니요", "아니 됐어", "괜찮아요", "하지 마", "필요 없어요", "나중에 할게", "싫어"):
            self.assertEqual(parse_yes_no(text), NO, text)

    def test_unclear(self):
        for text in ("", "음...", "어제 병원 갔다 왔어", "손주가 왔어요"):
            self.assertIsNone(parse_yes_no(text), text)


class TimeAnswerTest(unittest.TestCase):
    def test_exact(self):
        cases = {
            "세 시 반": dt.time(15, 30),       # 오전·오후 없는 1~6시 → 오후
            "오후 3시": dt.time(15, 0),
            "오전 10시": dt.time(10, 0),
            "10시": dt.time(10, 0),
            "아침 8시": dt.time(8, 0),          # 정확한 시각이 대략적인 때보다 먼저
            "저녁 일곱 시": dt.time(19, 0),
            "열두 시": dt.time(12, 0),
            "밤 12시": dt.time(0, 0),
            "두 시 십오 분": dt.time(14, 15),
            "9시 45분에 해 줘": dt.time(9, 45),
            "오후 네 시 삼십 분": dt.time(16, 30),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_time_answer(text), expected, text)

    def test_hint(self):
        self.assertEqual(parse_time_answer("아침에요"), "morning")
        self.assertEqual(parse_time_answer("점심 먹고"), "noon")
        self.assertEqual(parse_time_answer("저녁에"), "evening")
        self.assertEqual(parse_time_answer("자기 전에 먹어"), "bedtime")

    def test_unknown_and_unclear(self):
        self.assertEqual(parse_time_answer("아직 몰라요"), UNKNOWN_TIME)
        self.assertIsNone(parse_time_answer("치과는 무서워"))
        self.assertIsNone(parse_time_answer("30시"))


class SpeechTest(unittest.TestCase):
    def test_describe(self):
        d = TodoDraft("appointment", "치과 예약", "…", date=dt.date(2026, 11, 25), time=dt.time(15, 30))
        self.assertEqual(speech.confirm_question(d, TODAY), "내일 오후 3시 반 치과 예약, 등록해 드릴까요?")
        m = TodoDraft("medication", "혈압약", "…", time_hint="morning", repeat=Repeat("daily"))
        self.assertEqual(speech.describe(m, TODAY), "매일 아침 혈압약")
        w = TodoDraft("task", "수영", "…", time=dt.time(9, 0), repeat=Repeat("weekly", ("MON", "THU")))
        self.assertEqual(speech.describe(w, TODAY), "매주 월·목요일 오전 9시 수영")
        far = TodoDraft("task", "관리비 내기", "…", date=dt.date(2026, 11, 27))
        self.assertEqual(speech.describe(far, TODAY), "11월 27일 금요일 관리비 내기")

    def test_time_phrase(self):
        self.assertEqual(speech.time_phrase(dt.time(0, 0)), "밤 12시")
        self.assertEqual(speech.time_phrase(dt.time(12, 0)), "낮 12시")
        self.assertEqual(speech.time_phrase(dt.time(9, 5)), "오전 9시 5분")

    def test_topic_particle(self):
        self.assertEqual(speech.topic("치과 예약"), "치과 예약은")
        self.assertEqual(speech.topic("관리비 내기"), "관리비 내기는")
        self.assertEqual(speech.ask_time_question(TodoDraft("task", "은행", "…", date=TODAY)),
                         "은행은 몇 시로 알려 드릴까요?")

    def test_registered_with_hint_says_actual_time(self):
        m = TodoDraft("medication", "혈압약", "…", time_hint="morning", repeat=Repeat("daily"))
        self.assertEqual(speech.registered(m, TODAY, "2026-11-25T08:00:00+09:00"),
                         "매일 아침 혈압약, 등록해 두었어요. 오전 8시에 알려 드릴게요.")
        self.assertEqual(speech.registered(m, TODAY, None),
                         "혈압약, 등록해 두었어요. 시간이 정해지면 말씀해 주세요.")


if __name__ == "__main__":
    unittest.main()
