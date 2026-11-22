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
  | 5xx / 시간 초과 | B 장애 | 같은 `request_id`로 최대 3번 재시도 (1초, 3초, 10초 간격) |

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
| `repeat` | `{ "freq": "daily" }` \| `{ "freq": "weekly", "weekdays": ["MON","THU"] }` \| null | | 반복 규칙 |
| `original_text` | string | O | 어르신이 실제로 한 말. 등록 내용 확인용 |
| `source` | object | O | 대화 기록 추적용 |

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

에러 코드: `INVALID_DATE`(과거 날짜), `MISSING_TITLE`, `MISSING_SCHEDULE`(반복도 날짜도 없음)

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
| `brief` | string | O | 한 문장 요약. **공유 범위와 상관없이 항상 전달되는 기본값** ("감시 느낌 피하기" 원칙) |
| `sections.schedule` | string \| null | | 일정 공유에 동의했을 때만 전달 |
| `sections.health` | string \| null | | 건강 공유에 동의했을 때만 전달 |
| `sections.mood` | string \| null | | 기분 공유에 동의했을 때만 전달 |
| `mood_level` | `"good"` \| `"normal"` \| `"low"` | O | 기분 공유에 동의했을 때만 전달. 며칠째 `low`인지 판단하는 데도 사용 |
| `stats` | object | | 보호자 화면 통계용 |

- 각 section은 그것만 따로 읽어도 이해되는 문장으로 씁니다 (다른 section이 걸러질 수 있음)
- 의료 표현은 쓰지 않습니다 ("치매 증상" X → "같은 질문을 여러 번 하셨어요" O)
- 사용자 한 명당 날짜 하나에 요약 하나. 아직 발송 전이면 같은 날짜로 다시 보낼 때 덮어쓰고, 이미 발송했으면 `409 ALREADY_SENT`

응답 `202 Accepted`:

```json
{
  "report_id": "r_001",
  "shared_sections": ["schedule", "mood"],
  "deliveries": [
    { "guardian_id": "g_01", "channel": "alimtalk", "status": "queued" }
  ]
}
```

- 보호자가 없으면 `deliveries: []`. A는 에러로 처리하지 않습니다 (요약은 어르신 화면에도 쓰임)

## 4. 긴급 알림 (A → B)

사기·응급 문장을 감지하면 바로 호출합니다. **무료 등급 사용량 제한, 공유 범위와 상관없이 항상 동작합니다.**

`POST /alerts`

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
  "user_response": "guardian_check_requested",
  "detected_at": "2026-11-22T14:05:12+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `type` | `"fraud"` \| `"emergency"` | O | 사기 / 응급 |
| `severity` | `"high"` \| `"medium"` | O | `high`: 즉시 알림. `medium`: 앱 푸시만 (알림톡 X) |
| `reason` | string | O | 보호자에게 보여줄 한 줄 설명 |
| `evidence.text` | string | O | 감지 근거가 된 발화. **알림톡에는 넣지 않고** 보호자 앱에 로그인했을 때만 보여줌 |
| `user_response` | 아래 표 참고 | O | 경고 화면에서 어르신이 고른 것 |

| `user_response` | 의미 |
| --- | --- |
| `call_119_requested` | 119 연결을 골랐음 |
| `guardian_call_requested` | 보호자 전화를 골랐음 |
| `guardian_check_requested` | 사기 경고에서 "가족에게 확인할게요"를 골랐음 |
| `dismissed` | "괜찮아요"를 골랐음 (그래도 알림은 보냄. `severity`만 `medium`으로) |
| `no_response` | 정해진 시간 안에 응답이 없었음. 응급이면 가장 위험한 상황으로 봄 |

응답 `202 Accepted`:

```json
{
  "alert_id": "al_001",
  "deliveries": [
    { "guardian_id": "g_01", "channel": "push", "status": "sent" },
    { "guardian_id": "g_01", "channel": "alimtalk", "status": "queued" }
  ]
}
```

- **보호자가 없거나 모든 발송이 실패하면** `deliveries`가 비어 있거나 전부 `failed`입니다. 이때 A는 응급이면 어르신에게 119에 직접 전화하도록 다시 안내합니다
- 같은 사용자의 같은 `type` 알림이 10분 안에 또 오면 B가 하나로 합칠 수 있습니다 (중복 알림 방지, 회의에서 확정)

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
