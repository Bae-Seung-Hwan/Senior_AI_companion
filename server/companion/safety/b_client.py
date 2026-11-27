"""B 긴급 알림 API 클라이언트 (명세 4). 처음에는 B mock(tools/mocks/alert_mock.py, http://127.0.0.1:8049)에 붙인다.

감지는 대화 턴 안에서 일어나고 어르신이 경고를 기다리므로, 할 일 등록처럼 짧게 기다린다 (for_voice).
실패해도 경고는 바로 들려 드리고, 같은 request_id로 나중에 다시 보낸다 (flow.py SafetyConversation.flush).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..b_http import (
    RETRY_DELAYS,
    VOICE_RETRY_DELAYS,
    VOICE_TIMEOUT,
    BClient,
    Unavailable,
)

log = logging.getLogger(__name__)

ACCEPTED = "accepted"                    # 4-1 202 (같은 request_id 재요청도 202), 4-2 200
ALREADY_RESPONDED = "already_responded"  # 4-2 409: 이미 최종 선택이 기록됨
NOT_FOUND = "not_found"                  # 4-2 404: 없는 alert_id
USER_NOT_FOUND = "user_not_found"
REJECTED = "rejected"                    # 그 밖의 4xx
UNAVAILABLE = "unavailable"              # 재시도를 다 해도 5xx·시간 초과
# 다시 보내도 결과가 같은 실패. flush()가 다시 보내지 않는다
PERMANENT_FAILURES = (NOT_FOUND, USER_NOT_FOUND, REJECTED)


@dataclass(frozen=True)
class AlertResult:
    status: str
    body: dict | None = None  # alert_id, severity, deliveries
    error_code: str | None = None

    @property
    def alert_id(self) -> str | None:
        return (self.body or {}).get("alert_id")

    @property
    def reached_guardian(self) -> bool:
        """보호자에게 긴급 푸시가 갔거나 갈 예정인지. 보호자가 없거나 전부 실패했으면 False (명세 4-1)."""
        deliveries = (self.body or {}).get("deliveries") or []
        return any(isinstance(d, dict) and d.get("kind") == "alert" and d.get("status") in ("sent", "queued")
                   for d in deliveries)


class AlertApi:
    def __init__(self, base_url: str, *, timeout: float = 5.0, retry_delays=RETRY_DELAYS, sleep=time.sleep):
        self.http = BClient(base_url, timeout=timeout, retry_delays=retry_delays, sleep=sleep)

    @classmethod
    def for_voice(cls, base_url: str, **kwargs) -> AlertApi:
        """대화 턴 안에서 부를 때 (3초 × 2번 + 1초). 실패분은 flush()로 나중에 다시 보낸다."""
        return cls(base_url, timeout=VOICE_TIMEOUT, retry_delays=VOICE_RETRY_DELAYS, **kwargs)

    def create(self, body: dict) -> AlertResult:
        """4-1 감지 즉시 `POST /alerts`."""
        return self._call(self.http.post, "/alerts", body)

    def respond(self, alert_id: str, body: dict) -> AlertResult:
        """4-2 어르신의 선택 `PATCH /alerts/{alert_id}`."""
        return self._call(self.http.patch, f"/alerts/{alert_id}", body)

    @staticmethod
    def _call(send, path: str, body: dict) -> AlertResult:
        try:
            resp = send(path, body)
        except Unavailable:
            return AlertResult(UNAVAILABLE)
        if resp.ok:
            return AlertResult(ACCEPTED, body=resp.body)
        code = resp.error_code
        if resp.status == 409 and code == "ALREADY_RESPONDED":
            return AlertResult(ALREADY_RESPONDED, error_code=code)
        if resp.status == 404 and code == "USER_NOT_FOUND":
            return AlertResult(USER_NOT_FOUND, error_code=code)
        if resp.status == 404:
            return AlertResult(NOT_FOUND, error_code=code)
        # 그 밖의 4xx는 A 요청이 명세와 어긋났을 가능성이 커서 찾기 쉽게 warning (원문은 남기지 않음)
        log.warning("긴급 알림 거절: HTTP %d %s", resp.status, code)
        return AlertResult(REJECTED, error_code=code)
