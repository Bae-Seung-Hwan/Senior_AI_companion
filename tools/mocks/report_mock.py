"""하루 요약 수신 API mock (명세 3, #48)

A가 B의 실제 서버를 기다리지 않고 "하루 요약 → 보호자에게 전달" 흐름을 개발할 수 있게 하는 가짜 서버.
docs/api-contracts.md 3번과 공통 규칙 "보호자 발송 기록" (PR #37, 557c732) 형식을 따른다. 공유 범위 필터는 B가 하므로,
A는 항목을 전부 보내고 mock이 동의한 항목만 골라낸다. 실제로 푸시를 보내지는 않는다.
데이터는 메모리에만 저장하고, 끄면 사라진다. 표준 라이브러리만 사용하므로 설치할 것이 없다.

MVP 보호자 전송은 앱 푸시만 한다 (2026-11-24 A·B 합의). 그래서 deliveries의 channel은 push뿐이고,
하루 요약은 항상 kind: "report", priority: "normal"이다.

보호자 화면(guardian_view)은 "보낸 시점의 공유 범위 ∩ 지금 공유 범위"로 거른다 (A 제안, PR #66 리뷰).
철회는 지난 요약에도 바로 반영하고, 보낸 뒤 새로 켠 항목은 지난 요약에 소급하지 않는다.

사용법:
    python tools/mocks/report_mock.py                 # http://127.0.0.1:8048
    python tools/mocks/report_mock.py --port 9000
    python tools/mocks/report_mock.py --host 0.0.0.0  # 같은 와이파이의 실기기에서 접속할 때

엔드포인트:
    POST /guardian-reports/daily                    하루 요약 전달 (명세 3)
    GET  /guardian-reports?user_id=…                받은 요약과 "보호자에게 보이는 모습" (mock 전용)
    PUT  /mock/share-scope/{user_id}                공유 범위 바꾸기 (mock 전용)
    POST /mock/guardian-reports/{report_id}/send    발송 시각이 된 것처럼 보냄 처리 (mock 전용)
    GET  /health                                    살아 있는지 확인

테스트용 헤더 (mock 전용, 명세에는 없음):
    X-Mock-Fail: 500        5xx 응답을 강제로 돌려줌 → A의 재시도 정책 확인
    X-Mock-Delay: 3         응답을 3초 늦춤 → 시간 초과 처리 확인

테스트용 user_id (mock 전용):
    u_unknown               404 USER_NOT_FOUND
    u_no_guardian           보호자가 없음 → deliveries: []
    u_free_weekly           무료 등급 (주 1회 묶음 발송) → status: "scheduled" + scheduled_at
    u_share_all             일정·건강·기분을 모두 공유하는 사용자
    그 밖의 사용자            처음에는 아무것도 공유하지 않음 (brief만 전달, "감시 느낌 피하기" 기본값)
"""

import copy
import threading
from datetime import date, datetime, time, timedelta

from _common import (
    KST,
    ApiError,
    MockHandler,
    invalid,
    not_found,
    parse_datetime,
    require_text,
    require_user,
    require_uuid,
    run,
)

SECTIONS = ("schedule", "health", "mood")
MOOD_LEVELS = {"good", "normal", "low"}

NO_GUARDIAN_USER_ID = "u_no_guardian"
SHARE_ALL_USER_ID = "u_share_all"
FREE_WEEKLY_USER_ID = "u_free_weekly"
DEFAULT_GUARDIAN_ID = "u_789"

# 무료 등급 묶음 발송 시각 (mock 임시값). 명세 3번 예시(2026-11-22 요약 → 2026-11-29 21:00)에 맞춰
# "요약 날짜 다음 날부터 첫 일요일 21:00"으로 정했다. 회의에서 발송 시각이 정해지면 맞춘다.
WEEKLY_SEND_WEEKDAY = 6  # 일요일 (date.weekday 기준)
WEEKLY_SEND_TIME = time(21, 0)


def default_scope(user_id):
    shared = user_id == SHARE_ALL_USER_ID
    return {name: shared for name in SECTIONS}


def weekly_send_at(report_date):
    """무료 등급 요약을 묶어 보낼 시각: 요약 날짜 다음 날부터 첫 일요일 21:00 (KST)."""
    days = (WEEKLY_SEND_WEEKDAY - report_date.weekday()) % 7 or 7
    return datetime.combine(report_date + timedelta(days=days), WEEKLY_SEND_TIME, KST).isoformat()


def validate(body):
    if not isinstance(body, dict):
        raise invalid("요청 본문은 JSON 객체여야 합니다")
    require_uuid(body.get("request_id"))
    require_user(body.get("user_id"))
    try:
        date.fromisoformat(body.get("date"))
    except (TypeError, ValueError):
        raise invalid("date는 YYYY-MM-DD 형식이어야 합니다") from None
    require_text(body.get("brief"), "brief")

    sections = body.get("sections")
    if sections is None:
        sections = {}
    if not isinstance(sections, dict):
        raise invalid("sections는 객체여야 합니다")
    for name in SECTIONS:
        value = sections.get(name)
        if value is not None and not isinstance(value, str):
            raise invalid(f"sections.{name}는 문자열 또는 null이어야 합니다")

    if body.get("mood_level") not in MOOD_LEVELS:
        raise invalid("mood_level은 good / normal / low 중 하나여야 합니다")
    stats = body.get("stats")
    if stats is not None and not isinstance(stats, dict):
        raise invalid("stats는 객체여야 합니다")
    parse_datetime(body.get("generated_at"), "generated_at")


def effective_scope(report, scope):
    """보호자 화면에 쓰는 공유 범위.

    보내기 전에는 지금 범위, 보낸 뒤에는 "보낸 시점의 범위 ∩ 지금 범위".
    철회는 바로 반영하고, 보낸 뒤 새로 켠 항목은 지난 요약에 소급하지 않는다.
    """
    sent = report["sent_scope"]
    return {n: scope[n] and (sent is None or sent[n]) for n in SECTIONS}


def guardian_view(report, scope):
    """보호자에게 실제로 보이는 내용. 동의한 항목만 남긴다."""
    scope = effective_scope(report, scope)
    stats = {}
    if "conversation_count" in report["stats"]:
        stats["conversation_count"] = report["stats"]["conversation_count"]  # 항상 (안부 확인용)
    if scope["schedule"] and "completed_todos" in report["stats"]:
        stats["completed_todos"] = report["stats"]["completed_todos"]  # 할 일 수도 일정 정보
    view = {
        "report_id": report["report_id"],
        "date": report["date"],
        "brief": report["brief"],  # 공유 범위와 상관없이 항상
        "sections": {n: report["sections"][n] for n in SECTIONS if scope[n] and report["sections"][n]},
        "stats": stats,
    }
    if scope["mood"]:
        view["mood_level"] = report["mood_level"]
    return view


class Store:
    """메모리 저장소.

    - 사용자·날짜당 요약 하나. 보내기 전이면 덮어쓰고, 보낸 뒤면 409 ALREADY_SENT
    - 받은 request_id를 모두 기억한다. 덮어쓰기 전의 request_id로 재시도가 와도
      새 요청으로 처리하지 않고 지금 저장된 결과를 돌려준다
    """

    def __init__(self, clock=lambda: datetime.now(KST)):
        self.clock = clock
        self.lock = threading.Lock()
        self.reports = {}       # (user_id, date) → 요약
        self.by_id = {}         # report_id → (user_id, date)
        self.request_ids = {}   # request_id → (user_id, date)
        self.scopes = {}        # user_id → {schedule, health, mood}

    def _scope(self, user_id):
        return self.scopes.setdefault(user_id, default_scope(user_id))

    @staticmethod
    def _response(report):
        return copy.deepcopy({k: report[k] for k in ("report_id", "shared_sections", "deliveries")})

    @staticmethod
    def _deliveries(user_id, report_date):
        """공통 규칙 "보호자 발송 기록" 형식. 무료 등급은 주 1회 묶어 보내므로 scheduled."""
        if user_id == NO_GUARDIAN_USER_ID:
            return []
        if user_id == FREE_WEEKLY_USER_ID:
            status, scheduled_at = "scheduled", weekly_send_at(date.fromisoformat(report_date))
        else:
            status, scheduled_at = "queued", None
        return [{
            "guardian_id": DEFAULT_GUARDIAN_ID,
            "channel": "push",
            "kind": "report",
            "priority": "normal",
            "status": status,
            "scheduled_at": scheduled_at,
        }]

    def submit(self, body):
        """POST /guardian-reports/daily. (HTTP 상태, 응답 본문)을 돌려준다."""
        with self.lock:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and rid in self.request_ids:
                return 202, self._response(self.reports[self.request_ids[rid]])

            validate(body)
            key = (body["user_id"], body["date"])
            old = self.reports.get(key)
            if old is not None and old["sent_at"] is not None:
                raise ApiError(409, "ALREADY_SENT", "이미 보호자에게 발송한 요약입니다")

            scope = self._scope(body["user_id"])
            sections = {n: (body.get("sections") or {}).get(n) for n in SECTIONS}
            report = {
                "report_id": old["report_id"] if old else f"r_{len(self.by_id) + 1:03d}",
                "user_id": body["user_id"],
                "date": body["date"],
                "brief": body["brief"].strip(),
                "sections": sections,
                "mood_level": body["mood_level"],
                "stats": body.get("stats") or {},
                "generated_at": body["generated_at"],
                "shared_sections": [n for n in SECTIONS if scope[n] and sections[n]],
                "deliveries": self._deliveries(body["user_id"], body["date"]),
                "request_ids": [*(old["request_ids"] if old else []), rid],
                "received_at": self.clock().isoformat(),
                "sent_at": None,
                "sent_scope": None,  # 보낼 때의 공유 범위. 보낸 뒤 보호자 화면은 이것 ∩ 지금 범위
            }
            self.reports[key] = report
            self.by_id[report["report_id"]] = key
            self.request_ids[rid] = key
            return 202, self._response(report)

    def send(self, report_id):
        """mock 전용: 발송 시각이 된 것처럼 보냄 처리. 이후 같은 날짜는 409.

        보낼 때의 공유 범위를 저장해 둔다. 이후 새로 켠 항목은 이 요약에 소급하지 않는다.
        """
        with self.lock:
            key = self.by_id.get(report_id)
            if key is None:
                raise not_found("없는 report_id입니다")
            report = self.reports[key]
            if report["sent_at"] is None:
                report["sent_at"] = self.clock().isoformat()
                report["sent_scope"] = dict(self._scope(report["user_id"]))
                for d in report["deliveries"]:
                    # scheduled_at은 scheduled일 때만 값이 있음 (공통 규칙 "보호자 발송 기록")
                    d.update(status="sent", scheduled_at=None)
            return 200, self._response(report)

    def set_scope(self, user_id, body):
        """mock 전용: 공유 범위 바꾸기.

        이미 받은 요약의 shared_sections는 그대로 둔다. 철회는 보낸 요약의 보호자 화면에도
        바로 반영되고, 새로 켠 항목은 아직 안 보낸 요약에만 반영된다.
        """
        with self.lock:
            require_user(user_id)
            if not isinstance(body, dict) or any(k not in SECTIONS or not isinstance(v, bool) for k, v in body.items()):
                raise invalid("schedule / health / mood 를 true·false로 보내세요")
            self._scope(user_id).update(body)
            return 200, {"user_id": user_id, "share_scope": dict(self.scopes[user_id])}

    def list(self, user_id=None):
        with self.lock:
            out = []
            for r in self.reports.values():
                if user_id not in (None, r["user_id"]):
                    continue
                scope = self._scope(r["user_id"])
                out.append({
                    **copy.deepcopy(r),
                    "share_scope": dict(scope),
                    "push_body": r["brief"],  # 푸시 본문에는 brief만 (잠금 화면에 보일 수 있음)
                    "guardian_view": guardian_view(r, scope),
                })
            return out


def make_handler(store):
    class Handler(MockHandler):
        log_name = "report-mock"
        allowed_methods = ("GET", "POST", "PUT")

        def do_PUT(self):
            self._handle("PUT")

        def route(self, method, path, query, body):
            parts = path.strip("/").split("/")
            if method == "POST" and path == "/guardian-reports/daily":
                return store.submit(body)
            if method == "GET" and path == "/guardian-reports":
                return 200, {"reports": store.list(query.get("user_id", [None])[0])}
            if method == "PUT" and len(parts) == 3 and parts[:2] == ["mock", "share-scope"]:
                return store.set_scope(parts[2], body)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["mock", "guardian-reports"] and parts[3] == "send":
                return store.send(parts[2])
            return super().route(method, path, query, body)

    return Handler


def main():
    run(make_handler(Store()), "하루 요약 수신 API mock (명세 3)", 8048, "하루 요약 mock")


if __name__ == "__main__":
    main()
