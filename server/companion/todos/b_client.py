"""B 할 일 API 클라이언트 (명세 1 `POST /todos`).

공통 규칙: 5xx·시간 초과는 같은 request_id로 최대 3번 재시도 (1초, 3초, 10초 간격), 4xx는 재시도하지 않음.
처음에는 B mock(tools/mocks/todo_mock.py, http://127.0.0.1:8045)에 붙인다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

RETRY_DELAYS = (1, 3, 10)  # 공통 규칙
# 음성 대화 중에는 어르신이 기다리므로 짧게: 3초 × 2번 + 1초 ≈ 최대 7초.
# 실패하면 "조금 있다가 다시 말씀해 주세요"로 안내한다 (백그라운드 재시도는 음성 연결 때 추가)
VOICE_TIMEOUT = 3.0
VOICE_RETRY_DELAYS = (1,)

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


class _Retryable(Exception):
    pass


class TodoApi:
    def __init__(self, base_url: str, *, timeout: float = 5.0, retry_delays=RETRY_DELAYS, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_delays = tuple(retry_delays)
        self.sleep = sleep

    @classmethod
    def for_voice(cls, base_url: str, **kwargs) -> TodoApi:
        """음성 대화 턴 안에서 부를 때. 기다리는 시간을 몇 초로 줄인다."""
        return cls(base_url, timeout=VOICE_TIMEOUT, retry_delays=VOICE_RETRY_DELAYS, **kwargs)

    def register(self, body: dict) -> RegisterResult:
        """할 일 등록. body의 request_id는 재시도 내내 같은 값이어야 한다."""
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                self.sleep(self.retry_delays[attempt - 1])
            try:
                return self._post(body)
            except _Retryable as e:
                log.warning("할 일 등록 재시도 대상 (%d번째 시도): %s", attempt + 1, e)
        return RegisterResult(UNAVAILABLE)

    def _post(self, body: dict) -> RegisterResult:
        req = urllib.request.Request(
            f"{self.base_url}/todos",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if not isinstance(payload, dict):  # 같은 request_id라 다시 보내도 안전
                    raise _Retryable("응답이 JSON 객체가 아님")
                return RegisterResult(CREATED if resp.status == 201 else REPLAYED, todo=payload)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise _Retryable(f"HTTP {e.code}") from None
            return self._client_error(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise _Retryable(type(e).__name__) from None
        except ValueError:  # 2xx인데 JSON이 아님. 같은 request_id라 다시 보내도 안전
            raise _Retryable("응답이 JSON이 아님") from None

    @staticmethod
    def _client_error(e: urllib.error.HTTPError) -> RegisterResult:
        try:
            err = json.loads(e.read().decode("utf-8"))["error"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            err = {}
        if not isinstance(err, dict):  # {"error": "bad request"} 같은 형식
            err = {}
        code = err.get("code")
        # message·원문은 남기지 않음
        if e.code == 409 and code == "DUPLICATE_TODO":
            log.info("할 일 등록: 이미 있음 (HTTP %d %s)", e.code, code)
            return RegisterResult(DUPLICATE, error_code=code, existing_todo_id=err.get("existing_todo_id"))
        if e.code == 404 and code == "USER_NOT_FOUND":
            log.info("할 일 등록: 사용자 없음 (HTTP %d %s)", e.code, code)
            return RegisterResult(USER_NOT_FOUND, error_code=code)
        # 그 밖의 4xx는 A 요청이 명세와 어긋났을 가능성이 커서 찾기 쉽게 warning
        log.warning("할 일 등록 거절: HTTP %d %s", e.code, code)
        return RegisterResult(REJECTED, error_code=code)
