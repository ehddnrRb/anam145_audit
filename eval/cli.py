"""eval 하네스 진입점. eval/ 에서 `./eval run ...` 또는 `python cli.py run ...` 로 실행한다."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

HOLDOUT_WARNING = (
    "holdout 셋은 마일스톤 평가용입니다. 튜닝에 쓰면 오염됩니다. "
    "진행하려면 --i-understand-holdout 을 붙이세요."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval", description="AttackTrail 평가 하네스")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="에이전트 실행 + 채점 + 리포트")
    r.add_argument("--agent", required=True, choices=["attacktrail", "baseline"])
    r.add_argument("--dataset", required=True, choices=["scabench", "holdout"])
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--projects", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    r.add_argument("--suite")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--judge", choices=["nethermind", "mock"], default="nethermind")
    r.add_argument("--judge-model", default="o4-mini")
    r.add_argument("--baseline-mode", choices=["precomputed", "live"], default=None)
    r.add_argument("--model", default=None)
    r.add_argument("--i-understand-holdout", action="store_true")

    c = sub.add_parser("compare", help="두 run 비교")
    c.add_argument("run_a", type=Path)
    c.add_argument("run_b", type=Path)

    d = sub.add_parser("render", help="run 디렉터리의 리포트 재생성")
    d.add_argument("run", type=Path)
    return p


def _agent_opts(args) -> dict:
    opts = {}
    if args.model:
        opts["model"] = args.model
    if args.baseline_mode:
        if args.agent != "baseline":
            raise SystemExit("--baseline-mode 는 --agent baseline 에서만 쓴다.")
        opts["mode"] = args.baseline_mode
    return opts


def _judge_kind(run_dir: Path) -> str | None:
    f = run_dir / "scores.json"
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8")).get("judge")


def cmd_run(args) -> int:
    if args.dataset == "holdout":
        if not args.i_understand_holdout:
            print(HOLDOUT_WARNING)
            return 2
        print(HOLDOUT_WARNING, file=sys.stderr)
    if args.repeat < 1 or args.jobs < 1 or args.timeout < 1:
        print("--repeat, --jobs, --timeout 은 1 이상이어야 한다.", file=sys.stderr)
        return 2
    from runner.run import run

    try:
        out = run(
            args.agent, args.dataset, args.out,
            projects=args.projects, suite=args.suite, repeat=args.repeat,
            jobs=args.jobs, timeout_s=args.timeout, judge=args.judge,
            judge_model=args.judge_model, agent_opts=_agent_opts(args),
            allow_holdout=args.i_understand_holdout,
        )
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"실행 중단: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def cmd_compare(args) -> int:
    ka, kb = _judge_kind(args.run_a), _judge_kind(args.run_b)
    if (ka == "mock") != (kb == "mock") and ka and kb:
        print(f"비교 거부: 한쪽만 mock judge 다 (A={ka}, B={kb}).", file=sys.stderr)
        return 1
    from report.compare import compare

    try:
        print(compare(args.run_a, args.run_b))
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"비교 거부: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_render(args) -> int:
    from report.render import render

    html, md = render(args.run)
    print(html)
    print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"run": cmd_run, "compare": cmd_compare, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
