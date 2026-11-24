# 연결 지점 mock 서버

B의 실제 서버가 완성되기 전에 A가 먼저 개발할 수 있게 하는 가짜 서버입니다.
[docs/api-contracts.md](../../docs/api-contracts.md)의 요청·응답 형식을 그대로 따릅니다.
Python 3.10 이상만 있으면 되고, 설치할 것은 없습니다. 데이터는 메모리에만 있어서 서버를 끄면 사라집니다.

| mock | 명세 | 이슈 | 기본 주소 |
| --- | --- | --- | --- |
| [todo_mock.py](todo_mock.py) | 1. 할 일 등록 | #45 | `http://127.0.0.1:8045` |
| [alert_mock.py](alert_mock.py) | 4. 긴급 알림 | #49 | `http://127.0.0.1:8049` |

## 실행

```bash
python tools/mocks/todo_mock.py
```

```bash
python tools/mocks/alert_mock.py
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

## 4. 긴급 알림 mock

명세 4번 (PR #37 초안, 0b778ee) 형식. 실제 푸시·알림톡은 보내지 않고, 보냈다고 가정한 결과만 돌려줍니다.
**두 번에 나눠 호출합니다**: 감지 즉시 `POST /alerts`(`pending`) → 어르신이 고르면 `PATCH /alerts/{alert_id}`.

### 4-1. 감지 즉시: `POST /alerts`

```bash
curl -X POST http://127.0.0.1:8049/alerts -H "Content-Type: application/json" -d '{
  "request_id": "c3d4e5f6-a7b8-4c9d-8e0f-1a2b3c4d5e6f",
  "user_id": "u_123",
  "type": "fraud",
  "severity": "high",
  "reason": "가족을 사칭해 급하게 송금을 요구하는 메신저 피싱 의심",
  "evidence": {
    "text": "아들이 휴대폰이 고장 났다고 문자로 급하게 300만원만 보내달래요.",
    "conversation_id": "c_456",
    "message_id": "m_801"
  },
  "user_response": "pending",
  "detected_at": "2026-11-22T14:05:12+09:00"
}'
```

```json
{
  "alert_id": "al_001",
  "severity": "high",
  "deliveries": [
    { "guardian_id": "g_01", "channel": "push", "status": "sent" },
    { "guardian_id": "g_01", "channel": "alimtalk", "status": "queued" }
  ]
}
```

### 4-2. 어르신의 선택: `PATCH /alerts/{alert_id}`

```bash
curl -X PATCH http://127.0.0.1:8049/alerts/al_001 -H "Content-Type: application/json" -d '{
  "request_id": "d4e5f6a7-b8c9-4d0e-9f1a-2b3c4d5e6f70",
  "user_response": "dismissed",
  "responded_at": "2026-11-22T14:05:40+09:00"
}'
```

```json
{
  "alert_id": "al_001",
  "severity": "medium",
  "deliveries": [
    { "guardian_id": "g_01", "channel": "push", "status": "sent" },
    { "guardian_id": "g_01", "channel": "alimtalk", "status": "canceled" },
    { "guardian_id": "g_01", "channel": "push", "status": "sent" }
  ]
}
```

### severity 규칙 (명세 4번 표대로 B가 적용)

| `user_response` | 사기 (`fraud`) | 응급 (`emergency`) |
| --- | --- | --- |
| `pending`, `call_119_requested`, `guardian_call_requested`, `guardian_check_requested` | 그대로 | 그대로 |
| `dismissed` | `medium`으로 낮춤 → `queued` 알림톡을 `canceled`로 | 그대로 |
| `no_response` | 그대로 | `high`로 올림 → 알림톡 추가 |

- `dismissed`면 사기·응급 모두 "괜찮다고 응답하셨어요" 후속 푸시를 하나 더 보냅니다 (`deliveries` 끝에 push 추가)
- POST에서 처음부터 최종 선택(`pending` 아닌 값)을 보내면 규칙을 먼저 적용한 뒤 한 번만 발송합니다. 이 알림에는 PATCH를 보낼 수 없습니다 (`409`)

### mock이 돌려주는 응답

| 상황 | 응답 |
| --- | --- |
| POST 정상 | `202` + `alert_id`, 적용한 `severity`, `deliveries` (`high`: push `sent` + alimtalk `queued`, `medium`: push만) |
| POST 같은 `request_id` | `202` + 처음 응답 그대로 (다시 발송하지 않음) |
| PATCH 정상 | `200` + 같은 모양. `deliveries`에는 처음 발송과 추가 발송이 모두 들어감 |
| PATCH 같은 `request_id` | `200` + 처음 응답 그대로 (후속 푸시를 다시 보내지 않음) |
| PATCH 이미 최종 선택이 있음 | `409 ALREADY_RESPONDED` |
| PATCH 없는 `alert_id` | `404 NOT_FOUND` |
| PATCH `user_response: pending` | `400 INVALID_REQUEST` |
| `user_id`가 `u_no_guardian` | `deliveries: []` (보호자 없음). severity 조정은 똑같이 함 |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 형식 오류 (UUID 아님, 없는 enum 값, 빈 `reason`·`evidence.text`, 시간대 없는 시각 등) | `400 INVALID_REQUEST` |

보호자는 사용자마다 `g_01` 한 명이 연결되어 있다고 가정합니다.

> **mock에서 임시로 정한 것** (#37에서 확정되면 맞춰 고칩니다)
>
> - 낮출 때 취소한 알림톡은 `status: "canceled"`로 보여 줌 (명세의 status 값에 아직 없음)
> - 후속 푸시는 일반 push 항목과 같은 모양 (구분 필드 없음)
> - POST 같은 `request_id` 재요청도 `202` (1번처럼 `200`으로 바꿀지 미정)
> - `evidence.conversation_id`·`message_id`는 검사하지 않음 (표에 필수 표시가 없음)
> - "같은 사용자·같은 `type` 10분 안 알림 합치기", 방해 금지 시간은 회의 확정 전이라 넣지 않음. 매번 바로 발송

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `GET /alerts?user_id=u_123`: 받은 알림 목록. 지금 `severity`와 원래 요청값 `requested_severity`, `user_response`, `responded_at`도 함께 보임
- `GET /health`, `X-Mock-Fail`, `X-Mock-Delay`: 1번 mock과 같음

## 테스트

```bash
python -m unittest discover tools/mocks
```

## 구조

공통 코드(오류 형식, 응답 보내기, 테스트용 헤더, CORS, 실행)는 [_common.py](_common.py)에 있습니다.
새 mock은 `MockHandler`를 상속해 `route()`만 채우면 됩니다 (명세 3·6·7 예정).

## 개인정보

서버 로그에는 경로와 상태 코드만 남기고, 대화 원문(`original_text`, `evidence.text`)은 남기지 않습니다.
