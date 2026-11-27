"""사기 감지 테스트 세트 평가 (#25 완료 조건: 감지율·오탐률).

    cd server
    pip install -r requirements.txt
    python -m companion.safety.eval                      # samples/fraud_cases.json, Claude Haiku 5.5
    python -m companion.safety.eval --show-misses        # 틀린 문장의 id와 결과

실제 Claude를 부르므로 ANTHROPIC_API_KEY가 필요하고 비용이 든다 (문장 50개 안팎, Haiku라 몇 센트 수준).
CI에서는 돌리지 않는다. 문지기(규칙)만 보는 검사는 tests/test_safety.py가 CI에서 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .detect import DEFAULT_MODEL, ClaudeFraudClassifier, FraudDetector

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "fraud_cases.json"


def load_cases(path: Path = SAMPLES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def outcome(detection) -> str:
    if detection is None:
        return "none"
    return "alert" if detection.should_alert else "refused"


def evaluate(cases: list[dict], detector: FraudDetector) -> dict:
    rows = []
    for case in cases:
        d = detector.detect(case["text"], recent=tuple(case.get("recent", ())))
        rows.append({
            "id": case["id"],
            "expect": case["expect"],
            "got": outcome(d),
            "category_ok": d is None or case.get("category") in (None, d.category),
            "severity": d.severity if d is not None and d.should_alert else None,
        })
    by_expect = Counter(r["expect"] for r in rows)
    alerts = [r for r in rows if r["expect"] == "alert"]
    negatives = [r for r in rows if r["expect"] == "none"]
    refused = [r for r in rows if r["expect"] == "refused"]
    detected = [r for r in alerts if r["got"] == "alert"]
    return {
        "counts": dict(by_expect),
        "detection_rate": len(detected) / len(alerts) if alerts else None,
        "false_positive_rate": sum(r["got"] == "alert" for r in negatives) / len(negatives) if negatives else None,
        "refused_ok": sum(r["got"] == "refused" for r in refused) / len(refused) if refused else None,
        "refused_alerted": sum(r["got"] == "alert" for r in refused),
        "category_accuracy": sum(r["category_ok"] for r in detected) / len(detected) if detected else None,
        "severity": dict(Counter(r["severity"] for r in detected)),
        "misses": [r for r in rows if r["got"] != r["expect"] or not r["category_ok"]],
    }


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def main() -> int:
    ap = argparse.ArgumentParser(description="사기 감지 테스트 세트 평가 (#25)")
    ap.add_argument("--cases", type=Path, default=SAMPLES)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()

    detector = FraudDetector(ClaudeFraudClassifier(model=args.model, effort=args.effort))
    report = evaluate(load_cases(args.cases), detector)
    print(f"모델 {args.model} (effort {args.effort}), 문장 수 {report['counts']}")
    print(f"감지율 (알려야 할 문장 중 알림): {_pct(report['detection_rate'])}")
    print(f"오탐률 (사기 아닌 문장 중 알림): {_pct(report['false_positive_rate'])}")
    print(f"거절한 사기를 칭찬으로 처리: {_pct(report['refused_ok'])} (알림으로 처리 {report['refused_alerted']}개)")
    print(f"유형 맞춤 (감지한 것 중): {_pct(report['category_accuracy'])}")
    print(f"severity: {report['severity']}")
    if args.show_misses:
        for r in report["misses"]:  # 문장 id만. 원문은 samples 파일에서 확인
            print(f"  {r['id']}: 기대 {r['expect']} / 결과 {r['got']}{'' if r['category_ok'] else ' (유형 다름)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
