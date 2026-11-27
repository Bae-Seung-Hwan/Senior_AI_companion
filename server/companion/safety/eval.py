"""사기(#25)·응급(#26) 감지 테스트 세트 평가 (완료 조건: 감지율·오탐률).

    cd server
    pip install -r requirements.txt
    python -m companion.safety.eval                         # 사기: samples/fraud_cases.json
    python -m companion.safety.eval --kind emergency        # 응급: samples/emergency_cases.json
    python -m companion.safety.eval --show-misses           # 틀린 문장의 id와 결과

실제 Claude Haiku 5.5를 부르므로 ANTHROPIC_API_KEY가 필요하고 비용이 든다 (문장 50개 안팎, 몇 센트 수준).
CI에서는 돌리지 않는다. 문지기(규칙)만 보는 검사는 tests/test_safety.py가 CI에서 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .detect import (
    DEFAULT_MODEL,
    ClaudeEmergencyClassifier,
    ClaudeFraudClassifier,
    EmergencyDetector,
    FraudDetector,
)

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
SAMPLES = SAMPLES_DIR / "fraud_cases.json"
EMERGENCY_SAMPLES = SAMPLES_DIR / "emergency_cases.json"
KINDS = {
    "fraud": (SAMPLES, ClaudeFraudClassifier, FraudDetector),
    "emergency": (EMERGENCY_SAMPLES, ClaudeEmergencyClassifier, EmergencyDetector),
}
# 감지는 했지만 알리지 않아야 하는 것: 사기를 이미 거절함 / 응급이 이미 지나감
QUIET = ("refused", "past")


def load_cases(path: Path = SAMPLES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def outcome(detection) -> str:
    if detection is None:
        return "none"
    return "alert" if detection.should_alert else "quiet"


def evaluate(cases: list[dict], detector) -> dict:
    rows = []
    for case in cases:
        d = detector.detect(case["text"], recent=tuple(case.get("recent", ())))
        expect = "quiet" if case["expect"] in QUIET else case["expect"]
        rows.append({
            "id": case["id"],
            "expect": expect,
            "got": outcome(d),
            "category_ok": d is None or case.get("category") in (None, d.category),
            "severity": d.severity if d is not None and d.should_alert else None,
        })
    alerts = [r for r in rows if r["expect"] == "alert"]
    negatives = [r for r in rows if r["expect"] == "none"]
    quiet = [r for r in rows if r["expect"] == "quiet"]
    detected = [r for r in alerts if r["got"] == "alert"]
    return {
        "counts": dict(Counter(r["expect"] for r in rows)),
        "detection_rate": len(detected) / len(alerts) if alerts else None,
        "false_positive_rate": sum(r["got"] == "alert" for r in negatives) / len(negatives) if negatives else None,
        # 알리지 않기만 하면 맞음 ("이미 지남"으로 보든 "아님"으로 보든 보호자에게 가지 않음)
        "quiet_ok": sum(r["got"] != "alert" for r in quiet) / len(quiet) if quiet else None,
        "quiet_alerted": sum(r["got"] == "alert" for r in quiet),
        "category_accuracy": sum(r["category_ok"] for r in detected) / len(detected) if detected else None,
        "severity": dict(Counter(r["severity"] for r in detected)),
        "misses": [r for r in rows if not _same(r) or not r["category_ok"]],
    }


def _same(row) -> bool:
    if row["expect"] == "quiet":
        return row["got"] != "alert"
    return row["got"] == row["expect"]


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def main() -> int:
    ap = argparse.ArgumentParser(description="사기·응급 감지 테스트 세트 평가 (#25, #26)")
    ap.add_argument("--kind", choices=sorted(KINDS), default="fraud")
    ap.add_argument("--cases", type=Path, help="기본: 종류별 samples 파일")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()

    default_cases, classifier_cls, detector_cls = KINDS[args.kind]
    detector = detector_cls(classifier_cls(model=args.model, effort=args.effort))
    report = evaluate(load_cases(args.cases or default_cases), detector)
    quiet_label = "거절한 사기" if args.kind == "fraud" else "이미 지난 응급"
    print(f"{args.kind} / 모델 {args.model} (effort {args.effort}), 문장 수 {report['counts']}")
    print(f"감지율 (알려야 할 문장 중 알림): {_pct(report['detection_rate'])}")
    print(f"오탐률 (아닌 문장 중 알림): {_pct(report['false_positive_rate'])}")
    print(f"{quiet_label}을 알리지 않음: {_pct(report['quiet_ok'])} (알림으로 처리 {report['quiet_alerted']}개)")
    print(f"유형 맞춤 (감지한 것 중): {_pct(report['category_accuracy'])}")
    print(f"severity: {report['severity']}")
    if args.show_misses:
        for r in report["misses"]:  # 문장 id만. 원문은 samples 파일에서 확인
            print(f"  {r['id']}: 기대 {r['expect']} / 결과 {r['got']}{'' if r['category_ok'] else ' (유형 다름)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
