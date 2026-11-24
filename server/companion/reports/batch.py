"""하루 요약 배치 (#29 생성, #31 B 전송).

사용자마다: 그날 대화 → 요약 쓰기 → B에 전달(명세 3) → 결과 저장.
대화 기록 저장(#18)이 생기기 전이라 하루치 대화를 JSON 파일로 받고, 결과도 파일로 남긴다.

    python tools/mocks/report_mock.py                       # B mock 먼저 (PR #66)
    cd server
    python -m companion.reports.batch samples/day_u_123.json --out ../.reports
    python -m companion.reports.batch a.json b.json --b-url http://127.0.0.1:8048

같은 날짜로 다시 돌려도 안전하다 (결과 파일이 그날의 기록):
- 이미 보냈으면(sent / already_sent) 아무것도 하지 않는다 → 보호자가 실제로 받은 내용의 기록이 지워지지 않음
- B가 응답하지 않았으면(unavailable) 요약을 새로 쓰지 않고 **같은 request_id로 같은 본문을 다시 보낸다**
  (B는 받았는데 응답만 끊긴 경우에도 중복으로 처리되지 않게, 명세 공통 규칙)
- 그 밖(쓰기 실패, 거절, 대화 없음)은 처음부터 다시 한다

정해진 시각에 돌리는 것(예: 보호자 요약 발송 시각 21:00 전)은 서버 스택(#1)이 정해지면 스케줄러에 붙인다.
ANTHROPIC_API_KEY(또는 ant auth login)가 필요하다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..clock import KST
from .b_client import ACCEPTED, ALREADY_SENT, UNAVAILABLE, ReportApi
from .models import DayLog
from .writer import DEFAULT_MODEL, ClaudeReportWriter, ReportError

log = logging.getLogger(__name__)

SENT = "sent"
NO_CONVERSATION = "no_conversation"
WRITE_FAILED = "write_failed"
DONE = (SENT, ALREADY_SENT)  # 이 상태면 다시 돌려도 아무것도 하지 않음


@dataclass(frozen=True)
class Outcome:
    user_id: str
    date: str
    status: str            # sent / no_conversation / write_failed / already_sent / rejected / user_not_found / unavailable
    request: dict | None = None
    response: dict | None = None
    error_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Outcome:
        return cls(**{k: data.get(k) for k in ("user_id", "date", "status", "request", "response", "error_code")})


def run_one(day: DayLog, *, writer, api: ReportApi, previous: Outcome | None = None,
            clock=lambda: dt.datetime.now(KST), new_request_id=lambda: str(uuid.uuid4())) -> Outcome:
    base = {"user_id": day.user_id, "date": day.date.isoformat()}

    if previous is not None and previous.status in DONE:
        log.info("이미 보낸 요약이라 건너뜀 (%s %s)", day.user_id, day.date)
        return previous
    if previous is not None and previous.status == UNAVAILABLE and previous.request:
        log.info("B가 응답하지 않았던 요약을 같은 request_id로 다시 보냄 (%s %s)", day.user_id, day.date)
        return _submit(api, base, previous.request)

    if day.senior_turn_count == 0:
        # 대화가 없는 날은 보내지 않는다 (임시). "오늘은 대화가 없었어요"를 보호자에게 알릴지는 회의에서 정함
        return Outcome(**base, status=NO_CONVERSATION)
    try:
        report = writer.write(day)
    except ReportError as e:
        log.warning("하루 요약 쓰기 실패 (%s %s): %s", day.user_id, day.date, e)
        return Outcome(**base, status=WRITE_FAILED)

    # request_id는 요약 하나에 하나. b_http의 재시도와, unavailable 뒤 다시 돌릴 때 모두 같은 값을 쓴다
    body = report.to_request(request_id=new_request_id(), day=day, generated_at=clock())
    return _submit(api, base, body)


def _submit(api: ReportApi, base: dict, body: dict) -> Outcome:
    result = api.submit(body)
    status = SENT if result.status == ACCEPTED else result.status
    if status == ALREADY_SENT:
        log.info("이미 보호자에게 보낸 요약이라 덮어쓰지 않음 (%s %s)", base["user_id"], base["date"])
    return Outcome(**base, status=status, request=body, response=result.body, error_code=result.error_code)


def record_path(out_dir: Path, user_id: str, date: str) -> Path:
    return out_dir / f"{user_id}_{date}.json"


def load(out_dir: Path, user_id: str, date: str) -> Outcome | None:
    path = record_path(out_dir, user_id, date)
    if not path.exists():
        return None
    return Outcome.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save(outcome: Outcome, out_dir: Path) -> Path:
    """결과를 파일로 남긴다. 요약 문장(민감정보)이 들어 있으니 저장소에 올리지 않는 곳에 둔다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = record_path(out_dir, outcome.user_id, outcome.date)
    path.write_text(json.dumps(asdict(outcome), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_file(path: Path, *, writer, api: ReportApi, out_dir: Path) -> Outcome:
    day = DayLog.from_dict(json.loads(path.read_text(encoding="utf-8")))
    previous = load(out_dir, day.user_id, day.date.isoformat())
    outcome = run_one(day, writer=writer, api=api, previous=previous)
    if outcome is not previous:  # 건너뛴 경우에는 파일을 건드리지 않음
        save(outcome, out_dir)
    return outcome


def main() -> int:
    ap = argparse.ArgumentParser(description="하루 요약 배치 (#29, #31)")
    ap.add_argument("inputs", nargs="+", type=Path, help="하루치 대화 JSON 파일 (samples/ 형식)")
    ap.add_argument("--b-url", default="http://127.0.0.1:8048", help="B 하루 요약 API 주소 (기본: report mock)")
    ap.add_argument("--out", type=Path, default=Path(".reports"), help="결과 저장 폴더 (그날의 기록)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    writer = ClaudeReportWriter(model=args.model)
    api = ReportApi(args.b_url)
    failed = 0
    for path in args.inputs:
        try:
            outcome = run_file(path, writer=writer, api=api, out_dir=args.out)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # 입력 파일 하나가 깨져도 다른 사용자는 계속 돌린다
            failed += 1
            print(f"{path}: 입력을 읽지 못함 ({type(e).__name__}: {e})")
            continue
        if outcome.status not in (*DONE, NO_CONVERSATION):
            failed += 1
        # 화면에는 결과 상태만 (요약 내용은 저장 파일에서 확인)
        print(f"{outcome.user_id} {outcome.date}: {outcome.status}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
