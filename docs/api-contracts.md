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
| 6 | A 음성 대화 → B 구독 | 대화 횟수(사용량) 기록·제한 확인 | 미정 |
| 7 | B 회원 → 전체 | 사용자·보호자 정보, 공유 범위(건강/일정/기분) 조회 | 미정 |

## 공통 규칙 (초안)

- **형식**: HTTP + JSON. 서버를 하나로 합치면 같은 모양의 함수 호출로 바꿔도 됩니다
- **시각**: ISO 8601, 한국 시간대 명시 (`2026-11-23T15:30:00+09:00`). 날짜만 있으면 `YYYY-MM-DD`
- **사용자 식별**: `user_id` (문자열, 7번 회원 API 기준)
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
| `stats` | object | | 보호자 화면 통계용 |

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
    { "guardian_id": "g_01", "channel": "push", "status": "queued" },
    { "guardian_id": "g_02", "channel": "push", "status": "scheduled", "scheduled_at": "2026-11-29T21:00:00+09:00" }
  ]
}
```

| `deliveries[].status` | 의미 |
| --- | --- |
| `queued` | 곧 보냄 |
| `sent` | 보냄 |
| `failed` | 보내지 못함 |
| `scheduled` | 나중에 묶어서 보냄 (무료 등급은 주 1회). `scheduled_at`에 보낼 예정 시각 |

- `channel`은 MVP에서는 `push`만 씁니다 (알림톡은 MVP 이후)
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
    { "guardian_id": "g_01", "channel": "push", "status": "sent" },
    { "guardian_id": "g_02", "channel": "push", "status": "sent" }
  ]
}
```

- `severity`는 B가 실제로 적용한 값입니다 (아래 규칙 적용 후)
- `channel`은 MVP에서는 `push`만 씁니다 (알림톡은 MVP 이후)
- **보호자가 없거나 모든 발송이 실패하면** `deliveries`가 비어 있거나 전부 `failed`입니다. 이때 A는 응급이면 어르신에게 119에 직접 전화하도록 다시 안내합니다. 재시도를 다 해도 POST가 실패했을 때도 똑같이 안내합니다
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
| 보호자가 안 열어 봤을 때 | 5분 뒤 한 번 더 보냄 (재알림) | 다시 보내지 않음 |

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
- 올릴 때(`medium` → `high`)는 그때 긴급 푸시를 다시 보냅니다
- `dismissed`면 보호자에게 "어르신이 괜찮다고 응답하셨어요" 후속 푸시를 보냅니다. 그 밖의 선택도 보호자 앱의 알림 상세에 표시합니다

## 5. 모임 목록 조회 (B → A)

- 요청: TBD
- 응답: TBD

## 6. 대화 사용량 (A → B)

- 요청: TBD
- 응답: TBD (남은 횟수, 초과 시 "내일 또 이야기해요" 마무리)

## 7. 사용자·공유 범위 조회 (B → 전체)

- 요청: TBD
- 응답: TBD

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
