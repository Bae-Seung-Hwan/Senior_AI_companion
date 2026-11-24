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
| [`companion/todos/b_client.py`](companion/todos/b_client.py) | B `POST /todos` 호출. 같은 `request_id`로 5xx·시간 초과 재시도, `409 DUPLICATE_TODO` 처리. 음성 대화용(`TodoApi.for_voice`)은 짧게 기다림 (아래) |
| [`companion/todos/models.py`](companion/todos/models.py) | 할 일 초안 → 명세 1 요청 본문 |

### 지키는 규칙

- **동의한 뒤에만 B를 호출합니다.** "네"가 아니면 등록하지 않습니다. 두 번 알아듣지 못하면 접고 일반 대화로 넘깁니다
- **시각을 추측하지 않습니다.** "아침에"는 `time_hint: "morning"`으로 보내고 B가 사용자별 기본 시각으로 바꿉니다. 등록 뒤에는 B가 정한 실제 시각을 알려 드립니다 ("오전 8시에 알려 드릴게요")
- 오전·오후를 말하지 않은 1~6시는 오후로 보고, 확인 질문에서 "오후 3시"로 다시 읽어 드려 바로잡을 수 있게 합니다
- 시각도 대략적인 때도 없으면 "몇 시로 알려 드릴까요?"를 **한 번만** 묻고, 모르면 시각 없이 등록합니다 (B는 알림 없이 저장)
- 같은 대화에서 **끝난 할 일**(등록함, 이미 있음, 거절함)은 다시 제안하지 않습니다. 제목·날짜·시각·반복이 모두 같아야 같은 할 일입니다 ("다음 주 화요일에도 치과"는 새로 제안)
- 등록에 실패하면("다시 말씀해 주세요") 끝난 것으로 치지 않아서, 다시 말하면 다시 제안합니다
- 한 번에 여러 개를 말하면 하나씩 확인합니다. 확인 질문에 새 할 일을 말해도 뒤에 줄 세워 묻습니다
- 헷갈리는 대답은 거절 쪽으로 봅니다 ("그래도 안 할래요" → 거절)
- **음성 대화 중 B 재시도는 짧게** 합니다: 3초 시간 초과 × 2번 + 1초 간격(최대 약 7초). 공통 규칙(1·3·10초, 3번 재시도)대로 하면 "네" 뒤에 30초 넘게 말이 없을 수 있어서입니다. 실패하면 "조금 있다가 다시 말씀해 주세요"로 안내하고, 뒤에서 다시 보내는 것은 음성 연결 때 추가합니다
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

### 남은 일 (할 일 등록)

- 음성 대화(STT·TTS, 대화 LLM)에 붙이기: 추출이 1~3초 걸리므로 **대화 답변과 동시에** 돌리고, 할 일이 나오면 답변 뒤에 확인 질문을 붙이는 방식 권장
- 추출 정확도 검증용 문장 모음 (사투리, 숫자 표현, 할 일이 아닌 말)
- 사용자별 호칭·기본 시각은 명세 7(`GET /users/{user_id}`)이 정해지면 반영

## 하루 요약 (#29 생성, #31 B 전송, 명세 3)

```
그날 대화 (samples/day_u_123.json)  →  요약 쓰기 (Claude Sonnet 5.5)  →  규칙 검사  →  B POST /guardian-reports/daily  →  결과 저장
```

| 파일 | 하는 일 |
| --- | --- |
| [`companion/reports/writer.py`](companion/reports/writer.py) | 요약 쓰기. 하루 한 번 돌고 보호자가 읽는 글이라 Sonnet 5.5 (음성 스택 조사 결론). 거절되면 서버가 다른 모델로 다시 쓰게 server-side fallback 켬 |
| [`companion/reports/guard.py`](companion/reports/guard.py) | 보호자에게 나가기 전 규칙 검사 (아래) |
| [`companion/reports/batch.py`](companion/reports/batch.py) | 사용자마다 쓰기 → 보내기 → 저장. 명령줄로 실행 |
| [`companion/reports/b_client.py`](companion/reports/b_client.py) | 명세 3 호출. 배치라 공통 규칙대로 재시도 (1·3·10초), `409 ALREADY_SENT`면 덮어쓰지 않음 |
| [`companion/reports/models.py`](companion/reports/models.py) | 하루치 대화(입력)와 명세 3 요청 본문 |
| [`companion/b_http.py`](companion/b_http.py) | B 호출 공통부 (재시도, 에러 본문 읽기). 할 일 등록도 같이 씀 |

### 지키는 규칙

- **brief에는 일정·건강·기분 내용을 넣지 않습니다** (공유 범위와 상관없이 항상 보호자에게 감). 보호자 푸시 본문에도 brief만 들어갑니다
- **각 section에는 자기 항목 내용만** 넣습니다. 보호자는 동의한 항목만 받기 때문에, 예를 들어 기분만 공유했는데 mood에 "치과 가기 귀찮아하셨어요"가 있으면 일정이 새어 나갑니다
- 의료 표현을 쓰지 않습니다 ("치매", "증상", "진단" 등)
- 위 규칙은 프롬프트로 지시하고, **보내기 전에 단어 목록으로 한 번 더 검사**합니다. 걸리면 이유를 알려 주고 한 번 다시 쓰게 하고, 그래도 걸리면(또는 다시 쓰기 호출이 실패하면) brief는 기본 문장으로 바꾸고 해당 section은 뺍니다 (동의하지 않은 내용이 새는 것보다 빠지는 편이 낫다고 봄)
- 단어 목록은 완벽하지 않습니다. 놓치는 표현도 있고, 멀쩡한 문장이 걸려 section이 빠질 수도 있습니다. 짧은 말은 다른 낱말 안에 들어 있기 쉬워서("스마트폰"의 "마트") 좁혀 두었습니다
- section을 빼도 `mood_level`은 보냅니다. 기분 공유 동의 여부로 B가 따로 거르고, "며칠째 low" 판단에 쓰이기 때문입니다
- 대화 횟수(`stats.conversation_count`)는 어르신이 한 마디라도 한 대화만 셉니다. 다른 날짜(한국 시간 기준)에 시작한 대화는 뺍니다
- 대화가 없는 날은 보내지 않습니다 (임시, 아래)
- 화면·로그에는 상태만 남기고, 요약 문장은 `--out` 폴더(`.gitignore`)에만 저장합니다
- **같은 날짜로 다시 돌려도 안전합니다.** 결과 파일이 그날의 기록입니다
  - 이미 보냈으면(`sent`, `already_sent`) 아무것도 하지 않습니다. 보호자가 실제로 받은 내용의 기록이 지워지지 않습니다
  - B가 응답하지 않았으면(`unavailable`) 요약을 새로 쓰지 않고 **같은 `request_id`로 같은 본문을 다시 보냅니다** (B는 받았는데 응답만 끊긴 경우에도 중복이 생기지 않게)
  - 그 밖(쓰기 실패, 거절, 대화 없음)은 처음부터 다시 합니다
- 입력 파일 하나가 깨져도 다른 사용자는 계속 돌리고, 실패가 있으면 종료 코드 1로 끝납니다

### 실행

```bash
python tools/mocks/report_mock.py        # B mock 먼저 (PR #66)
cd server
python -m companion.reports.batch samples/day_u_123.json --out ../.reports
```

### 정해야 할 것

- **대화가 없는 날**: 지금은 보내지 않습니다. "오늘은 대화가 없었어요"를 보호자에게 알리면 안부 확인이 되지만 감시 느낌을 줄 수 있습니다. `mood_level`이 필수라 보낸다면 값도 정해야 합니다
- **`stats.completed_todos`**: 이슈 #29는 "완료한 할 일(B 조회)"인데 명세에 B 조회 API가 없습니다. 지금은 입력 파일에 있으면 넣고 없으면 뺍니다
- **입력**: 대화 기록 저장(#18)이 생기면 파일 대신 거기서 읽습니다
- **실행 시각**: 보호자 요약 발송 시각(명세 7 `report_send_time`, 기본 21:00) 전에 돌리는 스케줄러는 서버 스택(#1)이 정해지면 붙입니다
