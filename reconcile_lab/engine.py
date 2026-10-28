"""Disk-backed SQL reconciliation. No complete input or exception table enters Python memory."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

from .contract import InputError, load_contract, quote_identifier as qi, quote_literal as ql, validate_memory_limit


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inspect_csv(path: Path, required: list[str]) -> None:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if not header or len(header) != len(set(n.lower() for n in header)):
            raise InputError(f"{path.name}: empty or duplicate CSV header")
        if any(n.startswith("_") for n in header):
            raise InputError(f"{path.name}: reserved column name starting with _")
        missing = sorted(set(required) - set(header))
        if missing:
            raise InputError(f"{path.name}: missing required columns {missing}")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputError(f"Cannot read {path.name}: {exc}") from exc


def canonical(column: str, contract: dict, trim: bool = True) -> str:
    value = f"trim({qi(column)})" if trim else qi(column)
    tokens = contract["null_tokens"]
    null_test = " OR ".join(f"{value} = {ql(t)}" for t in tokens) or "false"
    return f"CASE WHEN {value} IS NULL OR {null_test} THEN NULL ELSE {value} END"


def key_json(alias: str, contract: dict) -> str:
    args = []
    for i, name in enumerate(contract["keys"]["columns"]):
        args.extend([ql(name), f"{alias}._k{i}"])
    return "json_object(" + ", ".join(args) + ")"


def join_keys(left: str, right: str, contract: dict) -> str:
    return " AND ".join(f"{left}._k{i} = {right}._k{i}" for i in range(len(contract["keys"]["columns"])))


def scalar(con, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def load_side(con, side: str, path: Path, contract: dict) -> dict:
    # all_varchar preserves identifiers such as 000012. force_not_null leaves
    # null semantics of every compared column to the contract.
    required = contract['keys']['columns'] + [f['name'] for f in contract['fields']]
    not_null = '[' + ', '.join(ql(n) for n in required) + ']'
    con.execute(
        f"CREATE TABLE {side}_raw AS SELECT row_number() OVER () AS _row_id, * "
        f"FROM read_csv(?, header=true, all_varchar=true, force_not_null={not_null}, strict_mode=true, parallel=false)",
        [str(path)],
    )
    values = ["_row_id"]
    for i, name in enumerate(contract["keys"]["columns"]):
        value = canonical(name, contract, contract["keys"].get("trim", True))
        if contract["keys"].get("case", "none") == "upper":
            value = f"upper({value})"
        values.append(f"{value} AS _k{i}")
    for i, field in enumerate(contract["fields"]):
        raw = canonical(field["name"], contract, field.get("trim", True))
        values.append(f"{raw} AS _raw{i}")
        if field["type"] == "decimal":
            scale = field["scale"]
            regex = r"[+-]?[0-9]+" + (rf"(\.[0-9]{{1,{scale}}})?" if scale else "")
            cast = f"try_cast({raw} AS DECIMAL(28,{scale}))"
            valid = f"({raw} IS NULL OR (regexp_full_match({raw}, {ql(regex)}) AND {cast} IS NOT NULL))"
            values.extend([f"{cast} AS _v{i}", f"NOT {valid} AS _bad{i}"])
        else:
            values.extend([f"{raw} AS _v{i}", f"false AS _bad{i}"])
    con.execute(f"CREATE TABLE {side}_typed AS SELECT {', '.join(values)} FROM {side}_raw")
    keycols = ", ".join(f"_k{i}" for i in range(len(contract["keys"]["columns"])))
    invalid_key = " OR ".join(f"(_k{i} IS NULL OR trim(_k{i})='')" for i in range(len(contract["keys"]["columns"])))
    invalid_value = " OR ".join(f"_bad{i}" for i in range(len(contract["fields"])))
    con.execute(f"CREATE TABLE {side}_classified AS SELECT *, CASE "
                f"WHEN {invalid_key} THEN 'invalid_key' "
                f"WHEN count(*) OVER (PARTITION BY {keycols}) > 1 THEN 'duplicate_key' "
                f"WHEN {invalid_value} THEN 'invalid_value' ELSE 'eligible' END AS _status "
                f"FROM {side}_typed")
    con.execute(f"CREATE TABLE {side}_eligible AS SELECT * FROM {side}_classified WHERE _status='eligible'")
    stats = {"raw_rows": scalar(con, f"SELECT count(*) FROM {side}_raw")}
    for category in ("invalid_key", "duplicate_key", "invalid_value", "eligible"):
        stats[category + "_rows"] = scalar(con, f"SELECT count(*) FROM {side}_classified WHERE _status={ql(category)}")
    stats["duplicate_groups"] = scalar(con, f"SELECT count(*) FROM (SELECT {keycols} FROM {side}_classified WHERE _status='duplicate_key' GROUP BY {keycols})")
    stats["invalid_cast_cells"] = sum(scalar(con, f"SELECT count(*) FROM {side}_typed WHERE _bad{i}") for i in range(len(contract["fields"])))
    assert stats["raw_rows"] == sum(stats[c + "_rows"] for c in ("invalid_key", "duplicate_key", "invalid_value", "eligible"))
    for status in ("invalid_key", "duplicate_key"):
        con.execute(f"INSERT INTO exceptions SELECT {ql(status)}, {ql(side)}, _row_id, "
                    f"{key_json('r', contract)}, '', '', '', "
                    f"{ql('All rows with an ambiguous key are quarantined' if status == 'duplicate_key' else 'Null or blank composite key component')} "
                    f"FROM {side}_classified r WHERE _status={ql(status)}")
    # Emit cast errors even on rows quarantined earlier; row partitions and cell
    # counts are deliberately distinct. An invalid value is never treated as NULL.
    for i, field in enumerate(contract["fields"]):
        con.execute(f"INSERT INTO exceptions SELECT 'invalid_value', {ql(side)}, _row_id, "
                    f"{key_json('r', contract)}, {ql(field['name'])}, "
                    f"{'_raw'+str(i) if side == 'source' else ql('')}, "
                    f"{'_raw'+str(i) if side == 'target' else ql('')}, "
                    "'Malformed, out-of-range, or excess-scale decimal; row excluded from comparison' "
                    f"FROM {side}_classified r WHERE _bad{i}")
    return stats


def build_report(con, contract: dict) -> dict:
    join = join_keys("s", "t", contract)
    con.execute(f"CREATE TABLE matched AS SELECT s._row_id AS source_row_id, t._row_id AS target_row_id "
                f"FROM source_eligible s JOIN target_eligible t ON {join}")
    changed = []
    for i, field in enumerate(contract["fields"]):
        a, b = f"s._v{i}", f"t._v{i}"
        if field["type"] == "decimal":
            tolerance = f"CAST({ql(field['tolerance'])} AS DECIMAL(28,{field['scale']}))"
            differs = f"(({a} IS NULL) != ({b} IS NULL) OR ({a} IS NOT NULL AND {b} IS NOT NULL AND abs({a} - {b}) > {tolerance}))"
        else:
            differs = f"{a} IS DISTINCT FROM {b}"
        changed.append(differs)
        con.execute("INSERT INTO exceptions SELECT 'field_difference', 'both', s._row_id, "
                    f"{key_json('s', contract)}, {ql(field['name'])}, cast({a} AS VARCHAR), cast({b} AS VARCHAR), "
                    f"'Canonical values differ beyond declared tolerance' FROM source_eligible s "
                    f"JOIN target_eligible t ON {join} WHERE {differs}")
    matched = scalar(con, "SELECT count(*) FROM matched")
    changed_rows = scalar(con, f"SELECT count(*) FROM source_eligible s JOIN target_eligible t ON {join} WHERE " + " OR ".join(changed))
    metrics = {"matched_rows": matched, "changed_rows": changed_rows, "matched_clean_rows": matched - changed_rows}
    for side, opposite in (("source", "target"), ("target", "source")):
        eq = join_keys("r", "o", contract)
        # A known opposing quarantined key is blocked, not reported as missing.
        exists_bad = f"EXISTS (SELECT 1 FROM {opposite}_classified o WHERE {eq} AND o._status != 'eligible')"
        sql = f"SELECT r.*, CASE WHEN {exists_bad} THEN 'blocked_by_quarantine' ELSE '{side}_only' END AS category "
        sql += f"FROM {side}_eligible r WHERE NOT EXISTS (SELECT 1 FROM {opposite}_eligible o WHERE {eq})"
        con.execute(f"CREATE TABLE {side}_unmatched AS {sql}")
        con.execute(f"INSERT INTO exceptions SELECT category, {ql(side)}, _row_id, {key_json('r', contract)}, '', '', '', "
                    "CASE WHEN category='blocked_by_quarantine' THEN 'Opposing key exists but is quarantined' "
                    f"ELSE 'No comparable opposing key' END FROM {side}_unmatched r")
        metrics[side + "_only_rows"] = scalar(con, f"SELECT count(*) FROM {side}_unmatched WHERE category={ql(side + '_only')}")
        metrics[side + "_blocked_rows"] = scalar(con, f"SELECT count(*) FROM {side}_unmatched WHERE category='blocked_by_quarantine'")
    metrics["field_differences"] = scalar(con, "SELECT count(*) FROM exceptions WHERE category='field_difference'")
    return metrics


def write_html(audit: dict, rows: list[tuple], path: Path) -> None:
    esc = lambda value: html.escape("" if value is None else str(value), quote=True)
    body = ["<!doctype html><html lang='en'><meta charset='utf-8'><title>Migration reconciliation audit</title>",
            "<style>body{font:16px system-ui;max-width:1180px;margin:40px auto;padding:0 24px;color:#16324a}pre{background:#edf3f6;padding:20px;overflow:auto}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #ccd6df;padding:8px;text-align:left;overflow-wrap:anywhere}h1{font-size:30px}.pass{color:#147344}.fail{color:#b52929}</style>",
            "<h1>Migration reconciliation audit</h1>",
            f"<p class={'pass' if audit['passed'] else 'fail'}><strong>{'PASS' if audit['passed'] else 'DATA QUALITY FAILURE'}</strong> · Run {esc(audit['run_id'])}</p>",
            "<p>Independent synthetic-data portfolio project. Report is deterministic for the same inputs and settings.</p>",
            "<h2>Accounting and quality gates</h2><pre>" + esc(json.dumps(audit, indent=2, sort_keys=True)) + "</pre>",
            "<h2>Exception preview</h2><p>First 50 exceptions. Full results: exceptions.csv. Cell errors and row categories are separate counts.</p>",
            "<table><tr>" + "".join("<th>" + esc(x) + "</th>" for x in ("Category", "Side", "Row", "Key", "Field", "Source", "Target", "Detail")) + "</tr>"]
    body.extend("<tr>" + "".join("<td>" + esc(v) + "</td>" for v in row) + "</tr>" for row in rows)
    body.append("</table></html>\n")
    path.write_text("\n".join(body), encoding="utf-8")


def reconcile(source: Path, target: Path, contract_path: Path, output: Path, memory_limit: str = "256MB") -> dict:
    source, target, contract_path, output = (Path(p).resolve() for p in (source, target, contract_path, output))
    c = load_contract(contract_path)
    memory_limit = validate_memory_limit(memory_limit)
    required = c["keys"]["columns"] + [f["name"] for f in c["fields"]]
    for p in (source, target):
        inspect_csv(p, required)
    if len({source, target, contract_path}) != 3:
        raise InputError("Source, target and contract must be different files")
    output_files = {(output / n).resolve() for n in ("audit.json", "exceptions.csv", "report.html")}
    input_files = {source, target, contract_path}
    aliases_input = any(candidate.exists() and candidate.samefile(raw) for candidate in output_files for raw in input_files)
    if output_files & input_files or aliases_input:
        raise InputError("Output would overwrite an input file")
    try:
        inputs = {name: {"file": p.name, "bytes": p.stat().st_size, "sha256": file_hash(p)}
                  for name, p in (("source", source), ("target", target), ("contract", contract_path))}
        run_id = hashlib.sha256(json.dumps({"inputs": inputs, "memory_limit": memory_limit}, sort_keys=True).encode()).hexdigest()[:20]
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".reconcile-", dir=output) as tmp:
            con = duckdb.connect(str(Path(tmp) / "audit.duckdb"))
            try:
                con.execute(f"SET memory_limit = {ql(memory_limit)}")
                con.execute(f"SET temp_directory = {ql(str(Path(tmp) / 'spill'))}")
                con.execute("SET threads = 1")
                con.execute("CREATE TABLE exceptions(category VARCHAR, side VARCHAR, row_id BIGINT, key_json VARCHAR, field VARCHAR, source_value VARCHAR, target_value VARCHAR, detail VARCHAR)")
                source_stats = load_side(con, "source", source, c)
                target_stats = load_side(con, "target", target, c)
                metrics = build_report(con, c)
                for side, stats in (("source", source_stats), ("target", target_stats)):
                    assert stats["eligible_rows"] == metrics["matched_rows"] + metrics[side + "_only_rows"] + metrics[side + "_blocked_rows"]
                observations = {
                    "max_invalid_key_rows": source_stats["invalid_key_rows"] + target_stats["invalid_key_rows"],
                    "max_duplicate_rows": source_stats["duplicate_key_rows"] + target_stats["duplicate_key_rows"],
                    "max_invalid_value_rows": scalar(con, "SELECT count(*) FROM source_typed WHERE " + " OR ".join(f"_bad{i}" for i in range(len(c["fields"])))) + scalar(con, "SELECT count(*) FROM target_typed WHERE " + " OR ".join(f"_bad{i}" for i in range(len(c["fields"])))),
                    "max_unmatched_rows": sum(metrics[k] for k in ("source_only_rows", "target_only_rows", "source_blocked_rows", "target_blocked_rows")),
                    "max_changed_rows": metrics["changed_rows"],
                }
                gates = {name: {"limit": limit, "observed": observations[name], "passed": observations[name] <= limit}
                         for name, limit in c["gates"].items()}
                categories = dict(con.execute("SELECT category, count(*) FROM exceptions GROUP BY category ORDER BY category").fetchall())
                audit = {"report_schema": "1.0", "run_id": run_id, "engine": {"name": "duckdb", "version": duckdb.__version__, "threads": 1, "memory_limit": memory_limit},
                         "inputs": inputs, "source": source_stats, "target": target_stats, "comparison": metrics,
                         "exception_counts": categories, "quality_gates": gates, "passed": all(g["passed"] for g in gates.values()),
                         "accounting_invariants_passed": True,
                         "row_classification_precedence": ["invalid_key", "duplicate_key", "invalid_value", "eligible"]}
                # A separate streaming CSV export avoids fetching all exceptions.
                ordered = "SELECT * FROM exceptions ORDER BY category, side, row_id, field, key_json"
                staged = Path(tmp)
                con.execute(f"COPY ({ordered}) TO {ql(str(staged / 'exceptions.csv'))} (HEADER, DELIMITER ',')")
                preview = con.execute(ordered + " LIMIT 50").fetchall()
                for name, p in (("source", source), ("target", target), ("contract", contract_path)):
                    if file_hash(p) != inputs[name]["sha256"]:
                        raise InputError(f"{name} input changed during reconciliation; no reports published")
                # Release native export handles before replacing files on Windows.
                con.close()
                (staged / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                write_html(audit, preview, staged / "report.html")
                for name in ("exceptions.csv", "audit.json", "report.html"):
                    shutil.copyfile(staged / name, output / name)
                return audit
            finally:
                con.close()
    except (OSError, duckdb.Error) as exc:
        raise InputError(f"Reconciliation could not complete: {exc}") from exc
