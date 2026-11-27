"""B 할 일 API 클라이언트 (명세 1 `POST /todos`).

재시도·에러 본문 처리는 공통 모듈(companion/b_http.py)이 한다.
처음에는 B mock(tools/mocks/todo_mock.py, http://127.0.0.1:8045)에 붙인다.
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

# 음성 대화 중에는 짧게 기다린다 (b_http.VOICE_TIMEOUT).
# 실패하면 "조금 있다가 다시 말씀해 주세요"로 안내한다 (백그라운드 재시도는 음성 연결 때 추가)

CREATED = "created"            # 201 새로 등록
REPLAYED = "replayed"          # 200 같은 request_id 재요청 (처음 결과)
DUPLICATE = "duplicate"        # 409 DUPLICATE_TODO
REJECTED = "rejected"          # 그 밖의 4xx
USER_NOT_FOUND = "user_not_found"
UNAVAILABLE = "unavailable"    # 재시도를 다 해도 5xx·시간 초과


@dataclass(frozen=True)
class RegisterResult:
    status: str
    todo: dict | None = None            # 성공 응답 본문 (todo_id, title, next_at, reminder)
    error_code: str | None = None
    existing_todo_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (CREATED, REPLAYED)


class TodoApi:
    def __init__(self, base_url: str, *, timeout: float = 5.0, retry_delays=RETRY_DELAYS, sleep=time.sleep):
        self.http = BClient(base_url, timeout=timeout, retry_delays=retry_delays, sleep=sleep)

    @property
    def timeout(self) -> float:
        return self.http.timeout

    @classmethod
    def for_voice(cls, base_url: str, **kwargs) -> TodoApi:
        """음성 대화 턴 안에서 부를 때. 기다리는 시간을 몇 초로 줄인다."""
        return cls(base_url, timeout=VOICE_TIMEOUT, retry_delays=VOICE_RETRY_DELAYS, **kwargs)

    def register(self, body: dict) -> RegisterResult:
        """할 일 등록. body의 request_id는 재시도 내내 같은 값이어야 한다."""
        try:
            resp = self.http.post("/todos", body)
        except Unavailable:
            return RegisterResult(UNAVAILABLE)
        if resp.ok:
            return RegisterResult(CREATED if resp.status == 201 else REPLAYED, todo=resp.body)
        code = resp.error_code
        if resp.status == 409 and code == "DUPLICATE_TODO":
            return RegisterResult(DUPLICATE, error_code=code, existing_todo_id=resp.error.get("existing_todo_id"))
        if resp.status == 404 and code == "USER_NOT_FOUND":
            return RegisterResult(USER_NOT_FOUND, error_code=code)
        # 그 밖의 4xx는 A 요청이 명세와 어긋났을 가능성이 커서 찾기 쉽게 warning (message·원문은 남기지 않음)
        log.warning("할 일 등록 거절: HTTP %d %s", resp.status, code)
        return RegisterResult(REJECTED, error_code=code)
