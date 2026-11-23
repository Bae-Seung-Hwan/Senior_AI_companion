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
| `time`·`time_hint` 둘 다 없음 | `next_at: null`, `reminder: false` |
| 반복 (`date: null`) | 오늘부터 시작해서 다음 알림 시각 |
| 과거 날짜, 오늘인데 이미 지난 시각 | `400 INVALID_DATE` |
| `title`이 비어 있음 | `400 MISSING_TITLE` |
| 반복도 아니고 `date`도 없음 | `400 MISSING_SCHEDULE` |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 그 밖의 형식 오류 (UUID 아님, 없는 `kind`, `HH:MM` 아님 등) | `400 INVALID_REQUEST` |

> "오늘인데 이미 지난 시각"을 `INVALID_DATE`로 거르는 것과 `404`의 코드 이름 `USER_NOT_FOUND`는 명세에 아직 없어서 mock에서 정한 것입니다. #37에서 확정되면 맞춰 고칩니다.

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `GET /todos?user_id=u_123`: 등록된 할 일 목록. "말로 등록한 할 일이 목록에 보이는지" 확인용
- `GET /health`: 서버가 떠 있는지 확인
- `X-Mock-Fail: 503` 헤더: 강제로 5xx 응답 → A의 재시도(같은 `request_id`로 최대 3번) 확인
- `X-Mock-Delay: 3` 헤더: 3초 늦게 응답 → 시간 초과 처리 확인

## 4. 긴급 알림 mock

`POST /alerts` — 명세 4번 (PR #37 초안) 형식. 실제 푸시·알림톡은 보내지 않고, 보냈다고 가정한 결과만 돌려줍니다.

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
  "user_response": "guardian_check_requested",
  "detected_at": "2026-11-22T14:05:12+09:00"
}'
```

```json
{
  "alert_id": "al_001",
  "deliveries": [
    { "guardian_id": "g_01", "channel": "push", "status": "sent" },
    { "guardian_id": "g_01", "channel": "alimtalk", "status": "queued" }
  ]
}
```

### mock이 돌려주는 응답

| 상황 | 응답 |
| --- | --- |
| `severity: high` | `202` + push `sent`, alimtalk `queued` |
| `severity: medium` | `202` + push `sent`만 |
| `user_response: dismissed` | 알림은 보내고 `severity`만 `medium`으로 낮춤 → push만 |
| 같은 `request_id`로 다시 보냄 | `202` + 처음 응답 그대로 (다시 발송하지 않음) |
| `user_id`가 `u_no_guardian` | `202` + `deliveries: []` (보호자 없음) |
| `user_id`가 `u_unknown` | `404 USER_NOT_FOUND` |
| 형식 오류 (UUID 아님, 없는 `type`·`severity`·`user_response`, 빈 `reason`·`evidence.text`, 시간대 없는 `detected_at` 등) | `400 INVALID_REQUEST` |

보호자는 사용자마다 `g_01` 한 명이 연결되어 있다고 가정합니다.

> **mock에서 임시로 정한 것** (#37에서 확정되면 맞춰 고칩니다)
>
> - 형식 오류 코드는 모두 `INVALID_REQUEST`, 사용자 없음은 `USER_NOT_FOUND` (4번 명세에 에러 코드가 없음)
> - 같은 `request_id` 재요청도 `202` (1번처럼 `200`으로 바꿀지 미정)
> - `detected_at`은 시간대(`+09:00`)가 있어야 함 (공통 규칙 "한국 시간대 명시"를 따름)
> - `evidence.conversation_id`·`message_id`는 검사하지 않음 (표에 필수 표시가 없음)
> - `no_response`여도 `severity`를 올리지 않고 A가 보낸 값 그대로 씀
> - "같은 사용자·같은 `type` 10분 안 알림 합치기"는 회의 확정 전이라 넣지 않음. 매번 새 `alert_id`로 발송
> - `dismissed`여도 알림을 보낼지, 방해 금지 시간에 `medium`을 미룰지는 회의에서 정하기로 해서, 지금은 명세 초안대로 항상 바로 보냄

### 확인·테스트용 (mock 전용, 실제 서버에는 없음)

- `GET /alerts?user_id=u_123`: 받은 알림 목록. 실제로 적용된 `severity`(`dismissed`면 낮춘 값)와 원래 요청값 `requested_severity`도 함께 보임
- `GET /health`, `X-Mock-Fail`, `X-Mock-Delay`: 1번 mock과 같음

## 테스트

```bash
python -m unittest discover tools/mocks
```

## 개인정보

서버 로그에는 경로와 상태 코드만 남기고, 대화 원문(`original_text`, `evidence.text`)은 남기지 않습니다.
