import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from companion.clock import KST
from companion.reports import batch, guard
from companion.reports.b_client import (
    ACCEPTED,
    ALREADY_SENT,
    REJECTED,
    UNAVAILABLE,
    ReportApi,
    SubmitResult,
)
from companion.reports.batch import (
    NO_CONVERSATION,
    SENT,
    WRITE_FAILED,
    Outcome,
    run_file,
    run_one,
)
from companion.reports.models import Conversation, DailyReport, DayLog, Turn
from companion.reports.writer import ClaudeReportWriter, ReportError, format_transcript

from tests.test_b_client import StubB, error

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "day_u_123.json"
NOW = dt.datetime(2026, 11, 24, 20, 30, tzinfo=KST)

GOOD = {
    "brief": "오늘도 편안하게 잘 지내셨어요.",
    "sections": {
        "schedule": "오후에 치과에 다녀오셨고, 아침에 혈압약도 챙겨 드셨어요.",
        "health": "무릎이 조금 쑤셔서 오래 걷기가 힘들다고 하셨어요.",
        "mood": "손주 민준이의 전화를 받고 기운이 난다며 기뻐하셨어요.",
    },
    "mood_level": "good",
}


def sample_day() -> DayLog:
    return DayLog.from_dict(json.loads(SAMPLE.read_text(encoding="utf-8")))


def reply(payload, stop_reason="end_turn", before=()):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(stop_reason=stop_reason, content=[*before, SimpleNamespace(type="text", text=text)])


class FakeBetaMessages:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def writer(*responses):
    messages = FakeBetaMessages(*responses)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeReportWriter(client=client, api_errors=(ConnectionError,)), messages


class ModelsTest(unittest.TestCase):
    def test_샘플_읽기(self):
        day = sample_day()
        self.assertEqual((day.user_id, day.date, len(day.conversations), day.senior_turn_count, day.completed_todos),
                         ("u_123", dt.date(2026, 11, 24), 3, 6, 2))

    def test_시간대_없는_시각은_거절(self):
        data = json.loads(SAMPLE.read_text(encoding="utf-8"))
        data["conversations"][0]["started_at"] = "2026-11-24T08:40:00"
        with self.assertRaises(ValueError):
            DayLog.from_dict(data)

    def test_명세_3_요청(self):
        day = sample_day()
        body = DailyReport(GOOD["brief"], GOOD["sections"], "good").to_request(
            request_id="rid-1", day=day, generated_at=NOW)
        self.assertEqual(body, {
            "request_id": "rid-1", "user_id": "u_123", "date": "2026-11-24",
            "brief": GOOD["brief"], "sections": GOOD["sections"], "mood_level": "good",
            "stats": {"conversation_count": 3, "completed_todos": 2},
            "generated_at": "2026-11-24T20:30:00+09:00",
        })

    def test_다른_날짜_대화는_뺌(self):
        data = json.loads(SAMPLE.read_text(encoding="utf-8"))
        data["conversations"][0]["started_at"] = "2026-11-23T23:50:00+09:00"
        self.assertEqual([c.conversation_id for c in DayLog.from_dict(data).conversations], ["c_102", "c_103"])
        # UTC로 와도 한국 날짜로 판단 (11-24 14:00 UTC = 11-24 23:00 KST)
        data["conversations"][0]["started_at"] = "2026-11-24T14:00:00+00:00"
        self.assertEqual(len(DayLog.from_dict(data).conversations), 3)

    def test_대화_횟수는_어르신이_말한_대화만(self):
        silent = Conversation("c_9", NOW, (Turn("ai", "좋은 저녁이에요", NOW),))
        day = sample_day()
        day = DayLog(day.user_id, day.date, (*day.conversations, silent), day.completed_todos)
        body = DailyReport("…").to_request(request_id="r", day=day, generated_at=NOW)
        self.assertEqual(body["stats"]["conversation_count"], 3)

    def test_끝낸_할_일_수를_모르면_빼기(self):
        day = DayLog("u_1", dt.date(2026, 11, 24), ())
        body = DailyReport("…").to_request(request_id="r", day=day, generated_at=NOW)
        self.assertEqual(body["stats"], {"conversation_count": 0})
        self.assertEqual(body["sections"], {"schedule": None, "health": None, "mood": None})


class GuardTest(unittest.TestCase):
    def test_brief(self):
        self.assertEqual(guard.brief_problems("오늘도 편안하게 잘 지내셨어요."), [])
        self.assertIn("기분", guard.brief_problems("기분 좋게 지내셨어요."))
        self.assertIn("병원", guard.brief_problems("병원에 다녀오셨어요."))
        self.assertTrue(guard.brief_problems("무릎이 아프셨어요."))
        self.assertEqual(guard.brief_problems(guard.SAFE_BRIEF), [])

    def test_항목끼리_섞임(self):
        self.assertEqual(guard.mixed_words("schedule", "오후에 치과에 다녀오셨고, 혈압약도 잘 챙겨 드셨어요."), [])
        self.assertEqual(guard.mixed_words("mood", "손주 소식을 듣고 기뻐하셨어요."), [])
        self.assertIn("치과", guard.mixed_words("mood", "치과 가기 귀찮아하셨어요."))
        self.assertIn("치과", guard.mixed_words("health", "치과 치료가 아프지 않았대요."))
        self.assertIn("기뻐", guard.mixed_words("schedule", "치과에 다녀오시고 기뻐하셨어요."))
        self.assertEqual(guard.mixed_words("health", "무릎이 쑤셔서 오래 걷기 힘들다고 하셨어요."), [])

    def test_흔한_낱말에_잘못_걸리지_않음(self):
        self.assertEqual(guard.brief_problems("스마트폰으로 이야기 나누셨어요."), [])
        self.assertEqual(guard.mixed_words("health", "기운이 없다고 하셨어요."), [])
        self.assertEqual(guard.mixed_words("health", "무릎이 걱정된다고 하셨어요."), [])
        self.assertEqual(guard.mixed_words("mood", "약간 처지셨어요."), [])
        self.assertIn("마트에", guard.mixed_words("mood", "마트에 다녀오시며 즐거워하셨어요."))

    def test_의료_표현(self):
        self.assertEqual(guard.medical_problems("같은 질문을 여러 번 하셨어요."), [])
        self.assertEqual(guard.medical_problems("치매 증상이 보이셨어요."), ["치매", "증상"])
        self.assertEqual(guard.medical_problems(None), [])


class WriterTest(unittest.TestCase):
    def test_요청_형식(self):
        w, messages = writer(reply(GOOD))
        report = w.write(sample_day())
        self.assertEqual((report.brief, report.sections, report.mood_level), (GOOD["brief"], GOOD["sections"], "good"))
        call = messages.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5-5")
        self.assertEqual((call["betas"], call["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(call["output_config"]["effort"], "medium")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertIn("08:40 어르신: 좋은 아침이에요. 혈압약 방금 먹었어요.", call["messages"][0]["content"])

    def test_규칙을_어기면_이유를_알려_주고_다시_쓰게_함(self):
        bad = {**GOOD, "brief": "오늘 기분 좋게 치과에 다녀오셨어요."}
        w, messages = writer(reply(bad), reply(GOOD))
        self.assertEqual(w.write(sample_day()).brief, GOOD["brief"])
        second = messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertIn("brief에 일정·건강·기분 내용 '기분'", second[2]["content"])
        self.assertIn("brief에 일정·건강·기분 내용 '치과'", second[2]["content"])

    def test_section에_다른_항목이_섞이면_다시_쓰고_그래도_섞이면_뺌(self):
        mixed = {**GOOD, "sections": {**GOOD["sections"],
                                      "mood": "혼자 치과 가기는 귀찮아하셨지만 손주 소식에 기뻐하셨어요."}}
        w, messages = writer(reply(mixed), reply(mixed))
        report = w.write(sample_day())
        self.assertIn("mood에 일정·건강 내용 '치과'", messages.calls[1]["messages"][2]["content"])
        self.assertIsNone(report.sections["mood"])                    # 기분만 공유한 보호자에게 일정이 새지 않게
        self.assertEqual(report.sections["schedule"], GOOD["sections"]["schedule"])

    def test_다시_써도_어기면_안전한_쪽으로(self):
        bad = {"brief": "병원에 다녀오셨어요.",
               "sections": {**GOOD["sections"], "health": "치매 증상이 조금 보이셨어요."}, "mood_level": "low"}
        w, _ = writer(reply(bad), reply(bad))
        report = w.write(sample_day())
        self.assertEqual(report.brief, guard.SAFE_BRIEF)
        self.assertIsNone(report.sections["health"])                  # 의료 표현 section은 뺌
        self.assertEqual(report.sections["mood"], GOOD["sections"]["mood"])
        self.assertEqual(report.mood_level, "low")

    def test_다시_쓰기가_실패해도_첫_결과를_고쳐서_씀(self):
        mixed = {**GOOD, "sections": {**GOOD["sections"], "mood": "치과 가기 귀찮아하셨어요."}}
        for second in (reply(GOOD, stop_reason="refusal"), ConnectionError("down"), reply("JSON 아님")):
            w, _ = writer(reply(mixed), second)
            report = w.write(sample_day())
            self.assertIsNone(report.sections["mood"])
            self.assertEqual(report.sections["schedule"], GOOD["sections"]["schedule"])

    def test_프로그래밍_오류는_그대로_터짐(self):
        w, _ = writer(TypeError("unexpected keyword 'fallbacks'"))
        with self.assertRaises(TypeError):
            w.write(sample_day())

    def test_대화_시각은_한국_시간으로(self):
        utc = dt.UTC
        at = dt.datetime(2026, 11, 24, 11, 10, tzinfo=utc)  # 20:10 KST
        day = DayLog("u_1", dt.date(2026, 11, 24), (Conversation("c_1", at, (Turn("senior", "안녕", at),)),))
        self.assertIn("20:10 어르신: 안녕", format_transcript(day))

    def test_빈_section은_None(self):
        w, _ = writer(reply({**GOOD, "sections": {"schedule": "  ", "health": None, "mood": "편안해하셨어요."}}))
        self.assertEqual(w.write(sample_day()).sections, {"schedule": None, "health": None, "mood": "편안해하셨어요."})

    def test_다른_모델이_이어_쓰면_마지막_글만(self):
        partial = SimpleNamespace(type="text", text='{"brief": "오늘')
        fallback = SimpleNamespace(type="fallback")
        w, _ = writer(reply(GOOD, before=(partial, fallback)))
        self.assertEqual(w.write(sample_day()).brief, GOOD["brief"])

    def test_실패는_ReportError(self):
        for r in (reply(GOOD, stop_reason="refusal"), reply(GOOD, stop_reason="max_tokens"),
                  reply("요약할 수 없어요"), reply({"brief": 1}), ConnectionError("down")):
            w, _ = writer(r)
            with self.assertRaises(ReportError):
                w.write(sample_day())

    def test_대화_원문_형식(self):
        text = format_transcript(sample_day())
        self.assertTrue(text.startswith("날짜: 2026-11-24\n대화 3번"))
        self.assertIn("[대화 3] 20:05 시작", text)
        self.assertIn("20:05 AI: 정말 기쁜 소식이네요!", text)


class FakeWriter:
    def __init__(self, result):
        self.result = result

    def write(self, day):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeApi:
    def __init__(self, result=None):
        self.result = result or SubmitResult(ACCEPTED, body={"report_id": "r_001", "shared_sections": [],
                                                              "deliveries": []})
        self.bodies = []

    def submit(self, body):
        self.bodies.append(body)
        return self.result


class BatchTest(unittest.TestCase):
    def run_one(self, day=None, writer_result=None, api=None):
        report = DailyReport(GOOD["brief"], GOOD["sections"], "good")
        return run_one(day or sample_day(), writer=FakeWriter(writer_result or report), api=api or FakeApi(),
                       clock=lambda: NOW, new_request_id=lambda: "rid-1")

    def test_보냄(self):
        api = FakeApi()
        outcome = self.run_one(api=api)
        self.assertEqual((outcome.status, outcome.response["report_id"]), (SENT, "r_001"))
        self.assertEqual(api.bodies[0]["request_id"], "rid-1")
        self.assertEqual(outcome.request, api.bodies[0])

    def test_대화가_없으면_보내지_않음(self):
        api = FakeApi()
        quiet = DayLog("u_1", dt.date(2026, 11, 24), (
            Conversation("c_1", NOW, (Turn("ai", "안녕하세요", NOW),)),))  # AI만 말함
        self.assertEqual(self.run_one(day=quiet, api=api).status, NO_CONVERSATION)
        self.assertEqual(api.bodies, [])

    def test_쓰기_실패(self):
        api = FakeApi()
        self.assertEqual(self.run_one(writer_result=ReportError("refusal"), api=api).status, WRITE_FAILED)
        self.assertEqual(api.bodies, [])

    def test_B_결과(self):
        for result, status in ((SubmitResult(ALREADY_SENT, error_code="ALREADY_SENT"), ALREADY_SENT),
                               (SubmitResult(REJECTED, error_code="INVALID_REQUEST"), REJECTED),
                               (SubmitResult(UNAVAILABLE), UNAVAILABLE)):
            self.assertEqual(self.run_one(api=FakeApi(result)).status, status)


class RerunTest(unittest.TestCase):
    """같은 날짜로 다시 돌릴 때."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_보낸_뒤_다시_돌리면_아무것도_안_함(self):
        sent = Outcome("u_123", "2026-11-24", SENT, request={"request_id": "rid-1", "brief": "보낸 것"})
        batch.save(sent, self.out)
        api, w = FakeApi(), mock.Mock()
        outcome = run_file(SAMPLE, writer=w, api=api, out_dir=self.out)
        self.assertEqual(outcome, sent)
        self.assertEqual((api.bodies, w.write.call_count), ([], 0))
        self.assertEqual(batch.load(self.out, "u_123", "2026-11-24"), sent)  # 기록이 지워지지 않음

    def test_B가_응답하지_않았으면_같은_본문을_다시_보냄(self):
        body = {"request_id": "rid-1", "brief": "처음 쓴 요약"}
        batch.save(Outcome("u_123", "2026-11-24", "unavailable", request=body), self.out)
        api, w = FakeApi(), mock.Mock()
        outcome = run_file(SAMPLE, writer=w, api=api, out_dir=self.out)
        self.assertEqual((outcome.status, api.bodies, w.write.call_count), (SENT, [body], 0))
        self.assertEqual(batch.load(self.out, "u_123", "2026-11-24").status, SENT)

    def test_쓰기_실패였으면_처음부터_다시(self):
        batch.save(Outcome("u_123", "2026-11-24", WRITE_FAILED), self.out)
        api = FakeApi()
        outcome = run_file(SAMPLE, writer=FakeWriter(DailyReport(GOOD["brief"], GOOD["sections"], "good")),
                           api=api, out_dir=self.out)
        self.assertEqual((outcome.status, len(api.bodies)), (SENT, 1))

    def test_입력_파일_하나가_깨져도_나머지는_계속(self):
        bad = self.out / "bad.json"
        bad.write_text("{", encoding="utf-8")
        report = DailyReport(GOOD["brief"], GOOD["sections"], "good")
        argv = ["batch", str(bad), str(SAMPLE), "--out", str(self.out / "rec")]
        with (mock.patch.object(batch, "ClaudeReportWriter", return_value=FakeWriter(report)),
              mock.patch.object(batch, "ReportApi", return_value=FakeApi()),
              mock.patch("sys.argv", argv), mock.patch("builtins.print"),
              mock.patch.object(batch.logging, "basicConfig")):
            code = batch.main()
        self.assertEqual(code, 1)  # 실패가 있었음을 종료 코드로 알림
        self.assertEqual(batch.load(self.out / "rec", "u_123", "2026-11-24").status, SENT)


class ReportApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = StubB(path="/guardian-reports/daily")

    @classmethod
    def tearDownClass(cls):
        cls.b.server.shutdown()

    def setUp(self):
        self.b.responses.clear()
        self.b.received.clear()
        self.slept = []
        self.api = ReportApi(self.b.url, sleep=self.slept.append)

    def test_접수(self):
        self.b.responses.extend([(503, error("X")), (202, {"report_id": "r_001"})])
        result = self.api.submit({"request_id": "rid-1"})
        self.assertEqual((result.status, result.body, self.slept), (ACCEPTED, {"report_id": "r_001"}, [1]))
        self.assertEqual([r["request_id"] for r in self.b.received], ["rid-1", "rid-1"])

    def test_이미_보냄(self):
        self.b.responses.append((409, error("ALREADY_SENT")))
        self.assertEqual(self.api.submit({"request_id": "rid"}).status, ALREADY_SENT)
        self.assertEqual(self.slept, [])


if __name__ == "__main__":
    unittest.main()
