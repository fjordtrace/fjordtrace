"""One command: ``signalpost --input <batch> --output-dir out``.

Every limit can be set by flag or environment variable so the evaluator's official budget can be
applied without code changes. Starter-kit flag names (--organisations, --output, --report) also work.
"""
from __future__ import annotations

import argparse
import os
import sys


def _env(name: str, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return type(default)(v) if default is not None else v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="signalpost", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", "--organisations", dest="input", default=_env("SIGNALPOST_INPUT", None),
                   help="batch of organisation numbers: JSONL, JSON, CSV or text (one per line)")
    p.add_argument("--output-dir", default=_env("SIGNALPOST_OUTPUT_DIR", "out"))
    p.add_argument("--output", default=_env("SIGNALPOST_OUTPUT", None), help="envelopes JSONL path (default <output-dir>/envelopes.jsonl)")
    p.add_argument("--report", default=_env("SIGNALPOST_REPORT", None), help="run report path (default <output-dir>/run-report.json)")
    p.add_argument("--profiles-output", default=None, help=argparse.SUPPRESS)  # starter-kit compatibility; envelopes are the profiles
    p.add_argument("--bulk", "--registry-snapshot", dest="bulk", default=_env("SIGNALPOST_BULK", None),
                   help="Brønnøysund bulk entity snapshot (CSV/CSV.gz/JSON). Optional: live registry is used otherwise")
    p.add_argument("--state-dir", default=_env("SIGNALPOST_STATE_DIR", "state"), help="refresh history (previous snapshots)")
    p.add_argument("--previous", default=_env("SIGNALPOST_PREVIOUS", None), help="diff against an earlier envelopes.jsonl instead")
    p.add_argument("--run-id", default=_env("SIGNALPOST_RUN_ID", None))
    p.add_argument("--time-budget-min", type=float, default=_env("SIGNALPOST_TIME_BUDGET_MIN", 45.0))
    p.add_argument("--max-requests", type=int, default=_env("SIGNALPOST_MAX_REQUESTS", 0), help="0 = requests-per-company x companies")
    p.add_argument("--requests-per-company", type=float, default=_env("SIGNALPOST_REQUESTS_PER_COMPANY", 18.0))
    p.add_argument("--soft-stop", type=float, default=_env("SIGNALPOST_SOFT_STOP", 0.85), help="fraction of time after which optional work stops")
    p.add_argument("--workers", type=int, default=_env("SIGNALPOST_WORKERS", 16))
    p.add_argument("--connectors", default=_env("SIGNALPOST_CONNECTORS", None), help="comma list to enable (default all)")
    p.add_argument("--tight", action="store_true", default=os.environ.get("SIGNALPOST_TIGHT", "").lower() in ("1", "true", "yes"),
                   help="use the tight request plan even when the cap would allow a full run")
    p.add_argument("--roles-bulk", default=_env("SIGNALPOST_ROLES_BULK", None),
                   help="local copy of Brønnøysund's roller/totalbestand (json.gz); tight plan downloads it otherwise")
    p.add_argument("--subunits-bulk", default=_env("SIGNALPOST_SUBUNITS_BULK", None),
                   help="local copy of Brønnøysund's underenheter CSV (csv.gz); tight plan downloads it otherwise")
    p.add_argument("--offline-registry", action="store_true", help="use only the bulk snapshot for identity (no live entity call)")
    p.add_argument("--no-html", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--expected-count", type=int, default=None, help="fail loudly if the input count differs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input:
        print("signalpost: --input is required", file=sys.stderr)
        return 2
    from .pipeline import Pipeline

    pipe = Pipeline(args)
    if args.expected_count is not None and args.expected_count != len(pipe.inputs):
        print(f"signalpost: warning: expected {args.expected_count} inputs, got {len(pipe.inputs)}", file=sys.stderr)
    pipe.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
