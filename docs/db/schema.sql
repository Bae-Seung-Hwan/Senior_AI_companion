-- 시니어 AI 동반자 DB 스키마 초안 (#3)
-- 상태: 초안. PostgreSQL 15 이상 (DB는 #1 회의에서 확정). 설명은 docs/db/README.md
--
-- 규칙
--   - PK는 bigint identity. API에서는 접두어를 붙여 보여준다 (users.id = 123 ↔ "u_123")
--   - 시각은 timestamptz, 날짜만은 date, 시각만은 time (한국 시간 기준 값)
--   - 지우지 않고 남겨야 하는 것(동의, 발송, 신고)은 UPDATE/DELETE 하지 않고 행을 쌓는다
--   - [민감] 표시 컬럼: 대화 원문·건강 정보. 로그 금지, 암호화·보관 기간 대상
--   - A 몫인 대화 기록(#18)은 이 파일에 없다. conversation_id / message_id는 문자열로만 참조한다

BEGIN;

-- ─────────────────────────────────────────────
-- 1. 회원·보호자 (#11)
-- ─────────────────────────────────────────────

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role          text        NOT NULL CHECK (role IN ('senior', 'guardian')),
    name          text        NOT NULL,                 -- 실명
    display_name  text,                                 -- AI가 부르는 호칭 (예: "김영자 님")
    birth_year    smallint    CHECK (birth_year BETWEEN 1900 AND 2100),
    phone         text,                                 -- E.164 (+8210...). 알림톡 발송에 사용
    region_code   text,                                 -- 행정동 코드. 정확한 위치는 저장하지 않음 (#54)
    region_name   text,                                 -- 예: "서울 강남구 역삼동"
    timezone      text        NOT NULL DEFAULT 'Asia/Seoul',
    created_at    timestamptz NOT NULL DEFAULT now(),
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

-- 어르신 개인 설정 (#43 보호자 원격 설정, 명세 1 time_hint)
CREATE TABLE user_preferences (
    user_id         bigint   PRIMARY KEY REFERENCES users(id),
    morning_time    time     NOT NULL DEFAULT '08:00',
    noon_time       time     NOT NULL DEFAULT '12:00',
    evening_time    time     NOT NULL DEFAULT '18:00',
    bedtime_time    time     NOT NULL DEFAULT '21:00',
    quiet_start     time     DEFAULT '22:00',          -- 방해 금지 시간 (긴급 high는 예외, 회의 확정 전)
    quiet_end       time     DEFAULT '07:00',
    report_send_time time    NOT NULL DEFAULT '21:00',  -- 보호자 요약 발송 시각 (#52)
    font_scale      numeric(3,2) NOT NULL DEFAULT 1.30,
    tts_speed       numeric(3,2) NOT NULL DEFAULT 0.90,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      bigint   REFERENCES users(id)
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
    date          date,                                 -- 한 번: 그날 / 반복: 시작일 (null이면 등록일)
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
    CHECK (date IS NOT NULL OR repeat_freq IS NOT NULL),              -- MISSING_SCHEDULE
    CHECK (repeat_freq IS DISTINCT FROM 'weekly' OR coalesce(cardinality(repeat_weekdays), 0) > 0),
    CHECK (time IS NULL OR time_hint IS NULL)                          -- 둘 중 하나만
);
CREATE INDEX todos_user_active ON todos (user_id, date) WHERE deleted_at IS NULL;

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
    evidence_text      text        NOT NULL,            -- [민감] 알림톡에 넣지 않고 보호자 앱에서만 표시
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

-- 발송 기록. 요약·긴급 알림·초대·할 일 알림을 누구에게, 어떤 채널로 보냈는지. 쌓기만 한다
CREATE TABLE notification_deliveries (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_id        bigint      NOT NULL REFERENCES users(id),
    channel             text        NOT NULL CHECK (channel IN ('push', 'alimtalk', 'sms')),
    subject_type        text        NOT NULL CHECK (subject_type IN ('daily_report', 'weekly_report', 'alert', 'invite', 'reminder')),
    subject_id          bigint      NOT NULL,           -- subject_type에 따라 daily_reports / alerts / guardian_invites / reminders의 id (FK 없음, README 참고)
    status              text        NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed', 'canceled')),  -- canceled: 긴급 알림 severity를 낮춰 아직 안 보낸 알림톡 취소
    fallback_of         bigint      REFERENCES notification_deliveries(id),  -- 푸시 실패 → 알림톡 대체 발송
    provider_message_id text,
    error_code          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz
);
CREATE INDEX notification_deliveries_subject ON notification_deliveries (subject_type, subject_id);
CREATE INDEX notification_deliveries_queue ON notification_deliveries (created_at) WHERE status = 'queued';

-- ─────────────────────────────────────────────
-- 4. 지역·취미 만남 (#14, 명세 2·5)
-- ─────────────────────────────────────────────

-- 관심사 (명세 2). "지금 즐기는 취미·활동"만. 본인이 지울 수 있음
CREATE TABLE user_interests (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint      NOT NULL REFERENCES users(id),
    name            text        NOT NULL,               -- 예: "등산", "서예"
    source          text        NOT NULL CHECK (source IN ('voice', 'manual')),
    conversation_id text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    removed_at      timestamptz
);
CREATE UNIQUE INDEX user_interests_active ON user_interests (user_id, name) WHERE removed_at IS NULL;

CREATE TABLE meetups (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id         bigint      NOT NULL REFERENCES users(id),
    hobby           text        NOT NULL,
    title           text        NOT NULL,
    description     text,
    region_code     text        NOT NULL,               -- 동네 단위 매칭
    place_name      text        NOT NULL,               -- 공공장소 (첫 만남 안전)
    starts_at       timestamptz NOT NULL,
    capacity        smallint    NOT NULL CHECK (capacity BETWEEN 2 AND 50),
    status          text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'canceled')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX meetups_match ON meetups (region_code, hobby, starts_at) WHERE status = 'open';

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

-- 하루 대화 횟수 (명세 6). 토큰 단위는 사용자에게 보여주지 않음
CREATE TABLE usage_daily (
    user_id             bigint   NOT NULL REFERENCES users(id),
    date                date     NOT NULL,
    conversation_count  integer  NOT NULL DEFAULT 0 CHECK (conversation_count >= 0),
    PRIMARY KEY (user_id, date)
);

COMMIT;
