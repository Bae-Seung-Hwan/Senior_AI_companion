# 연결 지점 mock 서버

B의 실제 서버가 완성되기 전에 A가 먼저 개발할 수 있게 하는 가짜 서버입니다.
[docs/api-contracts.md](../../docs/api-contracts.md)의 요청·응답 형식을 그대로 따릅니다.
Python 3.10 이상만 있으면 되고, 설치할 것은 없습니다. 데이터는 메모리에만 있어서 서버를 끄면 사라집니다.

| mock | 명세 | 이슈 | 기본 주소 |
| --- | --- | --- | --- |
| [todo_mock.py](todo_mock.py) | 1. 할 일 등록 | #45 | `http://127.0.0.1:8045` |
| [report_mock.py](report_mock.py) | 3. 하루 요약 | #48 | `http://127.0.0.1:8048` |
| [alert_mock.py](alert_mock.py) | 4. 긴급 알림 | #49 | `http://127.0.0.1:8049` |
| [user_mock.py](user_mock.py) | 6. 대화 사용량 + 7. 사용자·공유 범위 | #58, #42 | `http://127.0.0.1:8042` |

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

```bash
python tools/mocks/user_mock.py                 # 무료 한도를 바꾸려면 --free-limit 2
```

실기기(폰)에서 접속하려면 `--host 0.0.0.0`으로 띄우고, 같은 와이파이에서 내 컴퓨터 IP로 호출합니다.

## 1. 할 일 등록 mock

`POST /todos` — 명세 1번 형식

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
| 같은 사용자·`date`·`time`·`title`이 이미 있음 (`request_id`는 다름) | `409 DUPLICATE_TODO` + `existing_todo_id` |
| `time_hint`만 있음 | 기본 시각으로 계산: morning 08:00, noon 12:00, evening 18:00, bedtime 21:00 (회의에서 확정 전 임시값) |
| `time`·`time_hint` 둘 다 있음 | `400 INVALID_REQUEST` (둘 중 하나만) |
| `time`·`time_hint` 둘 다 없음 | `next_at: null`, `reminder: false` |
| 반복 (`date: null`) | 요청 받은 날(오늘)부터 시작해서 다음 알림 시각 |
| 과거 날짜, 오늘인데 이미 지난 시각 | `400 INVALID_DATE` |
| `title`이 비어 있음 | `400 MISSING_TITLE` |
| 반복도 아니고 `date`도 없음 | `400 MISSING_SCHEDULE` |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 그 밖의 형식 오류 (UUID 아님, 없는 `kind`, `HH:MM` 아님 등) | `400 INVALID_REQUEST` |

> **중복 판단 임시 규칙** (명세에 없어서 mock이 정함): `time`이 null이면 `time_hint`가 같을 때 같은 시각으로 봅니다 (`time`·`time_hint` 둘 다 null끼리도 같음). 반복이라 `date`가 null이면 null끼리 같은 날짜로 봅니다. `title`은 앞뒤 공백을 빼고 비교합니다.

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

명세 4번과 공통 규칙 "보호자 발송 기록 (`deliveries`)" (PR #37 초안, 557c732) 형식. MVP는 **앱 푸시만** 씁니다 (알림톡은 MVP 이후).
실제 푸시는 보내지 않고, 보냈다고 가정한 결과만 돌려줍니다.
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
    { "guardian_id": "u_789", "channel": "push", "kind": "alert", "priority": "urgent", "status": "sent", "scheduled_at": null },
    { "guardian_id": "u_789", "channel": "push", "kind": "re_alert", "priority": "urgent", "status": "queued", "scheduled_at": "2026-11-22T14:10:12+09:00" }
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
    { "guardian_id": "u_789", "channel": "push", "kind": "alert", "priority": "urgent", "status": "sent", "scheduled_at": null },
    { "guardian_id": "u_789", "channel": "push", "kind": "re_alert", "priority": "urgent", "status": "canceled", "scheduled_at": "2026-11-22T14:10:12+09:00" },
    { "guardian_id": "u_789", "channel": "push", "kind": "follow_up", "priority": "normal", "status": "sent", "scheduled_at": null }
  ]
}
```

### 발송 방식 (`deliveries`)

| 상황 | 추가되는 줄 |
| --- | --- |
| `high` 발송 | `alert` `urgent` `sent` + `re_alert` `urgent` `queued` (`scheduled_at`: 발송 시각 + 5분) |
| `medium` 발송 | `alert` `normal` `sent`만 |
| 올릴 때 (`medium` → `high`) | `alert` `urgent` `sent` + `re_alert` `queued`를 다시 보냄 (처음 `normal` 줄은 그대로) |
| 낮출 때 (`high` → `medium`) | 추가 없음. 아직 안 보낸 `re_alert`만 `canceled`로 바꿈 (이미 `sent`인 줄은 취소하지 않음) |
| `dismissed` | `follow_up` `normal` `sent` ("어르신이 괜찮다고 응답하셨어요") |
| 보호자가 알림을 열어 봄 (mock 전용 엔드포인트) | 추가 없음. 아직 안 보낸 `re_alert`만 `canceled` |

- 상태가 바뀌어도 줄을 지우지 않습니다. PATCH 응답에는 POST 때의 줄이 바뀐 상태로 함께 들어갑니다
- **`kind: "alert"`인 줄이 전부 `failed`이거나 `deliveries`가 비어 있으면** A는 응급일 때 어르신에게 119 직접 전화를 안내합니다. `u_push_fail`로 확인할 수 있습니다

### severity 규칙 (명세 4번 표대로 B가 적용)

| `user_response` | 사기 (`fraud`) | 응급 (`emergency`) |
| --- | --- | --- |
| `pending`, `call_119_requested`, `guardian_call_requested`, `guardian_check_requested` | 그대로 | 그대로 |
| `dismissed` | `medium`으로 낮춤 → 재알림 취소 | 그대로 |
| `no_response` | 그대로 | `high`로 올림 → 긴급 푸시 다시 |

- POST에서 처음부터 최종 선택(`pending` 아닌 값)을 보내면 규칙을 먼저 적용한 뒤 그 severity로 한 번만 발송합니다. 이 알림에는 PATCH를 보낼 수 없습니다 (`409`)

### mock이 돌려주는 응답

| 상황 | 응답 |
| --- | --- |
| POST 정상 | `202` + `alert_id`, 적용한 `severity`, `deliveries` |
| POST 같은 `request_id` | `202` + 처음 응답 그대로 (다시 발송하지 않음) |
| PATCH 정상 | `200` + 같은 모양. `deliveries`에는 처음 발송과 추가 발송이 모두 들어감 |
| PATCH 같은 알림에 같은 `request_id` | `200` + 처음 응답 그대로 (후속 푸시를 다시 보내지 않음) |
| PATCH 다른 알림에 이미 쓴 `request_id` | `400 INVALID_REQUEST` (그 알림은 바뀌지 않음) |
| PATCH 이미 최종 선택이 있음 | `409 ALREADY_RESPONDED` |
| PATCH 없는 `alert_id` | `404 NOT_FOUND` |
| PATCH `user_response: pending` | `400 INVALID_REQUEST` |
| `user_id`가 `u_no_guardian` | `deliveries: []` (보호자 없음). severity 조정은 똑같이 함 |
| `user_id`가 `u_push_fail` | 모든 푸시가 `failed`. 실패한 긴급 푸시에는 재알림을 걸지 않음 |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 형식 오류 (UUID 아님, 없는 enum 값, 빈 `reason`·`evidence.text`, 시간대 없는 시각 등) | `400 INVALID_REQUEST` |

보호자는 사용자마다 `u_789` 한 명이 연결되어 있다고 가정합니다. 보호자 ID도 어르신과 같은 `u_` 접두어를 씁니다 (명세 공통 규칙).

> **mock에서 임시로 정한 것** (#37에서 확정되면 맞춰 고칩니다)
>
> - 다른 알림에 이미 쓴 `request_id`로 PATCH하면 `400 INVALID_REQUEST` (명세에는 같은 `request_id` 재요청 규칙만 있음. A의 재시도 버그를 가리지 않으려고 거절)
> - 재알림 `scheduled_at`은 긴급 푸시를 보낸 시각(서버가 받은 시각) + 5분. `detected_at` 기준이 아님
> - 재알림은 시간이 지나도 `sent`로 바뀌지 않음 (mock은 `queued`로 둠)
> - 실패한 긴급 푸시(`failed`)에는 재알림을 걸지 않음
> - POST에서 처음부터 `dismissed`를 보내면 후속 푸시(`follow_up`)는 보내지 않음 (첫 알림과 동시에 가서)
> - POST 같은 `request_id` 재요청도 `202` (1번처럼 `200`으로 바꿀지 미정)
> - `evidence.conversation_id`·`message_id`는 검사하지 않음 (표에 필수 표시가 없음)
> - "같은 사용자·같은 `type` 10분 안 알림 합치기", 방해 금지 시간(`medium`은 아침까지 미룸)은 회의 확정 전이라 넣지 않음. 매번 바로 `sent`

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `POST /alerts/{alert_id}/opened`: 보호자가 알림을 열어 본 것으로 보고 아직 안 보낸 재알림을 `canceled`로. 본문 없음, 응답은 PATCH와 같은 모양
- `GET /alerts?user_id=u_123`: 받은 알림 목록. 지금 `severity`와 원래 요청값 `requested_severity`, `user_response`, `responded_at`도 함께 보임
- `GET /health`, `X-Mock-Fail`, `X-Mock-Delay`: 1번 mock과 같음

## 6·7. 사용자·공유 범위 + 대화 사용량 mock

명세 6·7번 (PR #67 초안) 형식. 6번의 한도는 7번의 등급(`plan`)으로 정해져서 한 서버에 같이 둡니다.

### 7. 사용자·공유 범위 조회: `GET /users/{user_id}`

```bash
curl http://127.0.0.1:8042/users/u_123
```

```json
{
  "user_id": "u_123",
  "role": "senior",
  "display_name": "김영자 님",
  "timezone": "Asia/Seoul",
  "area_name": "서울 강남구",
  "plan": "free",
  "guardians": [{ "guardian_id": "u_789", "relation": "딸", "linked_at": "2026-11-20T19:02:00+09:00" }],
  "sharing": {
    "schedule": { "granted": true, "decided_at": "2026-11-20T19:05:00+09:00" },
    "health": { "granted": false, "decided_at": "2026-11-20T19:05:00+09:00" },
    "mood": { "granted": true, "decided_at": "2026-11-21T09:30:00+09:00" }
  },
  "preferences": {
    "time_hints": { "morning": "08:00", "noon": "12:00", "evening": "18:00", "bedtime": "21:00" },
    "quiet_hours": { "start": "22:00", "end": "07:00" },
    "report_send_time": "21:00",
    "tts_speed": 1.0
  },
  "updated_at": "2026-11-21T09:30:00+09:00"
}
```

- `area_name`: 날씨용 시·구 이름 (할 일 추천 #33). 행정동은 넣지 않음. 지역을 정하지 않았으면 `null`
- `preferences.tts_speed`: TTS 속도 (숫자, 기본 `1.0`, DB `user_preferences.tts_speed`)
- `preferences.quiet_hours`: 어르신은 **할 일 알림**을 보내지 않는 시간

보호자 계정(`GET /users/u_789`)은 `sharing`이 `null`이고, `preferences`에는 **보호자 자신의** 방해 금지 시간만 있습니다. 하루 요약과 `medium` 알림을 아침까지 미루는 기준입니다 (어르신 `quiet_hours`와 다른 값).

```json
{
  "user_id": "u_789",
  "role": "guardian",
  "display_name": null,
  "timezone": "Asia/Seoul",
  "area_name": null,
  "plan": "free",
  "guardians": [],
  "sharing": null,
  "preferences": { "quiet_hours": { "start": "22:00", "end": "07:00" } },
  "updated_at": "2026-11-20T19:02:00+09:00"
}
```

| 테스트용 `user_id` | 내용 |
| --- | --- |
| `u_123` | 명세 예시와 같음. 무료, 호칭 "김영자 님", 지역 "서울 강남구", 보호자 `u_789`(딸), 일정·기분 공유 |
| `u_456` | `premium` (대화 무제한), 지역 "부산 해운대구" |
| `u_no_guardian` | 보호자 없음 (`guardians: []`), 지역 없음 |
| `u_789` | 보호자 계정 (`role: "guardian"`, `sharing`은 `null`, `preferences`는 보호자 방해 금지 시간) |
| `u_unknown` | `404 USER_NOT_FOUND` |
| 그 밖의 아무 `user_id` | 무료 어르신, 호칭·지역 없음(`null`), 보호자 `u_789`, **공유 안 함** |

실명·전화번호·생년·동네(행정동)는 응답에 없습니다 (명세 7 "꼭 필요한 필드만"). 지역은 날씨용 시·구까지만 줍니다.

### 6. 대화 사용량

```bash
curl http://127.0.0.1:8042/usage/u_123/today      # 6-1. 대화 시작 전 확인
curl -X POST http://127.0.0.1:8042/usage/conversations -H "Content-Type: application/json" -d '{
  "request_id": "e5f6a7b8-c9d0-4e1f-8a2b-3c4d5e6f7a80",
  "user_id": "u_123",
  "conversation_id": "c_456",
  "started_at": "2026-11-24T10:15:03+09:00"
}'                                                 # 6-2. 대화 1회 기록
```

```json
{ "user_id": "u_123", "date": "2026-11-24", "limit": 10, "used": 1, "remaining": 9, "allowed": true, "resets_at": "2026-11-25T00:00:00+09:00" }
```

| 상황 | 응답 |
| --- | --- |
| 6-1 확인 | `200` + `limit`, `used`, `remaining`, `allowed`, `resets_at` |
| 6-2 기록 | `201` + 같은 모양 (기록한 뒤의 횟수) |
| 6-2 같은 `request_id` | `200` + 처음 결과 그대로 (**두 번 세지 않음**) |
| 한도를 다 씀 | `allowed: false`, `remaining: 0`. **에러가 아님** |
| 한도를 넘겨 기록 (B 장애 뒤 늦게 기록 등) | 거절하지 않고 `201`. `used`가 `limit`보다 클 수 있음 |
| 늦게 기록 | `started_at`을 사용자 시간대로 바꾼 날짜에 셈 (어제 대화는 어제 횟수) |
| `basic`·`premium` | `limit: null`, `remaining: null`, 항상 `allowed: true` |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 형식 오류 (UUID 아님, 빈 `conversation_id`, 시간대 없는 `started_at` 등) | `400 INVALID_REQUEST` |

> **mock에서 임시로 정한 것** (#67에서 확정되면 맞춰 고칩니다)
>
> - 무료 한도 10회 (`--free-limit`로 바꿀 수 있음), 날짜 경계는 사용자 시간대 자정
> - 모든 사용자의 시간대는 `Asia/Seoul`. Windows에 시간대 데이터(`tzdata`)가 없으면 고정 `+09:00`으로 대신 씀 (한국은 서머타임이 없어 결과가 같음)
> - 보호자 방해 금지 시간 기본값 22:00~07:00, 어르신 `tts_speed` 기본값 `1.0`
> - "대화 1회"의 기준은 A가 정해서 기록하는 것으로 봄. mock은 받은 기록을 셀 뿐
> - 보호자 계정의 사용량도 같은 규칙으로 셈 (명세에 없음)

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `PATCH /mock/users/{user_id}`: `plan`, `display_name`, `area_name`, `sharing`만 바꿈. 예) `{"plan": "premium"}`, `{"sharing": {"health": true}}`, `{"area_name": null}`(지역 미설정 흐름 확인). 등급을 바꾸면 6번 한도에 바로 반영
- `POST /mock/usage/{user_id}/reset`: 오늘 횟수 비우기 (어제 기록은 남김)
- `X-Mock-Fail` / `X-Mock-Delay`: **GET에도 됩니다.** 6-1이 실패하거나 2초 넘게 걸릴 때 A가 대화를 허용하는지 확인할 때 씀

## 테스트

```bash
python -m unittest discover tools/mocks
```

## 구조

공통 코드(오류 형식, 응답 보내기, 테스트용 헤더, CORS, 실행)는 [_common.py](_common.py)에 있습니다.
새 mock은 `MockHandler`를 상속해 `route()`만 채우고 `run(Handler, 설명, 포트, 이름)`으로 띄웁니다. 옵션이 더 필요하면 `run(None, …, add_arguments=옵션을_붙이는_함수, handler_factory=lambda args: Handler)`처럼 옵션 값으로 Handler를 만드는 함수를 따로 줍니다 (`user_mock.py`의 `--free-limit` 참고).

## 개인정보

서버 로그에는 경로와 상태 코드만 남기고, 대화 원문(`original_text`, `evidence.text`)과 요약 문장(`sections`)은 남기지 않습니다.
