from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .contract import InputError
from .demo import generate_demo
from .engine import reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contract-driven migration reconciliation")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("generate-demo", help="Generate synthetic inputs with a known anomaly oracle")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--rows", type=int, default=1000)
    rec = sub.add_parser("reconcile", help="Export audit JSON, escaped HTML and all exceptions")
    rec.add_argument("--source", type=Path, required=True)
    rec.add_argument("--target", type=Path, required=True)
    rec.add_argument("--contract", type=Path, required=True)
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument("--memory-limit", default="256MB")
    bench = sub.add_parser("benchmark", help="Measure the complete SQL reconciliation on synthetic inputs")
    bench.add_argument("--rows", type=int, default=100000)
    bench.add_argument("--out", type=Path, required=True, help="Benchmark JSON destination")
    bench.add_argument("--memory-limit", default="256MB")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-demo":
            expected = generate_demo(args.out, args.rows)
            print(json.dumps({"output": str(args.out), "expected": expected}, indent=2))
            return 0
        if args.command == "reconcile":
            audit = reconcile(args.source, args.target, args.contract, args.out, args.memory_limit)
            print(json.dumps({"run_id": audit["run_id"], "passed": audit["passed"], "comparison": audit["comparison"], "output": str(args.out)}, indent=2))
            return 0 if audit["passed"] else 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="benchmark-") as tmp:
            root = Path(tmp)
            begin = time.perf_counter()
            expected = generate_demo(root / "data", args.rows)
            generation_seconds = time.perf_counter() - begin
            begin = time.perf_counter()
            audit = reconcile(root / "data/source.csv", root / "data/target.csv", root / "data/contract.json", root / "report", args.memory_limit)
            seconds = time.perf_counter() - begin
            oracle = (all(audit["comparison"][k] == v for k, v in expected.items() if k in audit["comparison"])
                      and audit["exception_counts"] == expected["exception_counts"]
                      and audit["source"]["raw_rows"] == expected["source_raw_rows"]
                      and audit["target"]["raw_rows"] == expected["target_raw_rows"])
            if not oracle:
                raise InputError("Benchmark oracle mismatch")
            result = {"measured_at_utc": datetime.now(timezone.utc).isoformat(), "dataset": "deterministic synthetic demo v1",
                      "nominal_rows_per_side": args.rows, "source_rows": audit["source"]["raw_rows"], "target_rows": audit["target"]["raw_rows"],
                      "generation_seconds": round(generation_seconds, 6), "reconciliation_seconds": round(seconds, 6),
                      "total_input_rows_per_second": round((audit["source"]["raw_rows"] + audit["target"]["raw_rows"]) / seconds, 2),
                      "environment": {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(), "logical_cpus": os.cpu_count(), "python": platform.python_version(), "duckdb": duckdb.__version__, "memory_limit": args.memory_limit, "threads": 1},
                      "measured_scope": "CSV parse, disk-backed tables, SQL classify/join, input SHA256, CSV/JSON/HTML report export; excludes fixture generation",
                      "peak_memory_measured": False, "oracle_verified": oracle, "data_quality_passed": audit["passed"], "comparison": audit["comparison"],
                      "limitations": "Single local run with synthetic uniform keys; not a production SLA or proof of bounded process RSS. DuckDB memory_limit does not cap all allocations."}
            args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(result, indent=2))
            return 0
    except (InputError, OSError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
