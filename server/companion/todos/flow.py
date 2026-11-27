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

from ..clock import KST
from . import speech
from .b_client import CREATED, DUPLICATE, REJECTED, REPLAYED, USER_NOT_FOUND, TodoApi
from .extract import ExtractionError
from .models import TodoDraft
from .parse import NO, UNKNOWN_TIME, YES, parse_time_answer, parse_yes_no

log = logging.getLogger(__name__)

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
        self.current_origin_key: tuple | None = None  # 시각을 묻기 전, 처음 뽑았을 때의 key
        self.stage: str | None = None
        self.unclear_count = 0
        # 끝난 할 일(등록함, 이미 있음, 어르신이 거절함). 같은 대화에서 다시 제안하지 않는다.
        # 등록에 실패한 것은 넣지 않는다 → "다시 말씀해 주세요" 뒤에 다시 말하면 다시 제안
        self.settled: set[tuple] = set()
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
        self._collect(text, message_id)
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
        collected = False
        fixed = self._corrected(text, answer)
        if fixed is not None:
            # "아니, 아침 여섯 시" → 시각만 바로잡은 것. 같은 말에 다른 할 일이 없으면 고친 모양으로 다시 확인
            collected = True
            if self._collect(text, message_id, skip_title=self.current.title) == 0:
                self.current = fixed
                self.unclear_count = 0
                return speech.confirm_question(fixed, self.today)
        if answer == YES:
            reply = self._register(self.current)
            nxt = self._next()
            return f"{reply} {nxt}" if nxt else reply
        if answer == NO:
            self._settle()
            nxt = self._next()
            return f"{speech.declined()} {nxt}" if nxt else speech.declined()
        # 네/아니요가 아님 ("네?"처럼 되물은 것 포함). 그 말에 새 할 일이 있을 수 있으니 뽑아서 뒤에 줄 세운다
        if not collected:
            self._collect(text, message_id)
        if self.unclear_count == 0:
            self.unclear_count = 1
            return speech.ask_again(self.current, self.today)
        # 두 번 알아듣지 못하면 이 할 일은 등록하지 않고 접는다 (끝난 것으로 치지 않음)
        return self._next()

    # --- 도우미 ---

    def _corrected(self, text: str, answer: str | None) -> TodoDraft | None:
        """확인 질문에 시각을 바로잡아 대답했으면 고친 할 일을, 아니면 None.

        1~6시를 오후로 읽는 규칙은 여기서 바로잡을 수 있어야 안전하다 ("아니, 아침 여섯 시").
        "네, 저녁 혈압약 맞아요"처럼 제목에 든 때 낱말을 정정으로 보지 않으려고, 동의했으면 정확한 시각만 본다.
        """
        parsed = parse_time_answer(text)
        draft = self.current
        if isinstance(parsed, dt.time):
            if parsed != draft.time:
                return replace(draft, time=parsed, time_hint=None)
        elif parsed not in (None, UNKNOWN_TIME) and answer != YES:
            if draft.time is not None or parsed != draft.time_hint:
                return replace(draft, time=None, time_hint=parsed)
        return None

    def _collect(self, text: str, message_id: str | None, skip_title: str | None = None) -> int:
        """발화에서 할 일을 뽑아 대기열에 넣고, 넣은 개수를 돌려준다. 끝났거나 이미 기다리는 것은 뺀다.

        skip_title: 시각을 바로잡는 말이면 추출기가 같은 할 일을 다시 뽑을 수 있어서, 그 제목은 넣지 않는다.
        """
        try:
            drafts = self.extractor.extract(text, self.today, message_id=message_id, recent=tuple(self.recent))
        except ExtractionError:
            return 0  # 할 일 제안만 건너뛰고 대화는 계속
        waiting = {d.key for d in self.queue} | {self.current_origin_key}
        if self.current is not None:
            waiting.add(self.current.key)
        added = 0
        for draft in drafts:
            if draft.title == skip_title:
                continue
            if draft.key not in self.settled and draft.key not in waiting:
                waiting.add(draft.key)
                self.queue.append(draft)
                added += 1
        return added

    def _settle(self):
        """지금 할 일을 끝난 것으로 기록 (처음 뽑은 모양과 시각을 채운 모양 둘 다)."""
        self.settled.add(self.current.key)
        self.settled.add(self.current_origin_key)

    def _next(self) -> str | None:
        """대기 중인 다음 할 일을 꺼내 질문한다."""
        self._clear()
        if not self.queue:
            return None
        self.current = self.queue.popleft()
        self.current_origin_key = self.current.key
        if self.current.needs_time:
            self.stage = ASK_TIME
            return speech.ask_time_question(self.current)
        self.stage = CONFIRM
        return speech.confirm_question(self.current, self.today)

    def _clear(self):
        self.current = None
        self.current_origin_key = None
        self.stage = None
        self.unclear_count = 0

    def _register(self, draft: TodoDraft) -> str:
        body = draft.to_request(
            request_id=self.new_request_id(), user_id=self.user_id, conversation_id=self.conversation_id
        )
        result = self.api.register(body)
        if result.status in (CREATED, REPLAYED):
            self._settle()
            return speech.registered(draft, self.today, result.todo.get("next_at"))
        if result.status == DUPLICATE:
            self._settle()
            return speech.already_registered(draft)
        if result.status == REJECTED:  # 로그는 b_client가 남김 (A 요청 버그일 가능성이 커서 warning)
            return speech.invalid_date() if result.error_code == "INVALID_DATE" else speech.try_again()
        if result.status == USER_NOT_FOUND:
            log.error("할 일 등록: 사용자 없음 (%s)", self.user_id)
        return speech.unavailable()
