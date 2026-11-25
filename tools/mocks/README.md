# 연결 지점 mock 서버

B의 실제 서버가 완성되기 전에 A가 먼저 개발할 수 있게 하는 가짜 서버입니다.
[docs/api-contracts.md](../../docs/api-contracts.md)의 요청·응답 형식을 그대로 따릅니다.
Python 3.10 이상만 있으면 되고, 설치할 것은 없습니다. 데이터는 메모리에만 있어서 서버를 끄면 사라집니다.

| mock | 명세 | 이슈 | 기본 주소 |
| --- | --- | --- | --- |
| [todo_mock.py](todo_mock.py) | 1. 할 일 등록 | #45 | `http://127.0.0.1:8045` |
| [report_mock.py](report_mock.py) | 3. 하루 요약 | #48 | `http://127.0.0.1:8048` |
| [alert_mock.py](alert_mock.py) | 4. 긴급 알림 | #49 | `http://127.0.0.1:8049` |

## 실행

```bash
python tools/mocks/todo_mock.py
```

```bash
python tools/mocks/report_mock.py
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

## 3. 하루 요약 mock

`POST /guardian-reports/daily` — 명세 3번과 공통 규칙 "보호자 발송 기록" (PR #37, 557c732) 형식.
**공유 범위 필터는 B가 합니다.** A는 항목을 전부 채워 보내고, mock이 동의한 항목만 골라 보호자에게 넘깁니다.
MVP 보호자 전송은 앱 푸시만이라 (2026-11-24 합의) `deliveries`는 `channel: "push"`, `kind: "report"`, `priority: "normal"`만 나옵니다. 실제로 보내지는 않습니다.

```bash
curl -X POST http://127.0.0.1:8048/guardian-reports/daily -H "Content-Type: application/json" -d '{
  "request_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "user_id": "u_share_all",
  "date": "2026-11-22",
  "brief": "오늘도 편안하게 잘 지내셨어요.",
  "sections": {
    "schedule": "오후에 치과에 다녀오셨고, 혈압약도 잘 챙겨 드셨어요.",
    "health": "무릎이 조금 쑤신다고 하셔서 집에서 쉬셨어요.",
    "mood": "손주 시험 이야기를 하시며 즐거워하셨어요."
  },
  "mood_level": "good",
  "stats": { "conversation_count": 6, "completed_todos": 2 },
  "generated_at": "2026-11-22T21:00:00+09:00"
}'
```

```json
{
  "report_id": "r_001",
  "shared_sections": ["schedule", "health", "mood"],
  "deliveries": [
    { "guardian_id": "u_789", "channel": "push", "kind": "report", "priority": "normal", "status": "queued", "scheduled_at": null }
  ]
}
```

무료 등급(`u_free_weekly`)은 주 1회 묶어 보내므로 `scheduled` + 보낼 예정 시각이 옵니다:

```json
{ "guardian_id": "u_789", "channel": "push", "kind": "report", "priority": "normal", "status": "scheduled", "scheduled_at": "2026-11-29T21:00:00+09:00" }
```

### 공유 범위

| 사용자 | 공유 범위 |
| --- | --- |
| 기본 (아무 `user_id`) | **아무것도 공유하지 않음** → 보호자에게는 `brief`만 ("감시 느낌 피하기" 기본값) |
| `u_share_all` | 일정·건강·기분 모두 공유 |
| 직접 바꾸기 | `PUT /mock/share-scope/{user_id}` + `{"schedule": true, "mood": true}` |

- `mood_level`도 기분 공유를 켰을 때만 보호자에게 보입니다
- `stats.conversation_count`는 항상, `stats.completed_todos`는 **일정 공유를 켰을 때만** 보호자에게 보입니다 (할 일 수도 일정 정보)
- 비어 있는(`null`) section은 동의했어도 `shared_sections`에 넣지 않습니다
- 푸시 본문에는 `brief`만 넣습니다. 잠금 화면에 보일 수 있어서 section·`stats`는 앱에서만 보여 줍니다

#### 공유 범위를 바꾼 뒤 보호자 화면

이미 보낸 요약은 **보낸 시점의 공유 범위 ∩ 지금 공유 범위**로 보여 줍니다 (A 제안, PR #66 리뷰).

| 바꾼 것 | 아직 안 보낸 요약 | 이미 보낸 요약 |
| --- | --- | --- |
| 철회 (켜져 있던 항목을 끔) | 바로 빠짐 | **바로 빠짐** ("언제든 철회 가능") |
| 새로 켬 | 보낼 때 들어감 | **소급하지 않음** (동의한 범위 안에서만) |

보낸 뒤 철회했다가 다시 켜면, 보낸 시점에 동의했던 항목이라 다시 보입니다. `mood_level`·`completed_todos`도 같은 규칙입니다.

### mock이 돌려주는 응답

| 상황 | 응답 |
| --- | --- |
| 정상 | `202` + `report_id`, `shared_sections`, `deliveries` (`queued`, `scheduled_at: null`) |
| `user_id`가 `u_free_weekly` (무료 등급) | `202` + `deliveries`가 `scheduled` + `scheduled_at` (요약 날짜 다음 날부터 첫 일요일 21:00) |
| 보내기 전에 같은 날짜로 다시 보냄 | `202` + 같은 `report_id`로 **덮어씀** |
| 같은 `request_id`로 다시 보냄 | `202` + 지금 저장된 결과 (새로 처리하지 않음) |
| **덮어쓰기 전의 `request_id`로 재시도** | `202` + 지금 저장된 결과 (예전 내용으로 되돌리지 않음) |
| 이미 보낸 날짜에 새 요약 | `409 ALREADY_SENT` |
| `user_id`가 `u_no_guardian` | `202` + `deliveries: []` (A는 에러로 처리하지 않음) |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 형식 오류 (UUID 아님, 날짜 형식, 빈 `brief`, 없는 `mood_level`, 시간대 없는 `generated_at` 등) | `400 INVALID_REQUEST` |

> **mock에서 임시로 정한 것** (#37에서 확정되면 맞춰 고칩니다)
>
> - 무료 등급 묶음 발송 시각(`scheduled_at`)은 **요약 날짜 다음 날부터 첫 일요일 21:00**. 명세 예시(11-22 → 11-29 21:00)에 맞춘 것이고, "하루 요약 발송 시각"이 회의에서 정해지면 맞춤
> - 무료 등급은 테스트용 `u_free_weekly`로만 구분 (구독 `plan`과는 아직 연결하지 않음)
> - 유료 등급 발송 전 상태는 `queued` (방해 금지 시간 등은 아직 반영하지 않음)
> - 보냄 처리 뒤에는 `status: "sent"`, `scheduled_at: null` (공통 규칙대로 `scheduled`일 때만 값)
> - `sections`를 아예 안 보내면 셋 다 `null`로 봄. `stats`를 안 보내면 보호자 화면의 `stats`는 `{}`
> - 미래 날짜는 검사하지 않음 (명세에 규칙 없음)

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `GET /guardian-reports?user_id=u_123`: 받은 요약 전체 + `guardian_view`(**보호자 화면에 실제로 보이는 내용**, 위 "공유 범위를 바꾼 뒤" 규칙 적용), `push_body`, `share_scope`, 보낼 때 저장한 `sent_scope`, 받은 `request_ids`
- `POST /mock/guardian-reports/{report_id}/send`: 발송 시각이 된 것처럼 보냄 처리 (`status`가 `sent`로, 그때 공유 범위를 `sent_scope`로 저장). 이후 같은 날짜는 `409`
- `PUT /mock/share-scope/{user_id}`: 공유 범위 바꾸기. 이미 받은 요약의 `shared_sections`는 그대로고, `guardian_view`는 위 규칙대로 바뀜
- `GET /health`, `X-Mock-Fail`, `X-Mock-Delay`: 1번 mock과 같음

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
    { "guardian_id": "u_789", "channel": "push", "status": "sent" },
    { "guardian_id": "u_789", "channel": "alimtalk", "status": "queued" }
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
    { "guardian_id": "u_789", "channel": "push", "status": "sent" },
    { "guardian_id": "u_789", "channel": "alimtalk", "status": "canceled" },
    { "guardian_id": "u_789", "channel": "push", "status": "sent" }
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

보호자는 사용자마다 `u_789` 한 명이 연결되어 있다고 가정합니다. 보호자 ID도 어르신과 같은 `u_` 접두어를 씁니다 (B 제안, #67 명세 7 "ID 규칙").

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
새 mock은 `MockHandler`를 상속해 `route()`만 채우면 됩니다 (명세 6·7 예정).

## 개인정보

서버 로그에는 경로와 상태 코드만 남기고, 대화 원문(`original_text`, `evidence.text`)과 요약 문장(`sections`)은 남기지 않습니다.
