"""하루 요약 배치 (#29 생성, #31 B 전송).

사용자마다: 그날 대화 → 요약 쓰기 → B에 전달(명세 3) → 결과 저장.
대화 기록 저장(#18)이 생기기 전이라 하루치 대화를 JSON 파일로 받고, 결과도 파일로 남긴다.

    python tools/mocks/report_mock.py                       # B mock 먼저 (PR #66)
    cd server
    python -m companion.reports.batch samples/day_u_123.json --out ../.reports
    python -m companion.reports.batch a.json b.json --b-url http://127.0.0.1:8048

정해진 시각에 돌리는 것(예: 보호자 요약 발송 시각 21:00 전)은 서버 스택(#1)이 정해지면 스케줄러에 붙인다.
ANTHROPIC_API_KEY(또는 ant auth login)가 필요하다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..clock import KST
from .b_client import (
    ACCEPTED,
    ALREADY_SENT,
    REJECTED,
    UNAVAILABLE,
    USER_NOT_FOUND,
    ReportApi,
)
from .models import DayLog
from .writer import DEFAULT_MODEL, ClaudeReportWriter, ReportError

log = logging.getLogger(__name__)

SENT = "sent"
NO_CONVERSATION = "no_conversation"
WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class Outcome:
    user_id: str
    date: str
    status: str            # sent / no_conversation / write_failed / already_sent / rejected / user_not_found / unavailable
    request: dict | None = None
    response: dict | None = None
    error_code: str | None = None


def run_one(day: DayLog, *, writer, api: ReportApi, clock=lambda: dt.datetime.now(KST),
            new_request_id=lambda: str(uuid.uuid4())) -> Outcome:
    base = {"user_id": day.user_id, "date": day.date.isoformat()}
    if day.senior_turn_count == 0:
        # 대화가 없는 날은 보내지 않는다 (임시). "오늘은 대화가 없었어요"를 보호자에게 알릴지는 회의에서 정함
        return Outcome(**base, status=NO_CONVERSATION)
    try:
        report = writer.write(day)
    except ReportError as e:
        log.warning("하루 요약 쓰기 실패 (%s %s): %s", day.user_id, day.date, e)
        return Outcome(**base, status=WRITE_FAILED)

    # request_id는 이번 실행에서 한 번 만들고 재시도 내내 같은 값 (b_http가 재시도)
    body = report.to_request(request_id=new_request_id(), day=day, generated_at=clock())
    result = api.submit(body)
    status = {ACCEPTED: SENT, ALREADY_SENT: ALREADY_SENT, REJECTED: REJECTED,
              USER_NOT_FOUND: USER_NOT_FOUND, UNAVAILABLE: UNAVAILABLE}[result.status]
    if status == ALREADY_SENT:
        log.info("이미 보호자에게 보낸 요약이라 덮어쓰지 않음 (%s %s)", day.user_id, day.date)
    return Outcome(**base, status=status, request=body, response=result.body, error_code=result.error_code)


def save(outcome: Outcome, out_dir: Path) -> Path:
    """결과를 파일로 남긴다. 요약 문장(민감정보)이 들어 있으니 저장소에 올리지 않는 곳에 둔다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{outcome.user_id}_{outcome.date}.json"
    path.write_text(json.dumps(asdict(outcome), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="하루 요약 배치 (#29, #31)")
    ap.add_argument("inputs", nargs="+", type=Path, help="하루치 대화 JSON 파일 (samples/ 형식)")
    ap.add_argument("--b-url", default="http://127.0.0.1:8048", help="B 하루 요약 API 주소 (기본: report mock)")
    ap.add_argument("--out", type=Path, default=Path(".reports"), help="결과 저장 폴더")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    writer = ClaudeReportWriter(model=args.model)
    api = ReportApi(args.b_url)
    for path in args.inputs:
        day = DayLog.from_dict(json.loads(path.read_text(encoding="utf-8")))
        outcome = run_one(day, writer=writer, api=api)
        saved = save(outcome, args.out)
        # 화면에는 결과 상태만 (요약 내용은 저장 파일에서 확인)
        print(f"{outcome.user_id} {outcome.date}: {outcome.status} → {saved}")


if __name__ == "__main__":
    main()
