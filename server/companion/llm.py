"""Claude 구조화 출력(JSON 스키마) 한 번 호출. 할 일 추출과 사기 판단이 같이 쓴다.

둘 다 **대화 턴 안에서** 돌기 때문에 SDK 기본값(시간 초과 10분, 재시도 2번)을 그대로 쓰면 API가 느릴 때
어르신이 한참 아무 말도 못 듣는다. 그래서 기본 client는 짧게 기다리게 만든다 (B 호출의 for_voice와 같은 생각).
대화 원문이 섞이므로 요청·응답 내용은 로그에 남기지 않는다.
"""

from __future__ import annotations

import json

# 대화 턴 안에서 기다릴 최대 시간: 8초 × (1 + 재시도 1번). 넘으면 각자 정한 방식으로 넘어간다
VOICE_LLM_TIMEOUT = 8.0
VOICE_LLM_MAX_RETRIES = 1


class LLMCallError(Exception):
    """API 오류 (연결, 시간 초과, 5xx 등). 메시지는 예외 이름뿐이다."""


class LLMOutputError(Exception):
    """응답은 왔지만 쓸 수 없음 (거절, 잘림, JSON이 아님)."""


def voice_client(client=None, api_errors=None):
    """(client, api_errors). 테스트에서는 가짜 client와 api_errors를 넣어서 SDK 없이도 돈다."""
    if client is None or api_errors is None:
        import anthropic

        client = client or anthropic.Anthropic(timeout=VOICE_LLM_TIMEOUT, max_retries=VOICE_LLM_MAX_RETRIES)
        api_errors = api_errors or (anthropic.APIError,)
    # API 오류만 잡고, 인자 이름이 틀린 TypeError 같은 프로그래밍 오류는 그대로 터뜨려 원인이 보이게
    return client, tuple(api_errors)


def call_json(client, api_errors, *, model: str, system: str, content: str, schema: dict, effort: str,
              max_tokens: int = 2048) -> dict:
    """JSON 객체 하나를 돌려준다. API 오류는 LLMCallError, 쓸 수 없는 응답은 LLMOutputError."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except api_errors as e:
        raise LLMCallError(type(e).__name__) from e
    if response.stop_reason in ("refusal", "max_tokens"):
        raise LLMOutputError(f"stop_reason={response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        raise LLMOutputError("결과가 JSON 객체가 아님")
    return raw
