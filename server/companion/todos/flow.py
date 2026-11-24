"""말로 할 일 등록 흐름 (#22).

대화 하나에 TodoConversation 하나를 만든다. 어르신이 말할 때마다 handle()을 부르면:

1. 할 일 확인 중이 아니면 발화에서 할 일을 뽑는다 (extract.py)
2. 시각을 모르면 "몇 시로 알려 드릴까요?"라고 한 번 묻는다 (명세 1)
3. "내일 오후 3시 반 치과 예약, 등록해 드릴까요?"라고 확인한다
4. **"네"라고 했을 때만** B에 등록하고 결과를 말한다 (명세 1: 동의한 뒤에만 호출)

handle()이 None을 돌려주면 할 일과 상관없는 말이므로 일반 대화가 답하면 된다.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import deque
from dataclasses import replace

from . import speech
from .b_client import CREATED, DUPLICATE, REJECTED, REPLAYED, USER_NOT_FOUND, TodoApi
from .extract import ExtractionError
from .models import TodoDraft
from .parse import NO, UNKNOWN_TIME, YES, parse_time_answer, parse_yes_no

log = logging.getLogger(__name__)

KST = dt.timezone(dt.timedelta(hours=9))

ASK_TIME = "ask_time"
CONFIRM = "confirm"


class TodoConversation:
    def __init__(
        self,
        *,
        user_id: str,
        conversation_id: str,
        extractor,
        api: TodoApi,
        clock=lambda: dt.datetime.now(KST),
        new_request_id=lambda: str(uuid.uuid4()),
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.extractor = extractor
        self.api = api
        self.clock = clock
        self.new_request_id = new_request_id

        self.queue: deque[TodoDraft] = deque()
        self.current: TodoDraft | None = None
        self.stage: str | None = None
        self.unclear_count = 0
        self.proposed: set[str] = set()  # 같은 대화에서 같은 할 일을 다시 제안하지 않음
        self.recent: deque[str] = deque(maxlen=4)

    @property
    def today(self) -> dt.date:
        return self.clock().date()

    def handle(self, text: str, message_id: str | None = None) -> str | None:
        try:
            if self.stage == CONFIRM:
                return self._on_confirm_answer(text, message_id)
            if self.stage == ASK_TIME:
                return self._on_time_answer(text)
            return self._on_utterance(text, message_id)
        finally:
            self.recent.append(text)

    # --- 단계별 처리 ---

    def _on_utterance(self, text: str, message_id: str | None) -> str | None:
        try:
            drafts = self.extractor.extract(text, self.today, message_id=message_id, recent=tuple(self.recent))
        except ExtractionError:
            return None  # 할 일 제안만 건너뛰고 대화는 계속
        for draft in drafts:
            key = draft.title
            if key not in self.proposed:
                self.proposed.add(key)
                self.queue.append(draft)
        return self._next()

    def _on_time_answer(self, text: str) -> str:
        answer = parse_time_answer(text)
        draft = self.current
        if isinstance(answer, dt.time):
            draft = replace(draft, time=answer, time_hint=None)
        elif answer not in (None, UNKNOWN_TIME):
            draft = replace(draft, time_hint=answer)
        # 몰라요·못 알아들음 → 한 번만 물었으니 시각 없이 확인으로 넘어감 (B는 알림 없이 저장)
        self.current = draft
        self.stage = CONFIRM
        return speech.confirm_question(draft, self.today)

    def _on_confirm_answer(self, text: str, message_id: str | None) -> str | None:
        answer = parse_yes_no(text)
        if answer == YES:
            reply = self._register(self.current)
            nxt = self._next()
            return f"{reply} {nxt}" if nxt else reply
        if answer == NO:
            self._clear()
            nxt = self._next()
            return f"{speech.declined()} {nxt}" if nxt else speech.declined()
        if self.unclear_count == 0:
            self.unclear_count = 1
            return speech.ask_again()
        # 두 번 알아듣지 못하면 등록하지 않고 접는다. 다른 이야기를 하신 것일 수 있으니 일반 발화로 처리
        self._clear()
        self.queue.clear()
        return self._on_utterance(text, message_id)

    # --- 도우미 ---

    def _next(self) -> str | None:
        """대기 중인 다음 할 일을 꺼내 질문한다."""
        self._clear()
        if not self.queue:
            return None
        self.current = self.queue.popleft()
        if self.current.needs_time:
            self.stage = ASK_TIME
            return speech.ask_time_question(self.current)
        self.stage = CONFIRM
        return speech.confirm_question(self.current, self.today)

    def _clear(self):
        self.current = None
        self.stage = None
        self.unclear_count = 0

    def _register(self, draft: TodoDraft) -> str:
        body = draft.to_request(
            request_id=self.new_request_id(), user_id=self.user_id, conversation_id=self.conversation_id
        )
        result = self.api.register(body)
        if result.status in (CREATED, REPLAYED):
            return speech.registered(draft, self.today, result.todo.get("next_at"))
        if result.status == DUPLICATE:
            return speech.already_registered(draft)
        if result.status == REJECTED:
            log.info("할 일 등록 거절: %s", result.error_code)
            return speech.invalid_date() if result.error_code == "INVALID_DATE" else speech.try_again()
        if result.status == USER_NOT_FOUND:
            log.error("할 일 등록: 사용자 없음 (%s)", self.user_id)
        return speech.unavailable()
