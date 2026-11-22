"""시니어 말벗 LLM 비교 벤치 (#17)

같은 시나리오를 여러 Claude 모델에 돌려서 응답 품질·첫 토큰 지연·비용을 비교한다.
결과는 results/<시각>.md 로 저장되며, 사람이 직접 읽고 점수를 매기는 용도다.

사용법:
    pip install -r requirements.txt
    set ANTHROPIC_API_KEY=...            (PowerShell: $env:ANTHROPIC_API_KEY="...")
    python bench.py --dry-run            # API 호출 없이 실행 계획만 출력
    python bench.py                      # 전체 모델 × 전체 시나리오
    python bench.py --models claude-haiku-5-5 --only chat-lonely
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import anthropic

HERE = Path(__file__).parent

# 1M 토큰당 USD (Anthropic 공식 단가, 2026-10 기준 — 바뀌면 여기만 고치면 됨)
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

# 정책상 거절(refusal) 시 서버가 다른 모델로 자동 재시도. Haiku 5.5는 미지원.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}

CHAT_SYSTEM = """당신은 65세 이상 어르신의 말벗 AI입니다. 답변은 음성(TTS)으로 읽힙니다.
- 존댓말, 짧고 쉬운 문장으로 2~3문장 이내로 답합니다. 목록·이모지·특수기호는 쓰지 않습니다.
- 먼저 공감하고, 필요할 때만 부담 없는 제안을 합니다. 훈계하지 않습니다.
- 병명이나 진단을 단정하지 않습니다. 건강 걱정에는 "평소와 다른 변화"로 표현하고 병원 상담을 권합니다.
- 대화 속에 날짜·시간이 있는 할 일이 나오면 "할 일로 등록해 드릴까요?"라고 물어봅니다.
- 응답 지연에 민감한 음성 대화입니다. 바로 답변을 시작하세요."""

EXTRACT_SYSTEM = """어르신의 발화에서 정보를 뽑아 JSON 하나만 출력합니다. 설명 문장은 쓰지 않습니다.
오늘 날짜는 {today} 입니다. 상대적인 날짜("내일")는 YYYY-MM-DD로 바꿉니다.
형식:
{{
  "todos": [{{"title": str, "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "repeat": str|null, "kind": "일정"|"복약"}}],
  "interests": [str],
  "risk": {{"type": "없음"|"사기"|"응급", "reason": str|null}}
}}"""


def build_stream(client, model, system, messages, effort):
    kwargs = dict(
        model=model,
        max_tokens=2048,
        system=system,
        messages=messages,
        output_config={"effort": effort},
    )
    if model in FALLBACK_MODELS:
        return client.beta.messages.stream(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    return client.messages.stream(**kwargs)


def run_turn(client, model, system, messages, effort):
    start = time.perf_counter()
    ttft = None
    with build_stream(client, model, system, messages, effort) as stream:
        for _ in stream.text_stream:
            if ttft is None:
                ttft = time.perf_counter() - start
        final = stream.get_final_message()
    total = time.perf_counter() - start

    if final.stop_reason == "refusal":
        text = "[거절됨: refusal]"
    else:
        text = "".join(b.text for b in final.content if b.type == "text").strip()

    usage = final.usage
    in_price, out_price = PRICES[model]
    cost = (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1_000_000
    return {
        "text": text,
        "ttft": ttft or total,
        "total": total,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost": cost,
    }


def run_scenario(client, model, scenario, effort):
    """시나리오의 user 턴을 순서대로 보내며 대화를 이어간다. 턴별 결과 목록을 반환."""
    if scenario["type"] == "chat":
        system = CHAT_SYSTEM
    else:
        system = EXTRACT_SYSTEM.format(today=datetime.now().strftime("%Y-%m-%d"))

    messages, results = [], []
    for user_text in scenario["turns"]:
        messages.append({"role": "user", "content": user_text})
        r = run_turn(client, model, system, messages, effort)
        messages.append({"role": "assistant", "content": r["text"]})
        results.append({"user": user_text, **r})
    return results


def write_report(rows, models, effort):
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.md"

    lines = [f"# LLM 벤치 결과 ({datetime.now():%Y-%m-%d %H:%M}, effort={effort})", ""]

    lines += ["## 모델별 요약", "", "| 모델 | 평균 첫 토큰(초) | 평균 전체(초) | 턴당 평균 비용(USD) | 1,000턴 환산(USD) |", "|---|---|---|---|---|"]
    for m in models:
        turns = [t for row in rows if row["model"] == m for t in row["turns"]]
        if not turns:
            continue
        n = len(turns)
        avg_cost = sum(t["cost"] for t in turns) / n
        lines.append(
            f"| {m} | {sum(t['ttft'] for t in turns) / n:.2f} | {sum(t['total'] for t in turns) / n:.2f} "
            f"| {avg_cost:.5f} | {avg_cost * 1000:.2f} |"
        )

    lines += ["", "## 시나리오별 응답", "", "각 응답 옆 `점수: _/5` 를 직접 채워 주세요.", ""]
    for sid in dict.fromkeys(row["scenario"]["id"] for row in rows):
        scenario = next(row["scenario"] for row in rows if row["scenario"]["id"] == sid)
        lines += [f"### {sid}", f"> 확인할 점: {scenario['note']}", ""]
        for row in (r for r in rows if r["scenario"]["id"] == sid):
            lines.append(f"**{row['model']}** — 점수: _/5")
            for t in row["turns"]:
                lines.append(f"- 🧓 {t['user']}")
                lines.append(f"- 🤖 {t['text']}  `({t['ttft']:.2f}s / {t['output_tokens']}tok)`")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    (path.with_suffix(".json")).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=list(PRICES), choices=list(PRICES))
    parser.add_argument("--only", nargs="+", help="실행할 시나리오 id")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"],
                        help="음성 대화는 지연이 중요해서 기본 low")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scenarios = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in args.only]

    n_calls = sum(len(s["turns"]) for s in scenarios) * len(args.models)
    print(f"모델 {len(args.models)}개 × 시나리오 {len(scenarios)}개 = API 호출 {n_calls}회 (effort={args.effort})")
    if args.dry_run:
        for s in scenarios:
            print(f"  - {s['id']} [{s['type']}] 턴 {len(s['turns'])}개")
        return

    client = anthropic.Anthropic()
    rows = []
    for model in args.models:
        for s in scenarios:
            print(f"[{model}] {s['id']} ...", end=" ", flush=True)
            try:
                turns = run_scenario(client, model, s, args.effort)
            except anthropic.APIStatusError as e:
                print(f"실패 ({e.status_code}: {e.message})")
                continue
            except anthropic.APIConnectionError as e:
                print(f"연결 실패 ({e})")
                continue
            print(f"{turns[-1]['ttft']:.2f}s")
            rows.append({"model": model, "scenario": s, "turns": turns})

    path = write_report(rows, args.models, args.effort)
    print(f"\n결과: {path}")


if __name__ == "__main__":
    main()
