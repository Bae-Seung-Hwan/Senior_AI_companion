# A 서버 (말벗 AI)

> 기술 스택(#1)이 정해지기 전이라 **Python 3.10+** 로 기능 로직만 먼저 만듭니다. HTTP 서버·앱 연결은 스택이 정해지면 붙입니다.
> B 연결은 [연결 지점 명세](../docs/api-contracts.md)를 따르고, B 실제 서버 전에는 B mock(`tools/mocks/`)에 붙입니다.

## 말로 할 일 등록 (#22, 명세 1)

```
어르신: 내일 오후 세 시 반에 치과 예약 있어요
AI:     내일 오후 3시 반 치과 예약, 등록해 드릴까요?
어르신: 네
AI:     내일 오후 3시 반 치과 예약, 등록해 두었어요.      ← 이때만 B POST /todos 호출
```

| 파일 | 하는 일 |
| --- | --- |
| [`companion/todos/flow.py`](companion/todos/flow.py) | 대화 하나의 흐름. 할 일 찾기 → (시각을 모르면 한 번 묻기) → 확인 → **동의했을 때만** 등록 |
| [`companion/todos/extract.py`](companion/todos/extract.py) | 발화에서 할 일·복약 추출. Claude Haiku 5.5 + 구조화 출력(JSON 스키마) |
| [`companion/todos/parse.py`](companion/todos/parse.py) | 짧은 대답 읽기: "네/아니요", "세 시 반", "아침에", "몰라요" (LLM 없이 규칙으로) |
| [`companion/todos/speech.py`](companion/todos/speech.py) | 어르신에게 들려줄 말 (TTS용 문장) |
| [`companion/todos/b_client.py`](companion/todos/b_client.py) | B `POST /todos` 호출. 같은 `request_id`로 5xx·시간 초과 3번 재시도, `409 DUPLICATE_TODO` 처리 |
| [`companion/todos/models.py`](companion/todos/models.py) | 할 일 초안 → 명세 1 요청 본문 |

### 지키는 규칙

- **동의한 뒤에만 B를 호출합니다.** "네"가 아니면 등록하지 않습니다. 두 번 알아듣지 못하면 접고 일반 대화로 넘깁니다
- **시각을 추측하지 않습니다.** "아침에"는 `time_hint: "morning"`으로 보내고 B가 사용자별 기본 시각으로 바꿉니다. 등록 뒤에는 B가 정한 실제 시각을 알려 드립니다 ("오전 8시에 알려 드릴게요")
- 오전·오후를 말하지 않은 1~6시는 오후로 보고, 확인 질문에서 "오후 3시"로 다시 읽어 드려 바로잡을 수 있게 합니다
- 시각도 대략적인 때도 없으면 "몇 시로 알려 드릴까요?"를 **한 번만** 묻고, 모르면 시각 없이 등록합니다 (B는 알림 없이 저장)
- 같은 대화에서 같은 할 일을 두 번 제안하지 않습니다. 한 번에 여러 개를 말하면 하나씩 확인합니다
- 대화 원문(`original_text`)은 로그에 남기지 않습니다

### 실행 (글자로 해 보는 데모)

```bash
python tools/mocks/todo_mock.py          # B mock 먼저 (PR #62)
cd server
pip install -r requirements.txt          # anthropic SDK
python -m companion.todos.demo           # ANTHROPIC_API_KEY 필요
```

### 테스트

```bash
python -m unittest discover -s server/tests -t server
```

테스트는 Claude를 부르지 않습니다 (가짜 client). B 호출은 테스트 안에서 띄운 가짜 HTTP 서버로 확인합니다.

### 남은 일

- 음성 대화(STT·TTS, 대화 LLM)에 붙이기: 추출이 1~3초 걸리므로 **대화 답변과 동시에** 돌리고, 할 일이 나오면 답변 뒤에 확인 질문을 붙이는 방식 권장
- 추출 정확도 검증용 문장 모음 (사투리, 숫자 표현, 할 일이 아닌 말)
- 사용자별 호칭·기본 시각은 명세 7(`GET /users/{user_id}`)이 정해지면 반영
