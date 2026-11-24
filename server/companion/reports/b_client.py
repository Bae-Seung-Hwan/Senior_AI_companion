"""B 하루 요약 API 클라이언트 (명세 3 `POST /guardian-reports/daily`).

배치에서 부르므로 공통 규칙 그대로 재시도한다 (1초, 3초, 10초). 처음에는 B mock(tools/mocks/report_mock.py, 8048).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..b_http import RETRY_DELAYS, BClient, Unavailable

ACCEPTED = "accepted"            # 202 (같은 request_id 재요청도 202)
ALREADY_SENT = "already_sent"    # 409 ALREADY_SENT: 이미 보호자에게 보냄 → 덮어쓰지 않음
REJECTED = "rejected"            # 그 밖의 4xx
USER_NOT_FOUND = "user_not_found"
UNAVAILABLE = "unavailable"      # 재시도를 다 해도 5xx·시간 초과


@dataclass(frozen=True)
class SubmitResult:
    status: str
    body: dict | None = None   # 202 응답 (report_id, shared_sections, deliveries)
    error_code: str | None = None


class ReportApi:
    def __init__(self, base_url: str, *, timeout: float = 10.0, retry_delays=RETRY_DELAYS, sleep=time.sleep):
        self.http = BClient(base_url, timeout=timeout, retry_delays=retry_delays, sleep=sleep)

    def submit(self, body: dict) -> SubmitResult:
        try:
            resp = self.http.post("/guardian-reports/daily", body)
        except Unavailable:
            return SubmitResult(UNAVAILABLE)
        if resp.ok:
            return SubmitResult(ACCEPTED, body=resp.body)
        code = resp.error_code
        if resp.status == 409 and code == "ALREADY_SENT":
            return SubmitResult(ALREADY_SENT, error_code=code)
        if resp.status == 404 and code == "USER_NOT_FOUND":
            return SubmitResult(USER_NOT_FOUND, error_code=code)
        return SubmitResult(REJECTED, error_code=code)
