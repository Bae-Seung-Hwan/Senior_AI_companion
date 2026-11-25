-- 시니어 AI 동반자 DB 스키마 초안 (#3)
-- 상태: 초안. PostgreSQL 15 이상 (DB는 #1 회의에서 확정). 설명은 docs/db/README.md
--
-- 규칙
--   - PK는 bigint identity. API에서는 접두어를 붙여 보여준다 (users.id = 123 ↔ "u_123", meetups.id = 12 ↔ "mt_12")
--     보호자도 users의 한 줄이라 같은 u_ 접두어를 쓴다 (역할은 role 컬럼과 필드 이름으로 구분)
--   - 시각은 timestamptz, 날짜만은 date, 시각만은 time (한국 시간 기준 값)
--   - 지우지 않고 남겨야 하는 것(동의, 발송, 신고)은 UPDATE/DELETE 하지 않고 행을 쌓는다
--   - [민감] 표시 컬럼: 대화 원문·건강 정보. 로그 금지, 암호화·보관 기간 대상
--   - A 몫인 대화 기록(#18)은 이 파일에 없다. conversation_id / message_id는 문자열로만 참조한다

BEGIN;

-- ─────────────────────────────────────────────
-- 1. 회원·보호자 (#11)
-- ─────────────────────────────────────────────

-- 행정동 (#54, 명세 5). 코드 → 이름. 정확한 위치(좌표)는 어디에도 저장하지 않는다
CREATE TABLE regions (
    code          text        PRIMARY KEY,              -- 행정동 코드 (행정안전부 10자리)
    name          text        NOT NULL                  -- 예: "서울 강남구 역삼1동". 명세 5 region_name
);

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role          text        NOT NULL CHECK (role IN ('senior', 'guardian')),
    name          text        NOT NULL,                 -- 실명
    display_name  text,                                 -- AI가 부르는 호칭 (예: "김영자 님")
    birth_year    smallint    CHECK (birth_year BETWEEN 1900 AND 2100),
    phone         text,                                 -- E.164 (+8210...). 알림톡 발송에 사용 (MVP 이후. MVP는 앱 푸시만)
    region_code   text        REFERENCES regions(code), -- 동네(행정동). null이면 아직 안 정함 → 명세 5 empty_reason "no_region"
    timezone      text        NOT NULL DEFAULT 'Asia/Seoul',
    created_at    timestamptz NOT NULL DEFAULT now(),
    suspended_at  timestamptz,                          -- 신고(user_reports)로 이용 정지. 정지된 주최자의 모임은 추천에서 뺌 (명세 5)
    deleted_at    timestamptz                           -- 탈퇴. 개인정보는 탈퇴 처리 때 지움
);

-- 로그인 수단 (#4). 한 사람이 여러 수단을 가질 수 있음
CREATE TABLE auth_identities (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint      NOT NULL REFERENCES users(id),
    provider      text        NOT NULL CHECK (provider IN ('kakao', 'apple', 'google', 'phone')),
    provider_uid  text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_uid)
);

-- 보호자 초대 (#40). 링크·코드를 어르신 폰의 카톡 공유로 보내거나, 전화번호를 받아 알림톡으로 보냄
CREATE TABLE guardian_invites (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    senior_id     bigint      NOT NULL REFERENCES users(id),
    code          text        NOT NULL UNIQUE,
    relation      text,                                 -- 어르신이 적은 관계 (예: "딸")
    invitee_phone text,                                 -- E.164. 알림톡 초대일 때만. 카톡 공유(링크)면 null. 사용·만료 후 지움
    expires_at    timestamptz NOT NULL,
    used_by       bigint      REFERENCES users(id),
    used_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 어르신 ↔ 보호자 연결. 보호자는 여러 명 가능
CREATE TABLE guardian_links (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    senior_id     bigint      NOT NULL REFERENCES users(id),
    guardian_id   bigint      NOT NULL REFERENCES users(id),
    relation      text,
    status        text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    invite_id     bigint      REFERENCES guardian_invites(id),
    linked_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz,
    revoked_by    bigint      REFERENCES users(id),     -- 어르신 본인이 해제했는지, 보호자가 나갔는지
    CHECK (senior_id <> guardian_id),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);
-- 같은 두 사람의 연결은 동시에 하나만 활성
CREATE UNIQUE INDEX guardian_links_active_pair ON guardian_links (senior_id, guardian_id) WHERE status = 'active';
CREATE INDEX guardian_links_guardian ON guardian_links (guardian_id) WHERE status = 'active';

-- 동의 이력 (#41). 행을 지우거나 고치지 않고 쌓기만 한다 (언제, 무엇에, 누가 동의·철회했는지)
CREATE TABLE consent_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint      NOT NULL REFERENCES users(id),   -- 동의의 주인 (어르신)
    consent_type  text        NOT NULL CHECK (consent_type IN (
                      'terms',             -- 이용약관
                      'privacy',           -- 개인정보 수집·이용
                      'sensitive_info',    -- 민감정보(대화·건강) 처리
                      'share_schedule',    -- 보호자에게 일정 공유
                      'share_health',      -- 보호자에게 건강 공유
                      'share_mood',        -- 보호자에게 기분 공유
                      'emergency_alert',   -- 긴급 알림 시 근거 발화를 보호자에게 공유 (명세 4)
                      'location',          -- 동네 정보 (모임 매칭)
                      'marketing')),
    action        text        NOT NULL CHECK (action IN ('grant', 'revoke')),
    policy_version text,                                 -- 약관·방침 버전 (예: "2026-11-01")
    actor_id      bigint      NOT NULL REFERENCES users(id),  -- 실제로 누른 사람 (보통 본인)
    channel       text        NOT NULL DEFAULT 'app' CHECK (channel IN ('app', 'voice', 'guardian_assist')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- 공유 범위는 보호자가 대신 바꿀 수 없음 (#43). 본인만
    CHECK (consent_type NOT LIKE 'share\_%' OR actor_id = user_id)  -- \_ : LIKE에서 _는 한 글자 와일드카드라 이스케이프
);
CREATE INDEX consent_events_latest ON consent_events (user_id, consent_type, created_at DESC);

-- 지금 유효한 동의 = 항목별 마지막 이벤트. 공유 범위 필터(명세 3·7)는 이 뷰를 읽는다
CREATE VIEW current_consents AS
SELECT DISTINCT ON (user_id, consent_type)
       user_id, consent_type, (action = 'grant') AS granted, created_at AS decided_at
FROM consent_events
ORDER BY user_id, consent_type, created_at DESC, id DESC;

-- 개인 설정 (#43 보호자 원격 설정, 명세 1 time_hint, 명세 7 preferences). 어르신·보호자 모두 각자 한 줄
-- 방해 금지 시간은 "받는 사람" 자신의 값을 쓴다 (README 13):
--   어르신 행 → 어르신이 받는 할 일 알림 / 보호자 행 → 보호자가 받는 하루 요약·medium 긴급 알림 (high는 무시하고 보냄)
CREATE TABLE user_preferences (
    user_id         bigint   PRIMARY KEY REFERENCES users(id),
    morning_time    time     NOT NULL DEFAULT '08:00',  -- time_hint 기본 시각 4개는 어르신 행에만 의미
    noon_time       time     NOT NULL DEFAULT '12:00',
    evening_time    time     NOT NULL DEFAULT '18:00',
    bedtime_time    time     NOT NULL DEFAULT '21:00',
    quiet_start     time     DEFAULT '22:00',          -- 방해 금지 시간. 명세 7 preferences.quiet_hours {start, end}. 자정을 넘을 수 있음
    quiet_end       time     DEFAULT '07:00',          -- 둘 다 null이면 방해 금지 없음 (quiet_hours: null). 기본값은 회의 확정 전
    report_send_time time    NOT NULL DEFAULT '21:00',  -- 보호자 요약 발송 시각 (#52). 어르신 행에만 의미
    font_scale      numeric(3,2) NOT NULL DEFAULT 1.30,
    tts_speed       numeric(3,2) NOT NULL DEFAULT 0.90,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      bigint   REFERENCES users(id),     -- 보호자가 어르신 설정을 원격으로 바꾸면 보호자 id
    CHECK ((quiet_start IS NULL) = (quiet_end IS NULL))
);

-- 푸시 토큰 (#50)
CREATE TABLE push_tokens (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint      NOT NULL REFERENCES users(id),
    platform      text        NOT NULL CHECK (platform IN ('ios', 'android')),
    token         text        NOT NULL UNIQUE,
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);

-- ─────────────────────────────────────────────
-- 2. 할 일·복약 (#12, 명세 1)
-- ─────────────────────────────────────────────

CREATE TABLE todos (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint      NOT NULL REFERENCES users(id),
    kind          text        NOT NULL CHECK (kind IN ('appointment', 'medication', 'task')),
    title         text        NOT NULL CHECK (length(btrim(title)) > 0),
    date          date        NOT NULL,                 -- 한 번: 그날 / 반복: 시작일. 요청에 date가 없으면(반복) B가 받은 날(사용자 시간대)로 채움 (명세 1)
    time          time,                                 -- 정확한 시각
    time_hint     text        CHECK (time_hint IN ('morning', 'noon', 'evening', 'bedtime')),
    repeat_freq   text        CHECK (repeat_freq IN ('daily', 'weekly')),
    repeat_weekdays text[]    CHECK (repeat_weekdays <@ ARRAY['MON','TUE','WED','THU','FRI','SAT','SUN']),
    repeat_until  date,
    remind_before_minutes smallint NOT NULL DEFAULT 0 CHECK (remind_before_minutes >= 0),
    source_type   text        NOT NULL CHECK (source_type IN ('voice', 'manual', 'guardian_assist')),
    original_text text,                                 -- [민감] 어르신이 실제로 한 말
    conversation_id text,                               -- A 대화 기록 참조 (#18)
    message_id    text,
    request_id    uuid        UNIQUE,                   -- 명세 공통 규칙: 같은 값 재요청 시 새로 만들지 않음
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz,
    -- MISSING_SCHEDULE(반복도 date도 없음)은 B가 date를 채우기 전에 API에서 검사한다 (DB에는 date가 항상 있음)
    CHECK (repeat_freq IS DISTINCT FROM 'weekly' OR coalesce(cardinality(repeat_weekdays), 0) > 0),
    CHECK (time IS NULL OR time_hint IS NULL)                          -- 둘 중 하나만
);
-- 409 DUPLICATE_TODO (명세 1): 같은 사용자·날짜·시각·제목이면 하나만. 지운 할 일은 제외
-- time이 null(time_hint만)이어도 같은 값으로 보려고 NULLS NOT DISTINCT (PG 15+). time_hint도 키에 넣어 "아침 혈압약"과 "저녁 혈압약"은 다른 할 일
-- 반복 할 일은 date = 시작일이라 같은 날 같은 반복을 두 번 등록하면 막히고, 다른 날 다시 등록한 것은 막지 않음 (README 12)
-- 할 일 목록 조회(user_id, date)도 이 인덱스를 씀
CREATE UNIQUE INDEX todos_no_dup ON todos (user_id, date, time, time_hint, title) NULLS NOT DISTINCT WHERE deleted_at IS NULL;

-- 완료 기록. 반복 할 일은 날짜(회차)마다 한 줄. "약 먹었어" → 여기에 기록
CREATE TABLE todo_completions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    todo_id         bigint      NOT NULL REFERENCES todos(id),
    occurrence_date date        NOT NULL,
    occurrence_time time,                               -- 하루 여러 번(아침·저녁) 복약 구분용, 필요하면
    completed_at    timestamptz NOT NULL DEFAULT now(),
    source          text        NOT NULL CHECK (source IN ('voice', 'tap', 'reminder_button', 'guardian_assist')),
    UNIQUE NULLS NOT DISTINCT (todo_id, occurrence_date, occurrence_time)
);

-- 예약된 알림 (#47). 스케줄러가 status='scheduled' AND scheduled_at <= now() 를 꺼내 보냄
CREATE TABLE reminders (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    todo_id         bigint      NOT NULL REFERENCES todos(id),
    occurrence_date date        NOT NULL,
    scheduled_at    timestamptz NOT NULL,
    status          text        NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'sent', 'failed', 'canceled')),
    sent_at         timestamptz,
    UNIQUE (todo_id, scheduled_at)
);
CREATE INDEX reminders_due ON reminders (scheduled_at) WHERE status = 'scheduled';

-- ─────────────────────────────────────────────
-- 3. 보호자 전송 (#13, 명세 3·4)
-- ─────────────────────────────────────────────

-- 하루 요약 (명세 3). A가 만들어 보내고 B가 저장. 사용자·날짜당 하나
CREATE TABLE daily_reports (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint      NOT NULL REFERENCES users(id),
    date            date        NOT NULL,
    brief           text        NOT NULL,               -- 공유 범위와 상관없이 항상 전달
    section_schedule text,
    section_health  text,                               -- [민감]
    section_mood    text,
    mood_level      text        NOT NULL CHECK (mood_level IN ('good', 'normal', 'low')),  -- 명세 3 필수. 보호자 전달은 기분 동의 시만
    stats           jsonb       NOT NULL DEFAULT '{}',  -- conversation_count, completed_todos
    generated_at    timestamptz NOT NULL,
    sent_at         timestamptz,                        -- 보낸 뒤에는 덮어쓰기 불가 (409 ALREADY_SENT)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, date)
);

-- 하루 요약을 만든 요청들 (명세 3). 덮어써도 이전 request_id를 모두 기억해서,
-- 예전 request_id로 재시도가 와도 새 요청으로 처리하지 않고 지금 요약을 돌려준다
CREATE TABLE daily_report_requests (
    request_id      uuid        PRIMARY KEY,
    report_id       bigint      NOT NULL REFERENCES daily_reports(id),
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX daily_report_requests_report ON daily_report_requests (report_id);

-- 긴급 알림 (명세 4). 사용량 제한·공유 범위와 상관없이 항상 처리
CREATE TABLE alerts (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            bigint      NOT NULL REFERENCES users(id),
    type               text        NOT NULL CHECK (type IN ('fraud', 'emergency')),
    severity           text        NOT NULL CHECK (severity IN ('high', 'medium')),   -- 실제 적용 값
    requested_severity text        NOT NULL CHECK (requested_severity IN ('high', 'medium')),  -- A가 보낸 값
    reason             text        NOT NULL,
    evidence_text      text        NOT NULL,            -- [민감] 푸시 내용에 넣지 않고 보호자 앱 알림 상세에서만 표시
    conversation_id    text,
    message_id         text,
    user_response      text        NOT NULL DEFAULT 'pending' CHECK (user_response IN (
                           'pending',              -- 4-1 감지 즉시. 어르신이 아직 고르지 않음
                           'call_119_requested', 'guardian_call_requested',
                           'guardian_check_requested', 'dismissed', 'no_response')),
    detected_at        timestamptz NOT NULL,
    responded_at       timestamptz,                    -- 4-2 최종 선택 시각 (no_response면 무응답으로 판단한 시각)
    request_id         uuid        NOT NULL UNIQUE,    -- 4-1 POST
    response_request_id uuid       UNIQUE,             -- 4-2 PATCH. 선택은 한 번만 (409 ALREADY_RESPONDED)
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((user_response = 'pending') = (responded_at IS NULL))
);
CREATE INDEX alerts_user_recent ON alerts (user_id, type, detected_at DESC);  -- 10분 내 합치기 판단용

-- 발송 기록. 요약·긴급 알림·초대·할 일 알림을 누구에게, 어떤 채널로 보냈는지. 줄을 지우지 않는다 (상태만 바뀜)
-- 보호자에게 보낸 줄은 명세 3·4 응답의 deliveries 한 줄과 같다 (명세 공통 규칙 "보호자 발송 기록", README 11)
CREATE TABLE notification_deliveries (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_id        bigint      NOT NULL REFERENCES users(id),   -- 명세 guardian_id (보호자에게 보낸 줄)
    channel             text        NOT NULL CHECK (channel IN ('push', 'alimtalk', 'sms')),  -- MVP는 push만. alimtalk·sms는 MVP 이후
    subject_type        text        NOT NULL CHECK (subject_type IN ('daily_report', 'weekly_report', 'alert', 'invite', 'reminder')),
    subject_id          bigint      NOT NULL,           -- subject_type에 따라 daily_reports / alerts / guardian_invites / reminders의 id (FK 없음, README 참고)
    kind                text        CHECK (kind IN (
                            'report',              -- 하루 요약 (명세 3)
                            'alert',               -- 긴급 알림. 처음 발송과 severity를 올려 다시 보낸 긴급 푸시 (명세 4)
                            're_alert',            -- alert를 안 열어 봤을 때 5분 뒤 재알림 (high만)
                            'follow_up')),         -- 어르신 선택을 알리는 후속 푸시 (예: dismissed)
    priority            text        NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'normal')),  -- urgent: 긴급 알림 채널, 방해 금지 무시
    status              text        NOT NULL DEFAULT 'queued' CHECK (status IN (
                            'queued',              -- 보낼 차례를 기다림 (방해 금지라 아침까지 미룸, 아직 때가 안 된 re_alert 포함)
                            'scheduled',           -- 나중에 묶어서 보냄 (무료 등급 하루 요약 주 1회). scheduled_at 필수
                            'sent', 'failed',
                            'canceled')),          -- 아직 안 보낸 재알림 취소 (보호자가 이미 열어 봄 / severity를 낮춤). 보낸 것은 취소하지 않음
    scheduled_at        timestamptz,                    -- 보낼 예정 시각. scheduled와 대기 중인 re_alert에 필수. 보낸 뒤·취소 뒤에도 값은 남김
    opened_at           timestamptz,                    -- 받은 사람이 푸시를 열어 본 시각. alert를 열어 봤으면 re_alert를 canceled로
    fallback_of         bigint      REFERENCES notification_deliveries(id),  -- (MVP 이후) 푸시 실패 → 알림톡 대체 발송
    provider_message_id text,
    error_code          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    -- 보호자에게 가는 요약·긴급 알림은 kind 필수, 초대·할 일 알림(invite, reminder)은 kind 없음
    CHECK ((kind IS NOT NULL) = (subject_type IN ('daily_report', 'weekly_report', 'alert'))),
    CHECK (kind IS NULL OR (kind = 'report') = (subject_type IN ('daily_report', 'weekly_report'))),
    CHECK (priority = 'normal' OR coalesce(kind IN ('alert', 're_alert'), false)),  -- 요약·후속 푸시·초대·할 일 알림은 항상 normal
    -- scheduled_at: 주 1회 묶음(scheduled)은 하루 요약에만 쓰고 예정 시각 필수. 재알림은 대기·보냄·취소 모두 예정 시각이 있음
    CHECK (status <> 'scheduled' OR (kind IS NOT DISTINCT FROM 'report' AND scheduled_at IS NOT NULL)),
    CHECK (kind IS DISTINCT FROM 're_alert' OR scheduled_at IS NOT NULL),
    CHECK (opened_at IS NULL OR status = 'sent')                     -- 보낸 것만 열어 볼 수 있음
);
CREATE INDEX notification_deliveries_subject ON notification_deliveries (subject_type, subject_id, recipient_id);
CREATE INDEX notification_deliveries_queue ON notification_deliveries (created_at) WHERE status = 'queued';
-- 스케줄러가 scheduled_at <= now() 인 주 1회 요약·재알림을 꺼냄
CREATE INDEX notification_deliveries_due ON notification_deliveries (scheduled_at) WHERE status IN ('queued', 'scheduled') AND scheduled_at IS NOT NULL;

-- ─────────────────────────────────────────────
-- 4. 지역·취미 만남 (#14, 명세 2·5)
-- ─────────────────────────────────────────────

-- 관심사 (명세 2). "지금 즐기는 취미·활동"만. 본인이 지울 수 있음
-- 지운 행은 남겨 둔다: 대화에서 온 같은 이름을 90일(임시) 안에 다시 넣지 않으려고 (명세 2 recently_removed)
CREATE TABLE user_interests (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint      NOT NULL REFERENCES users(id),
    name            text        NOT NULL CHECK (name = btrim(name) AND length(name) BETWEEN 1 AND 20),  -- 예: "등산". 명세 2 INVALID_INTEREST_NAME
    source          text        NOT NULL CHECK (source IN ('voice', 'manual')),
    original_text   text,                               -- [민감] 어르신이 한 말. 저장할지는 회의(#68). 본인 화면에서만, 보호자에게는 안 보임
    conversation_id text,                               -- source = 'voice'일 때 A 대화 기록 참조
    message_id      text,
    request_id      uuid,                               -- 이 관심사를 추가한 명세 2 요청 (interest_requests)
    created_at      timestamptz NOT NULL DEFAULT now(),
    removed_at      timestamptz,                        -- 본인이 지운 시각
    CHECK (source = 'voice' OR (original_text IS NULL AND conversation_id IS NULL AND message_id IS NULL))
);
CREATE UNIQUE INDEX user_interests_active ON user_interests (user_id, name) WHERE removed_at IS NULL;
CREATE INDEX user_interests_removed ON user_interests (user_id, name, removed_at) WHERE removed_at IS NOT NULL;

-- 관심사 반영 요청 (명세 2). 같은 request_id가 다시 오면 처음 결과(항목별 status)를 그대로 돌려준다
CREATE TABLE interest_requests (
    request_id      uuid        PRIMARY KEY,
    user_id         bigint      NOT NULL REFERENCES users(id),
    results         jsonb       NOT NULL,               -- [{"name": "등산", "status": "added"}, ...] 요청 순서대로
    received_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE meetups (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id         bigint      NOT NULL REFERENCES users(id),
    hobby           text        NOT NULL,
    title           text        NOT NULL,
    description     text,
    region_code     text        NOT NULL REFERENCES regions(code),  -- 동네 단위 매칭 (명세 5)
    place_name      text        NOT NULL,               -- 공공장소 (첫 만남 안전)
    starts_at       timestamptz NOT NULL,
    capacity        smallint    NOT NULL CHECK (capacity BETWEEN 2 AND 50),  -- 주최자 포함
    status          text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'canceled')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX meetups_match ON meetups (region_code, hobby, starts_at) WHERE status = 'open';

-- 참여자. 주최자도 모임을 만들 때 한 줄 넣는다 (명세 5 joined_count는 주최자 포함, left_at IS NULL만 셈)
CREATE TABLE meetup_members (
    meetup_id       bigint      NOT NULL REFERENCES meetups(id),
    user_id         bigint      NOT NULL REFERENCES users(id),
    joined_at       timestamptz NOT NULL DEFAULT now(),
    left_at         timestamptz,
    PRIMARY KEY (meetup_id, user_id)
);

-- 차단 (#56). 차단하면 서로 목록에서 안 보임
CREATE TABLE user_blocks (
    blocker_id      bigint      NOT NULL REFERENCES users(id),
    blocked_id      bigint      NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blocker_id, blocked_id),
    CHECK (blocker_id <> blocked_id)
);

-- 신고 (#56). 운영자 확인용, 쌓기만 한다
CREATE TABLE user_reports (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reporter_id     bigint      NOT NULL REFERENCES users(id),
    target_id       bigint      NOT NULL REFERENCES users(id),
    meetup_id       bigint      REFERENCES meetups(id),
    reason          text        NOT NULL CHECK (reason IN ('money_request', 'harassment', 'spam', 'impersonation', 'other')),
    detail          text,
    status          text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed', 'actioned')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────
-- 5. 구독·사용량 (#15, 명세 6)
-- ─────────────────────────────────────────────

-- 혜택 받는 사람(어르신)과 결제자(자녀일 수 있음)를 나눈다
CREATE TABLE subscriptions (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint      NOT NULL REFERENCES users(id),   -- 혜택 받는 어르신
    payer_id            bigint      NOT NULL REFERENCES users(id),   -- 결제한 사람
    plan                text        NOT NULL CHECK (plan IN ('free', 'basic', 'premium')),
    status              text        NOT NULL CHECK (status IN ('trial', 'active', 'grace', 'canceled', 'expired')),
    store               text        CHECK (store IN ('app_store', 'play_store')),
    store_original_transaction_id text UNIQUE,         -- 또는 RevenueCat 고객 ID (#60에서 결정)
    trial_ends_at       timestamptz,
    current_period_end  timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
-- 한 어르신에게 유효한 구독은 하나
CREATE UNIQUE INDEX subscriptions_one_live ON subscriptions (user_id) WHERE status IN ('trial', 'active', 'grace');

-- 대화 기록 (명세 6-2). 대화 하나에 한 줄. request_id로 같은 대화를 두 번 세지 않는다
CREATE TABLE usage_conversations (
    request_id          uuid        PRIMARY KEY,           -- A가 대화 하나에 하나 만듦. 재시도·나중 기록에도 같은 값
    user_id             bigint      NOT NULL REFERENCES users(id),
    conversation_id     text        NOT NULL,              -- A 대화 기록 참조 (#18)
    started_at          timestamptz NOT NULL,              -- 대화 시작 시각 (A가 보낸 값)
    date                date        NOT NULL,              -- started_at을 사용자 시간대(users.timezone)로 바꾼 날짜. 이 날짜의 횟수로 셈
    created_at          timestamptz NOT NULL DEFAULT now() -- B가 받은 시각 (장애 후 늦게 기록되면 started_at보다 늦음)
);
CREATE INDEX usage_conversations_daily ON usage_conversations (user_id, date);

-- 하루 대화 횟수 (명세 6-1). 기록에서 세기만 한다 (횟수를 따로 저장하지 않아 두 값이 어긋날 일이 없음)
CREATE VIEW usage_daily AS
SELECT user_id, date, count(*)::integer AS conversation_count
FROM usage_conversations
GROUP BY user_id, date;

COMMIT;
