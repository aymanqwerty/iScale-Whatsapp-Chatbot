"""Command-line runner for the prompt eval.

    python -m evals.run                      # score the current prompt
    python -m evals.run --version v1_initial # score the first prompt
    python -m evals.run --compare            # v1_initial vs current, side by side
    python -m evals.run --only fees          # just one category
    python -m evals.run --save-baseline      # freeze today's pass/fail as the bar
    python -m evals.run --against-baseline    # fail (exit 1) if any case regressed

The three questions it answers map onto the three things the assignment asks of
an eval: what the system does now (a plain run), whether a change helped or hurt
(``--compare``), and whether a change quietly made something worse
(``--against-baseline``, the regression gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from evals.checks import CheckResult, run_check
from evals.pipeline import CaseOutput, Harness, run_case
from evals.prompt_versions import KNOWN_VERSIONS, load_version
from evals.schema import Case, load_cases

DEFAULT_BASELINE = Path(__file__).resolve().parent / "baseline.json"
_REPLY_PREVIEW = 240


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CaseScore:
    case: Case
    output: CaseOutput
    checks: list[CheckResult]

    @property
    def errored(self) -> bool:
        return self.output.error is not None

    @property
    def passed(self) -> bool:
        return not self.errored and all(c.passed for c in self.checks)

    @property
    def status(self) -> str:
        if self.errored:
            return "ERR"
        return "PASS" if self.passed else "FAIL"


@dataclass(slots=True)
class VersionRun:
    version: str
    scores: list[CaseScore]

    def by_id(self) -> dict[str, CaseScore]:
        return {s.case.id: s for s in self.scores}


async def _score_case(
    case: Case,
    *,
    harness: Harness,
    version: str,
    module: ModuleType,
    use_cache: bool,
    sem: asyncio.Semaphore,
) -> CaseScore:
    async with sem:
        output = await run_case(
            case, harness=harness, version=version, module=module, use_cache=use_cache
        )
        if output.error is not None:
            # No point scoring checks against an empty reply - the instrument
            # failed, not the bot. Report it as ERR and move on.
            return CaseScore(case=case, output=output, checks=[])
        try:
            results = [
                await run_check(
                    spec,
                    output,
                    case=case,
                    judge_llm=harness.judge_llm,
                    model_label=harness.model_label,
                )
                for spec in case.checks
            ]
        except (ConfigurationError, LLMError) as exc:
            # A judge-model failure (rate limit, outage) is an instrument fault,
            # not a failing reply. Mark the case ERR so it is excluded from the
            # score rather than silently counted as a FAIL - and so one 429 can
            # never crash the whole run the way it did before this guard.
            output.error = f"judge model error: {exc}"
            return CaseScore(case=case, output=output, checks=[])
    return CaseScore(case=case, output=output, checks=results)


async def score_version(
    version: str,
    cases: list[Case],
    *,
    harness: Harness,
    use_cache: bool,
    concurrency: int,
) -> VersionRun:
    module = load_version(version)
    sem = asyncio.Semaphore(concurrency)
    scores = await asyncio.gather(
        *(
            _score_case(
                c,
                harness=harness,
                version=version,
                module=module,
                use_cache=use_cache,
                sem=sem,
            )
            for c in cases
        )
    )
    # Preserve case (file) order rather than completion order.
    order = {c.id: i for i, c in enumerate(cases)}
    return VersionRun(version=version, scores=sorted(scores, key=lambda s: order[s.case.id]))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _preview(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _REPLY_PREVIEW:
        return collapsed
    return collapsed[:_REPLY_PREVIEW] + " ..."


def print_run(run: VersionRun, *, harness: Harness, verbose: bool) -> None:
    module = load_version(run.version)
    print()
    print("iScale receptionist - prompt output eval")
    print(
        f"model: {harness.model_label}   prompt: {run.version}   "
        f"cases: {len(run.scores)}"
    )
    print(f"  ({getattr(module, 'DESCRIPTION', '')})")
    print("-" * 72)

    for score in run.scores:
        case = score.case
        print(f"[{score.status:4}] {case.category}/{case.id}")
        if score.errored:
            print(f"        ! {score.output.error}")
            continue
        if not score.passed or verbose:
            for result in score.checks:
                if result.passed and not verbose:
                    continue
                mark = "ok" if result.passed else "XX"
                print(f"        [{mark}] {result.kind}: {result.detail}")
            if not score.output.refused:
                print(f'        reply: "{_preview(score.output.text)}"')
        if verbose and case.notes:
            print(f"        why: {case.notes}")

    print("-" * 72)
    _print_rollup(run)


def _category_rollup(run: VersionRun) -> dict[str, tuple[int, int]]:
    """category -> (passed, scored) where scored excludes errored cases."""
    rollup: dict[str, list[int]] = {}
    for score in run.scores:
        bucket = rollup.setdefault(score.case.category, [0, 0])
        if score.errored:
            continue
        bucket[1] += 1
        if score.passed:
            bucket[0] += 1
    return {k: (v[0], v[1]) for k, v in rollup.items()}


def _print_rollup(run: VersionRun) -> None:
    rollup = _category_rollup(run)
    print("by category:")
    for category in sorted(rollup):
        passed, scored = rollup[category]
        print(f"  {category:14} {passed}/{scored}")
    total_pass = sum(1 for s in run.scores if s.passed)
    total_scored = sum(1 for s in run.scores if not s.errored)
    errored = sum(1 for s in run.scores if s.errored)
    pct = (100 * total_pass / total_scored) if total_scored else 0.0
    suffix = f"   ({errored} errored - model not reachable)" if errored else ""
    print("-" * 72)
    print(f"TOTAL  {total_pass}/{total_scored} passed  ({pct:.1f}%){suffix}")


def print_compare(older: VersionRun, newer: VersionRun) -> int:
    """Side-by-side of two runs. Returns the number of regressions."""
    a, b = older.by_id(), newer.by_id()
    ids = [s.case.id for s in newer.scores]

    cats_a = _category_rollup(older)
    cats_b = _category_rollup(newer)
    print()
    print(f"compare: {older.version}  ->  {newer.version}   ({len(ids)} cases)")
    print("-" * 72)
    print(f"  {'category':14} {older.version:>12} {newer.version:>12}")
    for category in sorted(set(cats_a) | set(cats_b)):
        pa = f"{cats_a.get(category, (0, 0))[0]}/{cats_a.get(category, (0, 0))[1]}"
        pb = f"{cats_b.get(category, (0, 0))[0]}/{cats_b.get(category, (0, 0))[1]}"
        print(f"  {category:14} {pa:>12} {pb:>12}")
    print("-" * 72)

    fixed, regressed, errored = [], [], []
    for cid in ids:
        sa, sb = a.get(cid), b.get(cid)
        if sa is None or sb is None:
            continue
        if sa.errored or sb.errored:
            errored.append(cid)
            continue
        if not sa.passed and sb.passed:
            fixed.append(cid)
        elif sa.passed and not sb.passed:
            regressed.append(cid)

    print(f"FIXED    (fail -> pass): {', '.join(fixed) if fixed else '(none)'}")
    print(f"REGRESSED (pass -> fail): {', '.join(regressed) if regressed else '(none)'}")
    if errored:
        print(f"skipped (model error):   {', '.join(errored)}")
    print("-" * 72)
    if regressed:
        print(f"!! {len(regressed)} case(s) got worse under {newer.version}.")
    else:
        print(f"no regressions: {newer.version} is >= {older.version} on every case.")
    return len(regressed)


# --------------------------------------------------------------------------- #
# Baseline (the regression gate)
# --------------------------------------------------------------------------- #
def save_baseline(run: VersionRun, path: Path, *, model: str) -> None:
    payload = {
        "version": run.version,
        "model": model,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": {s.case.id: s.passed for s in run.scores if not s.errored},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nbaseline saved: {path}  ({len(payload['cases'])} cases)")


def gate_against_baseline(run: VersionRun, path: Path) -> int:
    if not path.is_file():
        print(f"no baseline at {path} - run --save-baseline first.", file=sys.stderr)
        return 2
    baseline = json.loads(path.read_text(encoding="utf-8"))
    prior: dict[str, bool] = baseline.get("cases", {})
    current = run.by_id()

    regressed, newly_passing, missing, errored = [], [], [], []
    for cid, was_passing in prior.items():
        score = current.get(cid)
        if score is None:
            missing.append(cid)
            continue
        if score.errored:
            errored.append(cid)
            continue
        if was_passing and not score.passed:
            regressed.append(cid)
        elif not was_passing and score.passed:
            newly_passing.append(cid)

    print()
    print(f"regression gate vs baseline ({baseline.get('version')}, {baseline.get('generated')})")
    print("-" * 72)
    print(f"regressed (was passing, now failing): {', '.join(regressed) if regressed else '(none)'}")
    if newly_passing:
        print(f"newly passing: {', '.join(newly_passing)}")
    if missing:
        print(f"in baseline but not run: {', '.join(missing)}")
    if errored:
        print(f"skipped (model error): {', '.join(errored)}")
    print("-" * 72)
    if regressed:
        print(f"GATE FAILED: {len(regressed)} case(s) regressed against the baseline.")
        return 1
    print("GATE PASSED: nothing that was passing has started failing.")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _select_cases(cases: list[Case], only: str | None, limit: int | None) -> list[Case]:
    if only:
        cases = [c for c in cases if c.category == only]
        if not cases:
            raise SystemExit(f"no cases in category {only!r}")
    if limit:
        cases = cases[:limit]
    return cases


async def _amain(args: argparse.Namespace) -> int:
    cases = _select_cases(load_cases(), args.only, args.limit)
    harness = Harness.build(Settings())
    use_cache = not args.no_cache

    if args.compare:
        older, newer = args.compare
        run_old = await score_version(
            older, cases, harness=harness, use_cache=use_cache, concurrency=args.concurrency
        )
        run_new = await score_version(
            newer, cases, harness=harness, use_cache=use_cache, concurrency=args.concurrency
        )
        regressions = print_compare(run_old, run_new)
        return 1 if regressions else 0

    run = await score_version(
        args.version, cases, harness=harness, use_cache=use_cache, concurrency=args.concurrency
    )
    print_run(run, harness=harness, verbose=args.verbose)

    if args.save_baseline is not None:
        save_baseline(run, args.save_baseline, model=harness.model_label)
    if args.against_baseline is not None:
        return gate_against_baseline(run, args.against_baseline)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description="Score the WhatsApp bot's prompt output."
    )
    parser.add_argument(
        "--version", default="current", help="prompt version to score (default: current)"
    )
    parser.add_argument(
        "--compare",
        nargs="*",
        metavar="VERSION",
        help="compare two versions; with no names, uses the known pair "
        f"({' -> '.join(KNOWN_VERSIONS)})",
    )
    parser.add_argument("--only", help="run only this category")
    parser.add_argument("--limit", type=int, help="run at most N cases")
    parser.add_argument("--no-cache", action="store_true", help="ignore the reply cache")
    parser.add_argument(
        "--concurrency", type=int, default=5, help="max concurrent model calls (default: 5)"
    )
    parser.add_argument("--verbose", action="store_true", help="show passing checks and notes")
    # nargs="?" with const: absent -> None (skip); bare flag -> DEFAULT_BASELINE;
    # flag with a path -> that path. No post-processing needed.
    parser.add_argument(
        "--save-baseline",
        nargs="?",
        type=Path,
        const=DEFAULT_BASELINE,
        default=None,
        help=f"freeze pass/fail to a file (default: {DEFAULT_BASELINE.name})",
    )
    parser.add_argument(
        "--against-baseline",
        nargs="?",
        type=Path,
        const=DEFAULT_BASELINE,
        default=None,
        help="exit 1 if any case regressed against the baseline file",
    )
    args = parser.parse_args(argv)
    if args.compare is not None and len(args.compare) == 0:
        args.compare = list(KNOWN_VERSIONS)
    if args.compare is not None and len(args.compare) != 2:
        parser.error("--compare takes exactly two version names (or none for the default pair)")
    return args


def _force_utf8_stdout() -> None:
    """Make stdout/stderr UTF-8 so emoji in replies don't crash on Windows.

    The discovery pitches use emoji bullets, and the default Windows console
    codec (cp1252) raises UnicodeEncodeError the moment one is printed. Reconfigure
    to UTF-8 and replace anything unencodable rather than die mid-report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - best effort
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
