# 연결 지점 mock 서버

B의 실제 서버가 완성되기 전에 A가 먼저 개발할 수 있게 하는 가짜 서버입니다.
[docs/api-contracts.md](../../docs/api-contracts.md)의 요청·응답 형식을 그대로 따릅니다.
Python 3.10 이상만 있으면 되고, 설치할 것은 없습니다. 데이터는 메모리에만 있어서 서버를 끄면 사라집니다.

| mock | 명세 | 이슈 | 기본 주소 |
| --- | --- | --- | --- |
| [todo_mock.py](todo_mock.py) | 1. 할 일 등록 | #45 | `http://127.0.0.1:8045` |

## 실행

```bash
python tools/mocks/todo_mock.py
```

실기기(폰)에서 접속하려면 `--host 0.0.0.0`으로 띄우고, 같은 와이파이에서 내 컴퓨터 IP로 호출합니다.

## 1. 할 일 등록 mock

`POST /todos` — 명세 1번 (PR #37 초안) 형식

```bash
curl -X POST http://127.0.0.1:8045/todos -H "Content-Type: application/json" -d '{
  "request_id": "6f1c2e0a-8b7d-4a51-9a63-2f4e1b7c9d10",
  "user_id": "u_123",
  "kind": "appointment",
  "title": "치과 예약",
  "date": "2026-11-24",
  "time": "15:30",
  "time_hint": null,
  "repeat": null,
  "original_text": "내일 오후 세 시 반에 치과 예약 있어요",
  "source": { "conversation_id": "c_456", "message_id": "m_789" }
}'
```

```json
{ "todo_id": "t_001", "title": "치과 예약", "next_at": "2026-11-24T15:30:00+09:00", "reminder": true }
```

### mock이 돌려주는 응답

| 상황 | 응답 |
| --- | --- |
| 정상 | `201` + `todo_id`, `title`, `next_at`, `reminder` |
| 같은 `request_id`로 다시 보냄 | `200` + 처음 응답 그대로 (새로 만들지 않음) |
| `time_hint`만 있음 | 기본 시각으로 계산: morning 08:00, noon 12:00, evening 18:00, bedtime 21:00 (회의에서 확정 전 임시값) |
| `time`·`time_hint` 둘 다 있음 | `400 INVALID_REQUEST` (둘 중 하나만) |
| `time`·`time_hint` 둘 다 없음 | `next_at: null`, `reminder: false` |
| 반복 (`date: null`) | 오늘부터 시작해서 다음 알림 시각 |
| 과거 날짜, 오늘인데 이미 지난 시각 | `400 INVALID_DATE` |
| `title`이 비어 있음 | `400 MISSING_TITLE` |
| 반복도 아니고 `date`도 없음 | `400 MISSING_SCHEDULE` |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 그 밖의 형식 오류 (UUID 아님, 없는 `kind`, `HH:MM` 아님 등) | `400 INVALID_REQUEST` |

> "오늘인데 이미 지난 시각" → `INVALID_DATE`, `404` → `USER_NOT_FOUND`, `time`·`time_hint` 둘 다 → `400`은 #37 초안(0b778ee) 기준입니다. #37이 확정되면 다시 맞춥니다.

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `GET /todos?user_id=u_123`: 등록된 할 일 목록. "말로 등록한 할 일이 목록에 보이는지" 확인용
- `GET /health`: 서버가 떠 있는지 확인
- `X-Mock-Fail: 503` 헤더: 강제로 5xx 응답 → A의 재시도(같은 `request_id`로 최대 3번) 확인
- `X-Mock-Delay: 3` 헤더: 3초 늦게 응답 → 시간 초과 처리 확인 (최대 30초, 숫자가 아니거나 음수면 무시)

## 테스트

```bash
python -m unittest discover tools/mocks
```

## 개인정보

서버 로그에는 경로와 상태 코드만 남기고, 대화 원문(`original_text`)은 남기지 않습니다.
