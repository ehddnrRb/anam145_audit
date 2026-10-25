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
EFFORTS = ("high", "xhigh", "max")  # scan.py EFFORTS 와 같아야 한다 (tests/test_cli.py 가 맞춰 본다). max 는 D-28
MAX_TOKENS_LIMIT = 128000  # scan.py MAX_TOKENS_LIMIT 와 같아야 한다(모델 출력 상한, D-28. 시험이 맞춰 본다)


def _max_tokens(s: str) -> int:
    """--max-tokens 값: 1~MAX_TOKENS_LIMIT 의 정수. 범위 밖이면 argparse 오류(exit 2)로 요청 전에 막는다."""
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수여야 한다: {s!r}")
    if not 1 <= v <= MAX_TOKENS_LIMIT:
        raise argparse.ArgumentTypeError(f"1~{MAX_TOKENS_LIMIT} 이어야 한다: {v}")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval", description="AttackTrail 평가 하네스")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="에이전트 실행 + 채점 + 리포트")
    r.add_argument("--agent", required=True, choices=["attacktrail", "attacktrail-cli", "baseline"])
    r.add_argument("--dataset", required=True, choices=["scabench", "holdout"])
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--projects", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    r.add_argument("--suite")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--timeout", type=int, default=None,
                   help="프로젝트당 에이전트 제한 시간(초). 생략하면 에이전트 기본값: attacktrail 10800, 그 밖 1800 (D-24)")
    r.add_argument("--judge", choices=["nethermind", "mock"], default="nethermind")
    r.add_argument("--judge-model", default="o4-mini")
    r.add_argument("--baseline-mode", choices=["precomputed", "live"], default=None)
    r.add_argument("--model", default=None)
    r.add_argument("--effort", choices=EFFORTS, default=None,
                   help="scan.py --effort. --agent attacktrail 에서만. 생략하면 scan.py 기본값(xhigh)")
    r.add_argument("--max-tokens", type=_max_tokens, default=None, metavar="N",
                   help=f"scan.py --max-tokens(1~{MAX_TOKENS_LIMIT}). --agent attacktrail 에서만. 생략하면 scan.py 기본값(64000)")
    r.add_argument("--i-understand-holdout", action="store_true")

    s = sub.add_parser("rescore", help="에이전트 단계는 끝났는데 채점이 끊긴 run 마무리 (에이전트 재실행 없음)")
    s.add_argument("run", type=Path)
    s.add_argument("--agent", required=True, choices=["attacktrail", "attacktrail-cli", "baseline"])
    s.add_argument("--dataset", required=True, choices=["scabench"])
    s.add_argument("--projects", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    s.add_argument("--suite")
    s.add_argument("--judge", choices=["nethermind", "mock"], default="nethermind")
    s.add_argument("--judge-model", default="o4-mini")
    s.add_argument("--model", default=None, help="manifest 기록용. 원래 run 의 --model 과 같게")
    s.add_argument("--effort", choices=EFFORTS, default=None, help="manifest 기록용. 원래 run 의 --effort 와 같게")
    s.add_argument("--max-tokens", type=_max_tokens, default=None, metavar="N",
                   help="manifest 기록용. 원래 run 의 --max-tokens 와 같게")

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
    if args.effort:
        if args.agent != "attacktrail":
            raise SystemExit("--effort 는 --agent attacktrail 에서만 쓴다. CLI 어댑터는 effort 를 보내지 않는다.")
        opts["effort"] = args.effort
    if args.max_tokens:
        if args.agent != "attacktrail":
            raise SystemExit("--max-tokens 는 --agent attacktrail 에서만 쓴다. CLI 어댑터는 max_tokens 를 보내지 않는다.")
        opts["max_tokens"] = args.max_tokens
    if getattr(args, "baseline_mode", None):   # rescore 에는 이 플래그가 없다
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
    if args.repeat < 1 or args.jobs < 1 or (args.timeout is not None and args.timeout < 1):
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


def cmd_rescore(args) -> int:
    from runner.run import rescore

    try:
        out = rescore(args.run, args.agent, args.dataset, projects=args.projects, suite=args.suite,
                      judge=args.judge, judge_model=args.judge_model, agent_opts=_agent_opts(args))
    except (RuntimeError, ValueError, FileNotFoundError, FileExistsError) as e:
        print(f"재채점 중단: {e}", file=sys.stderr)
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
    return {"run": cmd_run, "rescore": cmd_rescore, "compare": cmd_compare, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
