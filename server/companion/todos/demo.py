"""말로 할 일 등록 흐름을 터미널에서 글자로 해 보는 데모.

음성(STT·TTS) 대신 키보드로 입력하고 화면으로 읽는다. B mock을 먼저 띄워 둔다.

    python tools/mocks/todo_mock.py                        # B mock (PR #62)
    cd server
    python -m companion.todos.demo                         # http://127.0.0.1:8045 로 등록
    python -m companion.todos.demo --b-url http://127.0.0.1:9000 --user u_123

ANTHROPIC_API_KEY(또는 ant auth login)가 필요하다. 끝내려면 빈 줄 또는 Ctrl+C.
"""

from __future__ import annotations

import argparse
import uuid

from .b_client import TodoApi
from .extract import DEFAULT_MODEL, ClaudeTodoExtractor
from .flow import TodoConversation


def main():
    ap = argparse.ArgumentParser(description="말로 할 일 등록 데모 (#22)")
    ap.add_argument("--b-url", default="http://127.0.0.1:8045", help="B 할 일 API 주소 (기본: todo mock)")
    ap.add_argument("--user", default="u_123")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    conversation = TodoConversation(
        user_id=args.user,
        conversation_id=f"c_demo_{uuid.uuid4().hex[:6]}",
        extractor=ClaudeTodoExtractor(model=args.model),
        api=TodoApi.for_voice(args.b_url),
    )
    print("어르신 역할로 말해 보세요. (예: 내일 오후 세 시 반에 치과 예약 있어요)")
    n = 0
    try:
        while True:
            text = input("\n어르신> ").strip()
            if not text:
                break
            n += 1
            reply = conversation.handle(text, message_id=f"m_{n}")
            print(f"AI> {reply}" if reply else "AI> (할 일 없음 → 일반 대화가 답함)")
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
