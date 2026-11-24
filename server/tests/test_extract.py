import datetime as dt
import json
import unittest
from types import SimpleNamespace

from companion.todos.extract import ClaudeTodoExtractor, ExtractionError, to_draft
from companion.todos.models import Repeat

TODAY = dt.date(2026, 11, 24)


def raw(**over):
    item = {"kind": "appointment", "title": "치과 예약", "date": "2026-11-25", "time": "15:30",
            "time_hint": None, "repeat_freq": None, "weekdays": [], "original_text": "내일 세 시 반 치과"}
    item.update(over)
    return item


class FakeMessages:
    def __init__(self, text="", stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error = text, stop_reason, error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=self.text)])


def extractor(**kw):
    messages = FakeMessages(**kw)
    ex = ClaudeTodoExtractor(client=SimpleNamespace(messages=messages), api_errors=(ConnectionError,))
    return ex, messages


class ToDraftTest(unittest.TestCase):
    def draft(self, **over):
        return to_draft(raw(**over), utterance="발화", today=TODAY, message_id="m_1")

    def test_정상(self):
        d = self.draft()
        self.assertEqual((d.title, d.date, d.time, d.time_hint, d.message_id),
                         ("치과 예약", dt.date(2026, 11, 25), dt.time(15, 30), None, "m_1"))

    def test_time과_hint가_둘_다면_time만(self):
        self.assertIsNone(self.draft(time_hint="evening").time_hint)

    def test_반복(self):
        d = self.draft(kind="medication", title="혈압약", date=None, time=None, time_hint="morning",
                       repeat_freq="daily")
        self.assertEqual((d.repeat, d.time_hint), (Repeat("daily"), "morning"))
        w = self.draft(date=None, repeat_freq="weekly", weekdays=["THU", "MON", "THU"])
        self.assertEqual(w.repeat, Repeat("weekly", ("MON", "THU")))

    def test_쓸_수_없는_것은_버림(self):
        self.assertIsNone(self.draft(title="  "))
        self.assertIsNone(self.draft(kind="party"))
        self.assertIsNone(self.draft(date="2026-11-23"))                      # 지난 날짜
        self.assertIsNone(self.draft(date=None))                              # 날짜도 반복도 없음
        self.assertIsNone(self.draft(date=None, repeat_freq="weekly"))        # 요일 없는 매주
        self.assertIsNone(self.draft(date="내일"))

    def test_이상한_시각은_없는_것으로(self):
        self.assertIsNone(self.draft(time="3시").time)
        self.assertIsNone(self.draft(time="25:00").time)

    def test_원문이_없으면_발화로(self):
        self.assertEqual(self.draft(original_text="").original_text, "발화")


class ExtractorTest(unittest.TestCase):
    def test_요청_형식(self):
        ex, messages = extractor(text=json.dumps({"todos": [raw()]}))
        drafts = ex.extract("내일 세 시 반 치과", TODAY, message_id="m_1", recent=("안녕하세요",))
        self.assertEqual([d.title for d in drafts], ["치과 예약"])
        call = messages.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["output_config"]["effort"], "low")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        content = call["messages"][0]["content"]
        self.assertIn("오늘: 2026-11-24 (화요일)", content)
        self.assertIn("안녕하세요", content)

    def test_할_일_없음(self):
        ex, _ = extractor(text='{"todos": []}')
        self.assertEqual(ex.extract("날씨 좋네요", TODAY), [])

    def test_거절이나_잘림은_빈_결과(self):
        for stop in ("refusal", "max_tokens"):
            ex, _ = extractor(text='{"todos": [', stop_reason=stop)
            self.assertEqual(ex.extract("…", TODAY), [])

    def test_JSON_아님(self):
        ex, _ = extractor(text="죄송해요")
        self.assertEqual(ex.extract("…", TODAY), [])

    def test_API_오류는_ExtractionError(self):
        ex, _ = extractor(error=ConnectionError("down"))
        with self.assertRaises(ExtractionError):
            ex.extract("…", TODAY)

    def test_프로그래밍_오류는_그대로_터짐(self):
        ex, _ = extractor(error=TypeError("unexpected keyword"))
        with self.assertRaises(TypeError):
            ex.extract("…", TODAY)


if __name__ == "__main__":
    unittest.main()
