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
    USER_NOT_FOUND,
    ReportApi,
    SubmitResult,
)
from companion.reports.batch import (
    NO_CONVERSATION,
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
    def test_brief는_대화_횟수로만(self):
        self.assertEqual(guard.template_brief(1), "오늘 AI와 한 번 이야기를 나누셨어요.")
        self.assertEqual(guard.template_brief(3), "오늘 AI와 세 번 이야기를 나누셨어요.")
        self.assertEqual(guard.template_brief(12), "오늘 AI와 12번 이야기를 나누셨어요.")
        for n in range(0, 15):  # 어떤 횟수여도 다른 항목 내용·의료 표현이 없음
            text = guard.template_brief(n)
            for name in ("schedule", "health", "mood"):
                self.assertEqual(guard.mixed_words(name, text), [], (n, name))
            self.assertEqual(guard.medical_problems(text), [], n)

    def test_리뷰에서_찾은_섞임(self):
        # (칸, 문장): 그 칸에 다른 항목 내용이 있어서 걸려야 함 (#73 리뷰)
        cases = (
            ("schedule", "경로당에 다녀오셨어요. 피곤하다고 하셨어요."),
            ("mood", "잠을 못 주무셔서 처지셨어요."),
            ("mood", "감기 기운에도 밝게 웃으셨어요."),
            ("mood", "경로당에서 친구분들과 즐거우셨어요."),
            ("mood", "식사를 거르셨지만 밝으셨어요."),
            ("mood", "혈압이 높다며 속상해하셨어요."),
            ("health", "산책하고 오셔서 다리가 부었다고 하셨어요."),
            ("health", "외로우셔서 잠이 안 온다고 하셨어요."),
            ("schedule", "약국에 다녀오시고 눈이 침침하다고 하셨어요."),
        )
        for name, text in cases:
            self.assertTrue(guard.mixed_words(name, text), (name, text))

    def test_항목끼리_섞임(self):
        self.assertEqual(guard.mixed_words("schedule", "오후에 치과에 다녀오셨고, 혈압약도 잘 챙겨 드셨어요."), [])
        self.assertEqual(guard.mixed_words("mood", "손주 소식을 듣고 기뻐하셨어요."), [])
        self.assertIn("치과", guard.mixed_words("mood", "치과 가기 귀찮아하셨어요."))
        self.assertIn("치과", guard.mixed_words("health", "치과 치료가 아프지 않았대요."))
        self.assertIn("기뻐", guard.mixed_words("schedule", "치과에 다녀오시고 기뻐하셨어요."))
        self.assertEqual(guard.mixed_words("health", "무릎이 쑤셔서 오래 걷기 힘들다고 하셨어요."), [])

    def test_흔한_낱말에_잘못_걸리지_않음(self):
        self.assertEqual(guard.mixed_words("mood", "스마트폰으로 이야기 나누셨어요."), [])
        self.assertEqual(guard.mixed_words("schedule", "저녁에 AI와 대화가 길어졌어요."), [])   # "화가"
        self.assertEqual(guard.mixed_words("schedule", "AI와 대화나 하자며 웃음을 보이셨어요."), [])  # "화나"
        self.assertEqual(guard.mixed_words("mood", "마음이 약해지셨는지 손주 이야기를 자주 하셨어요."), [])  # "약"
        self.assertEqual(guard.mixed_words("schedule", "붓글씨 모임에 가셨어요."), [])  # "붓"
        self.assertEqual(guard.mixed_words("schedule", "주무시기 전에 혈압약을 챙겨 드셨어요."), [])
        self.assertIn("화가 나", guard.mixed_words("schedule", "은행 일로 화가 나셨어요."))
        self.assertEqual(guard.mixed_words("health", "기운이 없다고 하셨어요."), [])
        self.assertEqual(guard.mixed_words("health", "무릎이 걱정된다고 하셨어요."), [])
        self.assertEqual(guard.mixed_words("mood", "약간 처지셨어요."), [])
        self.assertIn("마트에", guard.mixed_words("mood", "마트에 다녀오시며 즐거워하셨어요."))

    def test_의료_표현(self):
        self.assertEqual(guard.medical_problems("같은 질문을 여러 번 하셨어요."), [])
        self.assertEqual(guard.medical_problems("치매 증상이 보이셨어요."), ["치매", "증상"])
        self.assertEqual(guard.medical_problems(None), [])
        for text in ("건망증이 심해지셨어요.", "기억력이 나빠지셨어요.", "인지 기능이 떨어지신 것 같아요.",
                     "고혈압 때문에 짜게 드시지 않아요.", "당뇨가 있으셔서 단 것을 피하셨어요.",
                     "관절염으로 무릎이 쑤셨어요.", "치료가 필요해 보여요.", "병원에 가 보셔야 할 것 같아요.",
                     "검사를 받아 보시면 좋겠어요."):
            self.assertTrue(guard.medical_problems(text), text)


class WriterTest(unittest.TestCase):
    def test_요청_형식(self):
        w, messages = writer(reply(GOOD))
        report = w.write(sample_day())
        self.assertEqual((report.brief, report.sections, report.mood_level),
                         ("오늘 AI와 세 번 이야기를 나누셨어요.", GOOD["sections"], "good"))
        call = messages.calls[0]
        self.assertNotIn("brief", call["output_config"]["format"]["schema"]["properties"])  # LLM은 brief를 쓰지 않음
        self.assertEqual(call["model"], "claude-sonnet-5-5")
        self.assertEqual((call["betas"], call["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(call["output_config"]["effort"], "medium")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertIn("08:40 어르신: 좋은 아침이에요. 혈압약 방금 먹었어요.", call["messages"][0]["content"])

    def test_규칙을_어기면_이유를_알려_주고_다시_쓰게_함(self):
        bad = {**GOOD, "sections": {**GOOD["sections"], "health": "치과에 다녀와서 건망증이 걱정된다고 하셨어요."}}
        w, messages = writer(reply(bad), reply(GOOD))
        self.assertEqual(w.write(sample_day()).sections, GOOD["sections"])
        second = messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertIn("health에 일정·기분 내용 '치과'", second[2]["content"])
        self.assertIn("health에 의료 표현 '건망증'", second[2]["content"])

    def test_LLM이_brief를_보내도_쓰지_않음(self):
        w, _ = writer(reply({**GOOD, "brief": "병원에 다녀오셔서 기분이 좋으셨어요."}))
        self.assertEqual(w.write(sample_day()).brief, "오늘 AI와 세 번 이야기를 나누셨어요.")

    def test_답_없는_AI_인사는_횟수에_세지_않음(self):
        day = sample_day()
        at = dt.datetime(2026, 11, 24, 21, 0, tzinfo=KST)
        day = DayLog(day.user_id, day.date,
                     (*day.conversations, Conversation("c_x", at, (Turn("ai", "주무세요", at),))))
        self.assertTrue(format_transcript(day).startswith("날짜: 2026-11-24\n대화 3번"))
        w, _ = writer(reply(GOOD))
        report = w.write(day)
        self.assertEqual(report.brief, "오늘 AI와 세 번 이야기를 나누셨어요.")
        body = report.to_request(request_id="r", day=day, generated_at=NOW)
        self.assertEqual(body["stats"]["conversation_count"], 3)

    def test_section에_다른_항목이_섞이면_다시_쓰고_그래도_섞이면_뺌(self):
        mixed = {**GOOD, "sections": {**GOOD["sections"],
                                      "mood": "혼자 치과 가기는 귀찮아하셨지만 손주 소식에 기뻐하셨어요."}}
        w, messages = writer(reply(mixed), reply(mixed))
        report = w.write(sample_day())
        self.assertIn("mood에 일정·건강 내용 '치과'", messages.calls[1]["messages"][2]["content"])
        self.assertIsNone(report.sections["mood"])                    # 기분만 공유한 보호자에게 일정이 새지 않게
        self.assertEqual(report.sections["schedule"], GOOD["sections"]["schedule"])

    def test_다시_써도_어기면_안전한_쪽으로(self):
        bad = {"sections": {**GOOD["sections"], "health": "치매 증상이 조금 보이셨어요."}, "mood_level": "low"}
        w, _ = writer(reply(bad), reply(bad))
        report = w.write(sample_day())
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
        partial = SimpleNamespace(type="text", text='{"sections": {"schedule": "오늘')
        fallback = SimpleNamespace(type="fallback")
        w, _ = writer(reply(GOOD, before=(partial, fallback)))
        self.assertEqual(w.write(sample_day()).sections, GOOD["sections"])

    def test_실패는_ReportError(self):
        for r in (reply(GOOD, stop_reason="refusal"), reply(GOOD, stop_reason="max_tokens"),
                  reply("요약할 수 없어요"), reply({"sections": 1}), ConnectionError("down")):
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
        self.assertEqual((outcome.status, outcome.response["report_id"]), (ACCEPTED, "r_001"))
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

    def test_이미_보냈으면_이번_본문은_기록하지_않음(self):
        outcome = self.run_one(api=FakeApi(SubmitResult(ALREADY_SENT, error_code="ALREADY_SENT")))
        self.assertIsNone(outcome.request)  # 보호자가 받은 것이 아니므로


class RerunTest(unittest.TestCase):
    """같은 날짜로 다시 돌릴 때."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_보낸_뒤_다시_돌리면_아무것도_안_함(self):
        sent = Outcome("u_123", "2026-11-24", ACCEPTED, request={"request_id": "rid-1", "brief": "보낸 것"})
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
        self.assertEqual((outcome.status, api.bodies, w.write.call_count), (ACCEPTED, [body], 0))
        self.assertEqual(batch.load(self.out, "u_123", "2026-11-24").status, ACCEPTED)

    def test_사용자_없음_거절이면_Claude를_다시_부르지_않고_같은_본문(self):
        body = {"request_id": "rid-1", "brief": "처음 쓴 요약"}
        for status in (USER_NOT_FOUND, REJECTED):
            batch.save(Outcome("u_123", "2026-11-24", status, request=body), self.out)
            api, w = FakeApi(), mock.Mock()
            outcome = run_file(SAMPLE, writer=w, api=api, out_dir=self.out)
            self.assertEqual((outcome.status, api.bodies, w.write.call_count), (ACCEPTED, [body], 0), status)

    def test_예전_기록의_sent도_보낸_것으로(self):
        old = Outcome("u_123", "2026-11-24", "sent", request={"request_id": "rid-1"})
        batch.save(old, self.out)
        api, w = FakeApi(), mock.Mock()
        self.assertEqual(run_file(SAMPLE, writer=w, api=api, out_dir=self.out), old)
        self.assertEqual((api.bodies, w.write.call_count), ([], 0))

    def test_쓰기_실패였으면_처음부터_다시(self):
        batch.save(Outcome("u_123", "2026-11-24", WRITE_FAILED), self.out)
        api = FakeApi()
        outcome = run_file(SAMPLE, writer=FakeWriter(DailyReport(GOOD["brief"], GOOD["sections"], "good")),
                           api=api, out_dir=self.out)
        self.assertEqual((outcome.status, len(api.bodies)), (ACCEPTED, 1))

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
        self.assertEqual(batch.load(self.out / "rec", "u_123", "2026-11-24").status, ACCEPTED)


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
