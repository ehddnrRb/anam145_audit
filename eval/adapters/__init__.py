from adapters.base import Agent


def get_agent(name: str, **opts) -> Agent:
    """names: "attacktrail", "attacktrail-cli", "baseline". None-valued opts are dropped so callers can
    pass CLI flags straight through; "mode" only applies to baseline."""
    opts = {k: v for k, v in opts.items() if v is not None}
    if name == "attacktrail":
        from adapters.attacktrail import AttackTrailAgent
        opts.pop("mode", None)
        return AttackTrailAgent(**opts)
    if name == "attacktrail-cli":
        from adapters.attacktrail_cli import AttackTrailCLIAgent
        opts.pop("mode", None)
        return AttackTrailCLIAgent(**opts)
    if name == "baseline":
        from adapters.baseline_llm import BaselineAgent
        return BaselineAgent(**opts)
    raise ValueError("unknown agent %r (expected attacktrail, attacktrail-cli or baseline)" % name)
