"""B API 공통 HTTP 호출 (연결 지점 명세 "공통 규칙").

- 5xx·시간 초과·연결 실패는 **같은 본문(같은 request_id)** 으로 재시도한다 (기본 1초, 3초, 10초 간격)
- 4xx는 재시도하지 않고 에러 코드를 돌려준다
- 대화 원문 같은 민감정보가 섞일 수 있어서 요청·응답 본문은 로그에 남기지 않는다

표준 라이브러리만 쓴다.
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
# 대화 턴 안에서 부를 때 (할 일 등록, 긴급 알림): 어르신이 기다리므로 3초 × 2번 + 1초 ≈ 최대 7초
VOICE_TIMEOUT = 3.0
VOICE_RETRY_DELAYS = (1,)


@dataclass(frozen=True)
class Response:
    status: int
    body: dict  # 2xx면 응답 본문, 4xx면 {"error": {...}} (형식이 이상하면 {})

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> dict:
        err = self.body.get("error") if not self.ok else None
        return err if isinstance(err, dict) else {}

    @property
    def error_code(self) -> str | None:
        return self.error.get("code")


class Unavailable(Exception):
    """재시도를 다 해도 5xx·시간 초과·연결 실패."""


class _Retryable(Exception):
    pass


class BClient:
    def __init__(self, base_url: str, *, timeout: float = 5.0, retry_delays=RETRY_DELAYS, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_delays = tuple(retry_delays)
        self.sleep = sleep

    def post(self, path: str, body: dict) -> Response:
        """JSON POST. 재시도를 다 해도 안 되면 Unavailable."""
        return self._send("POST", path, body)

    def patch(self, path: str, body: dict) -> Response:
        """JSON PATCH (명세 4-2). 본문에 request_id가 있어서 다시 보내도 안전하다."""
        return self._send("PATCH", path, body)

    def _send(self, method: str, path: str, body: dict) -> Response:
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                self.sleep(self.retry_delays[attempt - 1])
            try:
                return self._send_once(method, path, body)
            except _Retryable as e:
                log.warning("B %s 재시도 대상 (%d번째 시도): %s", path, attempt + 1, e)
        raise Unavailable(path)

    def _send_once(self, method: str, path: str, body: dict) -> Response:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if not isinstance(payload, dict):  # 같은 request_id라 다시 보내도 안전
                    raise _Retryable("응답이 JSON 객체가 아님")
                return Response(resp.status, payload)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise _Retryable(f"HTTP {e.code}") from None
            response = Response(e.code, _read_error_body(e))
            log.info("B %s 거절: HTTP %d %s", path, e.code, response.error_code)  # message·원문은 남기지 않음
            return response
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise _Retryable(type(e).__name__) from None
        except ValueError:  # 2xx인데 JSON이 아님. 같은 request_id라 다시 보내도 안전
            raise _Retryable("응답이 JSON이 아님") from None


def _read_error_body(e: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(e.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}
