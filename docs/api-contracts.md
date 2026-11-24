# A ↔ B 연결 지점 API 명세

기획서의 "다른 사람과 연결되는 지점"을 모은 목록입니다.
**받는 쪽이 먼저 명세와 가짜 응답(mock)을 만들어** 서로 기다리지 않게 합니다.
세부 요청/응답 형식은 기술 스택 확정 후 각 항목 아래에 채웁니다.

| # | 보내는 쪽 → 받는 쪽 | 내용 | 상태 |
| --- | --- | --- | --- |
| 1 | A 음성 대화 → B 할 일 | 대화에서 추출한 할 일·복약 등록 요청 | 초안 |
| 2 | A 음성 대화 → B 회원 프로필 | 대화에서 추출한 취미·관심사 반영 | 미정 |
| 3 | A 오늘 한 일 요약 → B 보호자 전송 | 생성된 하루 요약 전달 | 초안 |
| 4 | A 사기·응급 감지 → B 보호자 전송 | 긴급 알림 호출 | 초안 |
| 5 | B 모임 → A 할 일 추천 | 추천에 쓸 모임 목록 조회 | 미정 |
| 6 | A 음성 대화 → B 구독 | 대화 횟수(사용량) 기록·제한 확인 | 초안 |
| 7 | B 회원 → 전체 | 사용자·보호자 정보, 공유 범위(건강/일정/기분) 조회 | 초안 |

## 공통 규칙 (초안)

- **형식**: HTTP + JSON. 서버를 하나로 합치면 같은 모양의 함수 호출로 바꿔도 됩니다
- **시각**: ISO 8601, 한국 시간대 명시 (`2026-11-23T15:30:00+09:00`). 날짜만 있으면 `YYYY-MM-DD`
- **사용자 식별**: `user_id` (문자열, 7번 회원 API 기준). **보호자도 같은 `u_` 접두어**를 씁니다 (예: 어르신 `u_123`, 보호자 `u_789`). 보호자도 B DB `users`의 한 줄이기 때문입니다 (B 제안 #67, 회의 #68에서 확정)
- **재시도 안전성**: 모든 쓰기 요청에 `request_id`(UUID)를 넣습니다. 같은 `request_id`로 다시 보내면 B는 새로 만들지 않고 처음 결과를 그대로 돌려줍니다
- **에러 응답**:

  ```json
  { "error": { "code": "INVALID_DATE", "message": "date는 오늘 이후여야 합니다" } }
  ```

  | HTTP | 의미 | A의 처리 |
  | --- | --- | --- |
  | 400 | 요청 형식 오류 | 재시도하지 않고 로그를 남김. 사용자에게는 "다시 말씀해 주세요" |
  | 404 | 사용자 없음 | 재시도하지 않음 |
  | 409 | 지금 상태에서는 처리할 수 없음 (예: 이미 발송된 요약, 이미 있는 할 일) | 재시도하지 않음. 항목별 안내가 있으면 따름 |
  | 5xx / 시간 초과 | B 장애 | 같은 `request_id`로 최대 3번 재시도 (1초, 3초, 10초 간격) |

  모든 API에 공통인 에러 코드:

  | 코드 | HTTP | 언제 |
  | --- | --- | --- |
  | `INVALID_REQUEST` | 400 | 형식 오류 (UUID 아님, 없는 enum 값, 필수 필드 없음, 시간대 없는 시각 등). 항목별 코드가 따로 없을 때 씀 |
  | `USER_NOT_FOUND` | 404 | `user_id`에 해당하는 사용자가 없음 |
  | `NOT_FOUND` | 404 | 경로나 대상(`alert_id` 등)이 없음 |

- **보호자 발송 기록 (`deliveries`)**: 3번·4번 응답에 공통으로 들어갑니다. 보호자 한 명에게 보내는 푸시 하나가 한 줄입니다

  | 필드 | 타입 | 필수 | 설명 |
  | --- | --- | --- | --- |
  | `guardian_id` | string | O | 받는 보호자 (`u_`) |
  | `channel` | `"push"` | O | MVP는 앱 푸시만 (알림톡은 MVP 이후) |
  | `kind` | 아래 표 참고 | O | 무엇을 보내는 푸시인지 |
  | `priority` | `"urgent"` \| `"normal"` | O | `urgent`: 긴급 알림 채널 (소리 + 화면 켜짐, iOS time-sensitive, 방해 금지 무시). `normal`: 일반 알림 |
  | `status` | 아래 표 참고 | O | |
  | `scheduled_at` | 시각 \| null | O | 보낼 예정 시각. `scheduled`와 아직 보내지 않은 `re_alert`에만 값이 있음 |

  | `kind` | 쓰는 곳 | 의미 |
  | --- | --- | --- |
  | `report` | 3번 | 하루 요약 |
  | `alert` | 4번 | 긴급 알림. 처음 발송과, severity를 올려서 다시 보낸 긴급 푸시 |
  | `re_alert` | 4번 | 보호자가 `alert`를 열어 보지 않았을 때 5분 뒤 재알림 (`high`만) |
  | `follow_up` | 4번 | 어르신의 선택을 알리는 후속 푸시 (예: "어르신이 괜찮다고 응답하셨어요") |

  | `status` | 의미 |
  | --- | --- |
  | `queued` | 보낼 차례를 기다림 (방해 금지 시간이라 아침까지 미룬 것, 아직 때가 안 된 `re_alert` 포함) |
  | `scheduled` | 나중에 묶어서 보냄 (무료 등급 하루 요약은 주 1회). `scheduled_at`에 보낼 예정 시각 |
  | `sent` | 보냄 |
  | `failed` | 보내지 못함 |
  | `canceled` | 보내기 전에 취소함 (보호자가 이미 열어 봐서, 또는 severity를 낮춰서 필요 없어진 `re_alert`) |

  - **이미 보낸(`sent`) 푸시는 취소하지 않습니다.** `canceled`는 아직 보내지 않은 것에만 씁니다
  - 상태가 바뀌어도 줄을 지우지 않습니다. 4-2 응답에는 4-1 때의 줄이 바뀐 상태로 함께 들어갑니다

- **민감정보**: 대화 원문은 꼭 필요한 필드(`original_text`, `evidence.text`)에만 담고, B는 이 필드를 로그에 남기지 않습니다

## 1. 할 일 등록 (A → B)

어르신이 "등록할까요?"에 **동의한 뒤에만** 호출합니다.

`POST /todos`

요청:

```json
{
  "request_id": "6f1c2e0a-8b7d-4a51-9a63-2f4e1b7c9d10",
  "user_id": "u_123",
  "kind": "appointment",
  "title": "치과 예약",
  "date": "2026-11-23",
  "time": "15:30",
  "time_hint": null,
  "repeat": null,
  "original_text": "내일 오후 세 시 반에 치과 예약 있어요",
  "source": { "conversation_id": "c_456", "message_id": "m_789" }
}
```

복약 예시 ("혈압약은 매일 아침 먹어야 해요"):

```json
{
  "request_id": "0b9e4c3d-1f2a-4e6b-8c7d-5a4b3c2d1e0f",
  "user_id": "u_123",
  "kind": "medication",
  "title": "혈압약",
  "date": null,
  "time": null,
  "time_hint": "morning",
  "repeat": { "freq": "daily" },
  "original_text": "혈압약은 매일 아침 먹어야 해요",
  "source": { "conversation_id": "c_456", "message_id": "m_790" }
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `kind` | `"appointment"` \| `"medication"` \| `"task"` | O | 일정 / 복약 / 기타 할 일 |
| `title` | string | O | 짧은 제목. 화면과 음성 알림에 그대로 쓰임 |
| `date` | `YYYY-MM-DD` \| null | | 반복이 아니면 필수 |
| `time` | `HH:MM` \| null | | 정확한 시각을 말했을 때만 넣음. **A가 시각을 추측해서 넣지 않음** |
| `time_hint` | `"morning"` \| `"noon"` \| `"evening"` \| `"bedtime"` \| null | | "아침에"처럼 대략만 말했을 때. B가 사용자별 기본 시각으로 바꿈 |
| `repeat` | `{ "freq": "daily" }` \| `{ "freq": "weekly", "weekdays": ["MON","THU"] }` \| null | | 반복 규칙. `date`가 null이면 **B가 요청을 받은 날(오늘)부터** 시작 |
| `original_text` | string | O | 어르신이 실제로 한 말. 등록 내용 확인용 |
| `source` | object | O | 대화 기록 추적용 |

- `time`과 `time_hint`는 **둘 중 하나만** 넣습니다. 둘 다 넣으면 B는 `400 INVALID_REQUEST`로 거절합니다 (A는 정확한 시각을 들었으면 `time_hint`를 null로 보냄)
- `time`과 `time_hint`가 둘 다 null이면 B는 시각 미정으로 저장하고 알림을 보내지 않습니다. A는 보내기 전에 "몇 시로 알려 드릴까요?"라고 한 번 되묻습니다

응답 `201 Created` (같은 `request_id` 재요청이면 `200 OK` + 같은 내용):

```json
{
  "todo_id": "t_001",
  "title": "치과 예약",
  "next_at": "2026-11-23T15:30:00+09:00",
  "reminder": true
}
```

- A는 이 응답으로 확인 멘트를 만듭니다: "내일 오후 3시 반 치과 예약, 등록해 두었어요."
- `next_at`이 null이면 알림이 없다는 뜻입니다 (시각 미정)

에러 코드 (공통 코드 외):

| 코드 | HTTP | 언제 |
| --- | --- | --- |
| `INVALID_DATE` | 400 | 과거 날짜, 또는 반복이 아닌데 오늘 이미 지난 시각 |
| `MISSING_TITLE` | 400 | `title`이 비어 있음 |
| `MISSING_SCHEDULE` | 400 | 반복도 아니고 `date`도 없음 |
| `DUPLICATE_TODO` | 409 | 같은 사용자에게 같은 날짜·같은 시각·같은 제목의 할 일이 이미 있음 (`request_id`가 다른 경우. 같으면 재시도로 보고 `200 OK`) |

`DUPLICATE_TODO`일 때는 본문에 이미 있는 할 일의 id를 넣습니다. A는 새로 만들지 않고 "이미 등록돼 있어요"라고 안내합니다.

```json
{
  "error": {
    "code": "DUPLICATE_TODO",
    "message": "같은 할 일이 이미 등록되어 있습니다",
    "existing_todo_id": "t_001"
  }
}
```

## 2. 취미·관심사 반영 (A → B)

- 요청: TBD
- 응답: TBD

## 3. 하루 요약 전달 (A → B)

A가 매일 요약 배치를 돌린 뒤 사용자마다 한 번 호출합니다. **공유 범위 필터링은 B가 합니다.** A는 항목을 나눠서 전부 보내고, B가 동의한 항목만 보호자에게 전달합니다.

`POST /guardian-reports/daily`

요청:

```json
{
  "request_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "user_id": "u_123",
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
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `date` | `YYYY-MM-DD` | O | 요약 대상 날짜 |
| `brief` | string | O | 한 문장 요약. **공유 범위와 상관없이 항상 전달되는 기본값** ("감시 느낌 피하기" 원칙). 그래서 **일정·건강·기분 내용을 넣지 않습니다** ("오늘도 편안하게 지내셨어요" O / "기분 좋게 지내셨어요", "병원에 다녀오셨어요" X) |
| `sections.schedule` | string \| null | | 보호자에게는 일정 공유에 동의했을 때만 전달 |
| `sections.health` | string \| null | | 보호자에게는 건강 공유에 동의했을 때만 전달 |
| `sections.mood` | string \| null | | 보호자에게는 기분 공유에 동의했을 때만 전달 |
| `mood_level` | `"good"` \| `"normal"` \| `"low"` | O | 보호자에게는 기분 공유에 동의했을 때만 전달. 며칠째 `low`인지 판단하는 데도 사용 |
| `stats.conversation_count` | integer | | 그날 대화 횟수. 보호자에게 항상 전달 (안부 확인용, 대화 내용은 없음) |
| `stats.completed_todos` | integer | | 그날 끝낸 할 일 수. 보호자에게는 **일정 공유에 동의했을 때만** 전달 (할 일 수도 일정 정보라서, A 제안) |

- **A는 동의 여부와 상관없이 모든 항목을 채워 보내고, 보호자에게 넘길지는 B가 거릅니다.** "필수"는 A가 보낼 때 기준입니다
- **보호자 푸시 알림 본문에는 `brief`만** 넣습니다. `sections`는 보호자가 앱을 열었을 때만 보입니다 (잠금 화면에 보일 수 있어서. 4번 `evidence.text`와 같은 규칙)
- 각 section은 그것만 따로 읽어도 이해되는 문장으로 씁니다 (다른 section이 걸러질 수 있음)
- 의료 표현은 쓰지 않습니다 ("치매 증상" X → "같은 질문을 여러 번 하셨어요" O)
- 사용자 한 명당 날짜 하나에 요약 하나. 아직 발송 전이면 같은 날짜로 다시 보낼 때 덮어쓰고, 이미 발송했으면 `409 ALREADY_SENT`
- 덮어쓴 뒤에도 B는 **이전에 받은 `request_id`를 모두 기억**합니다. 덮어쓰기 전의 `request_id`로 재시도가 와도 새 요청으로 처리하지 않고 지금 저장된 결과를 돌려줍니다

응답 `202 Accepted`:

```json
{
  "report_id": "r_001",
  "shared_sections": ["schedule", "mood"],
  "deliveries": [
    { "guardian_id": "u_789", "channel": "push", "kind": "report", "priority": "normal", "status": "queued", "scheduled_at": null },
    { "guardian_id": "u_790", "channel": "push", "kind": "report", "priority": "normal", "status": "scheduled", "scheduled_at": "2026-11-29T21:00:00+09:00" }
  ]
}
```

- `deliveries`의 필드와 상태는 공통 규칙 "보호자 발송 기록"을 따릅니다. 하루 요약은 항상 `kind: "report"`, `priority: "normal"`입니다
- 보호자가 없으면 `deliveries: []`. A는 에러로 처리하지 않습니다 (요약은 어르신 화면에도 쓰임)

## 4. 긴급 알림 (A → B)

사기·응급 문장을 감지하면 바로 호출합니다. **무료 등급 사용량 제한, 공유 범위와 상관없이 항상 동작합니다.**

**두 번에 나눠 호출합니다** (A 제안, B 동의). 어르신이 경고 화면에서 고를 때까지 기다리면 응급 알림이 그만큼(무응답 판단 시간, 30초?) 늦어지기 때문입니다.

1. 감지 즉시 `POST /alerts`를 `user_response: "pending"`으로 보냅니다. B는 이때 바로 보호자에게 발송합니다
2. 어르신이 고르거나 무응답 시간이 지나면 `PATCH /alerts/{alert_id}`로 결과를 보냅니다. B는 아래 규칙에 따라 severity를 바꾸고 필요하면 추가로 발송합니다

### 4-1. 감지 즉시: `POST /alerts`

요청:

```json
{
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
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `type` | `"fraud"` \| `"emergency"` | O | 사기 / 응급 |
| `severity` | `"high"` \| `"medium"` | O | **감지 결과만 보고** A가 정한 값. 발송 방식 차이는 아래 "severity별 발송 방식" 표 참고 |
| `reason` | string | O | 보호자에게 보여줄 한 줄 설명 |
| `evidence.text` | string | O | 감지 근거가 된 발화. **푸시 알림 본문에는 넣지 않고** 보호자 앱에 로그인했을 때만 보여줌 (잠금 화면에 보일 수 있어서) |
| `user_response` | 아래 표 참고 | O | 보통 `"pending"`. 경고 화면을 띄우기 전에 이미 어르신의 선택을 알고 있으면 그 값을 바로 넣어도 됨 (그러면 4-2는 보내지 않음) |
| `detected_at` | 시각 | O | 감지한 시각 (시간대 필수) |

| `user_response` | 의미 |
| --- | --- |
| `pending` | 아직 고르지 않았음 (경고 화면을 띄운 상태). POST에서만 씀 |
| `call_119_requested` | 119 연결을 골랐음 |
| `guardian_call_requested` | 보호자 전화를 골랐음 |
| `guardian_check_requested` | 사기 경고에서 "가족에게 확인할게요"를 골랐음 |
| `dismissed` | "괜찮아요"를 골랐음 |
| `no_response` | 정해진 시간 안에 응답이 없었음 |

응답 `202 Accepted`:

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

- `severity`는 B가 실제로 적용한 값입니다 (아래 규칙 적용 후)
- `deliveries`의 필드와 상태는 공통 규칙 "보호자 발송 기록"을 따릅니다. 위 예시는 `high`라서 긴급 푸시를 바로 보내고, 5분 뒤 재알림을 걸어 둔 상태입니다
- **보호자가 없거나 모든 발송이 실패하면** `deliveries`가 비어 있거나 `kind: "alert"`인 줄이 전부 `failed`입니다. 이때 A는 응급이면 어르신에게 119에 직접 전화하도록 다시 안내합니다. 재시도를 다 해도 POST가 실패했을 때도 똑같이 안내합니다
- 같은 사용자의 같은 `type` 알림이 10분 안에 또 오면 B가 하나로 합칠 수 있습니다 (중복 알림 방지, 회의에서 확정)

### 4-2. 어르신의 선택: `PATCH /alerts/{alert_id}`

```json
{
  "request_id": "d4e5f6a7-b8c9-4d0e-9f1a-2b3c4d5e6f70",
  "user_response": "dismissed",
  "responded_at": "2026-11-22T14:05:40+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `request_id` | UUID | O | 재시도 안전성 (공통 규칙) |
| `user_response` | `pending`을 뺀 값 | O | 최종 선택 |
| `responded_at` | 시각 | O | 고른 시각. `no_response`면 무응답으로 판단한 시각 |

응답 `200 OK`: 4-1과 같은 모양. `deliveries`에는 처음 발송과 이번에 추가로 보낸 것이 모두 들어갑니다.

예: 위 사기 알림(`high`)에 어르신이 "괜찮아요"(`dismissed`)를 고른 경우. `medium`으로 낮추면서 아직 안 보낸 재알림을 취소하고, 후속 푸시를 보냅니다.

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

에러 코드 (공통 코드 외):

| 코드 | HTTP | 언제 |
| --- | --- | --- |
| `NOT_FOUND` | 404 | 없는 `alert_id` |
| `ALREADY_RESPONDED` | 409 | 이미 최종 선택이 기록됨 (`pending`이 아닌 알림). 선택은 한 번만 바꿀 수 있음 |

### severity별 발송 방식

MVP는 앱 푸시만 쓰므로 `high`와 `medium`은 푸시를 보내는 방식으로 구분합니다 (B 제안).

| | `high` | `medium` |
| --- | --- | --- |
| 푸시 종류 | 긴급 알림 채널 (소리 + 화면 켜짐, iOS는 time-sensitive) | 일반 알림 |
| 방해 금지 시간 | 무시하고 바로 보냄 | 아침까지 미룸 |
| 보호자가 안 열어 봤을 때 | 5분 뒤 한 번 더 보냄 (`re_alert`). 그 전에 열어 보면 `canceled` | 다시 보내지 않음 |

- iOS critical alert(무음 모드에서도 소리 남)는 Apple 별도 승인이 필요해서 MVP에서는 쓰지 않습니다

### severity 규칙 (B가 적용)

A는 감지 결과로만 `severity`를 정해 보내고, **어르신의 선택에 따른 조정은 B가 합니다.** 이 규칙을 A와 B 양쪽에 따로 두지 않기 위해서입니다.

| `user_response` | 사기 (`fraud`) | 응급 (`emergency`) |
| --- | --- | --- |
| `pending` | 그대로 | 그대로 |
| `call_119_requested`, `guardian_call_requested`, `guardian_check_requested` | 그대로 | 그대로 |
| `dismissed` | `medium`으로 낮춤 | **그대로** (안전 우선. 응급에서 "괜찮아요"는 실수로 눌렀을 수 있음) |
| `no_response` | 그대로 | **`high`로 올림** (가장 위험한 상황으로 봄) |

- **이미 보낸 알림은 취소하지 않습니다.** 낮출 때는 아직 보내지 않은 재알림만 취소합니다
- 올릴 때(`medium` → `high`)는 그때 긴급 푸시(`kind: "alert"`, `priority: "urgent"`)를 다시 보내고 재알림을 겁니다
- `dismissed`면 보호자에게 "어르신이 괜찮다고 응답하셨어요" 후속 푸시(`kind: "follow_up"`)를 보냅니다. 그 밖의 선택도 보호자 앱의 알림 상세에 표시합니다

## 5. 모임 목록 조회 (B → A)

- 요청: TBD
- 응답: TBD

## 6. 대화 사용량 (A → B)

무료 등급의 하루 대화 횟수를 B가 세고 알려 줍니다. 형식, 시각, `request_id`, 에러는 위의 "공통 규칙"을 따릅니다.

**사기·응급 감지와 긴급 알림(명세 4)은 사용량 제한과 상관없이 항상 동작합니다.** 횟수를 다 쓴 뒤에도 A는 어르신의 말을 듣고 감지를 계속하며, 감지와 경고 화면은 횟수에 세지 않습니다.

흐름:

1. 대화를 시작하기 전에 A가 `GET /usage/{user_id}/today`로 확인합니다
2. `allowed: false`면 A는 대화를 길게 이어 가지 않고 "오늘은 이야기를 많이 나눴네요. 내일 또 이야기해요."처럼 따뜻하게 마무리합니다
3. 대화가 시작되면(어르신이 첫 마디를 했을 때, 임시) A가 `POST /usage/conversations`로 한 번 기록합니다

### 6-1. 대화 시작 전 확인: `GET /usage/{user_id}/today`

요청 본문은 없습니다. "오늘"은 사용자 시간대(명세 7 `timezone`) 기준입니다.

응답 `200 OK`:

```json
{
  "user_id": "u_123",
  "date": "2026-11-24",
  "limit": 10,
  "used": 3,
  "remaining": 7,
  "allowed": true,
  "resets_at": "2026-11-25T00:00:00+09:00"
}
```

유료 등급 (무제한):

```json
{
  "user_id": "u_456",
  "date": "2026-11-24",
  "limit": null,
  "used": 12,
  "remaining": null,
  "allowed": true,
  "resets_at": "2026-11-25T00:00:00+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `date` | `YYYY-MM-DD` | O | 사용자 시간대 기준 오늘 |
| `limit` | integer \| null | O | 하루 대화 한도. **null이면 무제한** (유료 등급, 체험 기간 포함) |
| `used` | integer | O | 오늘 기록된 대화 횟수 |
| `remaining` | integer \| null | O | 남은 횟수. 0보다 작아지지 않음. `limit`이 null이면 null |
| `allowed` | boolean | O | 지금 새 대화를 시작해도 되는지. A는 이 값만 보고 판단합니다 |
| `resets_at` | 시각 | O | 횟수가 다시 채워지는 시각 (다음 날 경계) |

- **토큰 단위는 응답에 없습니다.** 어르신에게는 토큰을 보여 주지 않습니다. 원가를 재는 토큰 기록은 A가 따로 남깁니다
- A는 `remaining`으로 직접 계산하지 말고 `allowed`만 봅니다. 한도 규칙(등급, 체험 기간 등)을 B 한 곳에만 두기 위해서입니다

### 6-2. 대화 1회 기록: `POST /usage/conversations`

요청:

```json
{
  "request_id": "e5f6a7b8-c9d0-4e1f-8a2b-3c4d5e6f7a80",
  "user_id": "u_123",
  "conversation_id": "c_456",
  "started_at": "2026-11-24T10:15:03+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `request_id` | UUID | O | 재시도 안전성 (공통 규칙). **A는 대화 하나에 `request_id` 하나를 만들고**, 재시도나 나중 기록에도 같은 값을 씁니다. 같은 값이 다시 오면 B는 횟수를 늘리지 않습니다 |
| `user_id` | string | O | |
| `conversation_id` | string | O | A 대화 기록 참조. 기록 추적용 |
| `started_at` | 시각 | O | 대화가 시작된 시각 (시간대 필수). **B는 이 시각으로 어느 날짜의 횟수인지 정합니다.** 그래서 나중에 늦게 기록해도 맞는 날짜에 들어갑니다 |

응답 `201 Created` (같은 `request_id` 재요청이면 `200 OK` + 처음 결과): 6-1과 같은 모양입니다. `date`는 `started_at` 기준 날짜입니다.

```json
{
  "user_id": "u_123",
  "date": "2026-11-24",
  "limit": 10,
  "used": 4,
  "remaining": 6,
  "allowed": true,
  "resets_at": "2026-11-25T00:00:00+09:00"
}
```

- **한도를 넘어도 에러가 아닙니다.** B는 기록을 거절하지 않고 `201` + `allowed: false`를 돌려줍니다 (B 장애 때 허용한 대화를 나중에 기록하는 경우가 있음). 이때 `used`가 `limit`보다 클 수 있습니다
- 이미 시작한 대화는 끝까지 이어 갑니다. 기록 결과가 `allowed: false`여도 지금 대화를 끊지 않고, **다음 대화**부터 6-1에서 막힙니다

### 세지 않는 것

| 경우 | 이유 |
| --- | --- |
| 사기·응급 감지, 경고 화면, 긴급 알림 (명세 4) | 안전 기능은 항상 동작 |
| `allowed: false`일 때의 마무리 인사 | 대화를 시작하지 않았음 |
| 할 일 알림에 답하는 짧은 대답 ("약 먹었어") | 임시. 회의에서 정함 |

### B 장애 시 (A의 처리)

**권장: 대화를 허용하고 나중에 기록합니다.** 어르신이 B 장애 때문에 대화를 못 하는 일이 없게 하기 위해서입니다. 한도를 조금 넘는 비용은 감수합니다.

| 상황 | A의 처리 |
| --- | --- |
| 6-1이 실패하거나 2초(임시) 안에 답이 없음 | 어르신이 기다리고 있으므로 재시도하지 않고 **대화를 허용** |
| 6-2가 5xx·시간 초과 | 공통 규칙대로 같은 `request_id`로 3번 재시도. 그래도 실패하면 저장해 두었다가 B가 살아나면 같은 `request_id`로 다시 보냄 (24시간 안, 임시) |
| 6-1·6-2가 `404 USER_NOT_FOUND` | 대화를 시작하지 않음 (탈퇴 등) |

에러 코드: 공통 코드(`INVALID_REQUEST`, `USER_NOT_FOUND`) 외에 없습니다. 한도 초과는 에러가 아니라 `allowed: false`로 알려 줍니다.

### 회의에서 정할 것 (6번)

- [ ] 무료 하루 한도 (임시 10회. 베타에서 원가를 잰 뒤 확정)
- [ ] 등급별 한도 (임시: `free` 10회, `basic`·`premium` 무제한. 체험 기간은 유료와 같게)
- [ ] "대화 1회"의 기준 (임시: 어르신이 첫 마디를 한 때 1회. 말이 10분 이상 없다가 다시 말하면 새 대화)
- [ ] 아주 긴 대화 한 번을 1회로 셀지, 일정 길이마다 나눠 셀지 (원가)
- [ ] 날짜 경계 시각 (임시: 사용자 시간대 자정. 새벽에 잠 못 드는 어르신을 생각하면 새벽 4시도 후보)
- [ ] 할 일 알림에 답하는 짧은 대답을 횟수에 셀지
- [ ] 남은 횟수를 어르신에게 알려 줄지 (예: 마지막 1회일 때 "오늘은 이번까지 이야기할 수 있어요"), 숫자는 말하지 않을지
- [ ] B 장애 때 대화 허용(권장)으로 확정할지, 6-1 시간 초과 2초가 적당한지
- [ ] 보호자(결제자)에게 "오늘 대화를 다 쓰셨어요"를 알릴지 (구독 권유로 보이지 않게)

## 7. 사용자·공유 범위 조회 (B → 전체)

A와 B의 모든 기능이 사용자 정보와 공유 범위를 여기서 조회합니다. A는 대화 호칭, 할 일 시각 해석(`time_hint`), 요약 작성 때 공유 범위 참고에 씁니다. 형식, 시각, 에러는 위의 "공통 규칙"을 따릅니다.

**꼭 필요한 필드만 담습니다.** 실명, 전화번호, 생년, 동네 같은 개인정보는 넣지 않습니다 (동네는 모임 조회 명세 5에서 따로 다룸).

`GET /users/{user_id}`

요청 본문은 없습니다.

응답 `200 OK`:

```json
{
  "user_id": "u_123",
  "role": "senior",
  "display_name": "김영자 님",
  "timezone": "Asia/Seoul",
  "plan": "free",
  "guardians": [
    { "guardian_id": "u_789", "relation": "딸", "linked_at": "2026-11-20T19:02:00+09:00" }
  ],
  "sharing": {
    "schedule": { "granted": true, "decided_at": "2026-11-20T19:05:00+09:00" },
    "health": { "granted": false, "decided_at": "2026-11-20T19:05:00+09:00" },
    "mood": { "granted": true, "decided_at": "2026-11-21T09:30:00+09:00" }
  },
  "preferences": {
    "time_hints": { "morning": "08:00", "noon": "12:00", "evening": "18:00", "bedtime": "21:00" },
    "quiet_hours": { "start": "22:00", "end": "07:00" },
    "report_send_time": "21:00"
  },
  "updated_at": "2026-11-21T09:30:00+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `role` | `"senior"` \| `"guardian"` | O | 어르신 / 보호자 |
| `display_name` | string \| null | O | AI가 부르는 호칭 (예: "김영자 님"). null이면 A는 "어르신"이라고 부릅니다 |
| `timezone` | string | O | IANA 시간대 (예: `Asia/Seoul`). 하루 경계, 요약 날짜, 시각 계산의 기준 |
| `plan` | `"free"` \| `"basic"` \| `"premium"` | O | 지금 적용되는 등급. 구독이 없거나 끝났으면 `free`. 대화 허용 여부는 이 값이 아니라 명세 6 `allowed`로 판단합니다 |
| `guardians` | array | O | 연결된(해제되지 않은) 보호자. 없으면 `[]` |
| `guardians[].guardian_id` | string | O | 보호자의 사용자 ID (`u_` 접두어, 아래 "ID 규칙"). 명세 3·4 응답의 `guardian_id`와 같은 값 |
| `guardians[].relation` | string \| null | O | 어르신이 적은 관계 (예: "딸"). 대화에서 "따님께 알려 드릴게요"처럼 씀 |
| `guardians[].linked_at` | 시각 | O | 연결된 시각 |
| `sharing.schedule` / `.health` / `.mood` | `{ granted, decided_at }` | O | 보호자에게 일정 / 건강 / 기분을 공유하는지. `granted`는 boolean, `decided_at`은 마지막으로 정한 시각 |
| `preferences.time_hints` | object | O | 명세 1 `time_hint`별 기본 시각 (`HH:MM`, 사용자 시간대) |
| `preferences.quiet_hours` | `{ start, end }` \| null | O | 방해 금지 시간 (`HH:MM`). 자정을 넘을 수 있음. null이면 없음 |
| `preferences.report_send_time` | `HH:MM` | O | 보호자에게 하루 요약을 보내는 시각. A는 이 시각 전에 명세 3을 보냅니다 |
| `updated_at` | 시각 | O | 이 응답의 내용 중 무엇이든 마지막으로 바뀐 시각 |

### 공유 범위 규칙

- **공유 범위는 어르신 본인만 바꿀 수 있습니다.** 보호자가 원격 설정을 해도 바꿀 수 없습니다 (DB에서도 막음). 이 API는 조회만 합니다
- 한 번도 정하지 않은 항목은 `granted: false`, `decided_at: null`입니다. **기본은 공유하지 않음**입니다
- 보호자에게 무엇을 넘길지는 **B가 거릅니다** (명세 3). A는 `sharing`을 보고 요약 내용을 빼지 않고, 명세 3대로 모든 항목을 채워 보냅니다. A가 `sharing`을 쓰는 곳은 대화 안내 정도입니다 (예: "건강 이야기는 따님께 전하지 않아요")
- **긴급 알림(명세 4)은 공유 범위와 상관없이 항상 나갑니다.** `sharing`이 모두 false여도, `guardians`가 `[]`여도 A는 명세 4 호출을 막지 않습니다 (보호자가 없을 때의 처리는 명세 4 참고)

### ID 규칙 (B 제안)

- **어르신과 보호자 모두 `u_` 접두어를 씁니다** (예: 어르신 `u_123`, 보호자 `u_789`). 둘 다 DB `users` 테이블의 한 줄이기 때문입니다
- 보호자인지는 접두어가 아니라 필드 이름(`guardian_id`)과 `role`로 구분합니다
- 역할마다 접두어를 바꾸면 같은 사람이 `u_789`도 되고 `g_789`도 될 수 있어서, 비교·중복 확인에서 실수가 나기 쉽습니다
- 명세 3·4 예시의 `g_01`도 `u_` 형태로 맞추는 것을 제안합니다 (#37)

### 보호자 계정 (`role: "guardian"`)

- `guardians`는 `[]`, `sharing`과 `preferences`는 null입니다
- A는 어르신 계정만 조회한다고 봅니다. 보호자 쪽에서 "내가 연결된 어르신 목록"을 보는 API는 이번 범위가 아닙니다

### 캐시

- A는 대화를 시작할 때마다 조회합니다. 같은 대화 안에서는 다시 조회하지 않아도 됩니다 (임시: 최대 5분 캐시)
- 공유 범위가 바뀐 뒤 몇 분 동안은 A가 이전 값을 볼 수 있습니다. 실제로 보호자에게 넘기는 필터는 B가 보내는 순간의 값으로 하므로 문제가 되지 않습니다

에러 코드: 공통 코드(`INVALID_REQUEST`, `USER_NOT_FOUND`) 외에 없습니다. 탈퇴한 사용자도 `404 USER_NOT_FOUND`입니다.

### 회의에서 정할 것 (7번)

- [ ] 공유 범위를 어르신 단위로 둘지(이 초안), 보호자마다 다르게 둘지 (예: 딸에게는 건강, 아들에게는 일정만). 보호자마다면 `sharing`이 `guardians[]` 안으로 들어감
- [ ] 사용자 ID 접두어: B 제안은 **보호자도 `u_`로 통일** (위 "ID 규칙"). 확정되면 명세 3·4 예시의 `g_01`도 맞춤
- [ ] 긴급 알림 때 근거 발화를 보호자 앱에 보여 주는 동의(`emergency_alert`)를 이 응답에 넣을지 (A에게는 필요 없어 보여서 뺐음)
- [ ] 방해 금지 시간에 무엇을 미룰지 (할 일 알림, 요약 발송. 긴급 알림 `high`는 예외)
- [ ] A의 캐시 시간 (5분?)과, 공유 범위·호칭이 바뀌었을 때 B가 A에게 알려 줄지
- [ ] 보호자 계정 조회 때 돌려줄 내용 (연결된 어르신 목록 등)

## 회의에서 정할 것

- [ ] A와 B 서버를 하나로 합칠지 나눌지 (나누면 서버 간 인증 방식도)
- [ ] `time_hint`별 기본 시각 (예: morning = 08:00) — 사용자가 바꿀 수 있게 할지
- [ ] 긴급 알림 중복 방지 시간 (10분?)
- [ ] 하루 요약 발송 시각 (21:00?)
- [ ] 응급 경고 화면의 무응답 판단 시간 (30초?)
- [x] 긴급 알림 두 번 호출(4-1 감지 즉시 POST → 4-2 선택 PATCH)과 severity 규칙 표 (A 제안, B 동의)
- [x] 사기 경고도 감지 즉시 보호자에게 보냄 (A 제안, B 동의. 송금은 몇 분 안에 일어남)
- [x] MVP 보호자 전송은 앱 푸시만, 알림톡은 MVP 이후 (A·B 합의)
- [ ] `dismissed`일 때 보호자 후속 푸시를 보낼지 ("감시당한다"는 느낌)
- [ ] 가입 때 "긴급 알림" 동의를 공유 범위와 따로 받을지 (4번이 공유 범위와 상관없이 동작하려면 필요. B #41)
- [ ] 방해 금지 시간: `high`는 밤에도 보내고 `medium`은 아침까지 미루는 안 (위 "severity별 발송 방식" 표) 확정
- [ ] 방해 금지 시간대 기본값 (예: 22:00~07:00)
- [ ] 보호자 ID도 `u_` 접두어로 통일 (B 제안 #67. 이 명세 예시는 `u_789`로 맞춰 둠)
- [ ] 하루 요약 `stats.completed_todos`는 일정 공유에 동의했을 때만 전달 (A 제안)
