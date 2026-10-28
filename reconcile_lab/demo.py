"""Deterministic streaming fixtures with a known anomaly oracle."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .contract import InputError


DEMO_CONTRACT = {
    "name": "Independent synthetic invoice migration",
    "keys": {"columns": ["company_code", "invoice_id"], "trim": True, "case": "upper"},
    "null_tokens": ["", "NULL", "N/A"],
    "fields": [
        {"name": "customer_name", "type": "string", "trim": True},
        {"name": "amount", "type": "decimal", "scale": 2, "tolerance": "0.01"},
        {"name": "currency", "type": "string", "trim": True},
    ],
    "gates": {"max_invalid_key_rows": 0, "max_duplicate_rows": 0, "max_invalid_value_rows": 0, "max_unmatched_rows": 0, "max_changed_rows": 0},
}


def demo_row(i: int) -> dict:
    # Integer arithmetic constructs exact two-place amounts, no binary float.
    cents = 10000 + (i * 37) % 900000
    return {"company_code": "c001" if i % 2 else "c002", "invoice_id": f"{i:08d}",
            "customer_name": f"Synthetic Customer {i % 97:03d}", "amount": f"{cents // 100}.{cents % 100:02d}", "currency": "TWD"}


def add_cents(value: str, delta: int) -> str:
    major, minor = value.split(".")
    cents = int(major) * 100 + int(minor) + delta
    return f"{cents // 100}.{cents % 100:02d}"


def generate_demo(directory: Path, rows: int = 1000) -> dict:
    directory = Path(directory)
    if rows < 12:
        raise InputError("Demo requires at least 12 rows")
    names = ("source.csv", "target.csv", "contract.json", "expected.json")
    if any((directory / n).exists() for n in names):
        raise InputError("Demo destination already contains generated inputs; choose a new directory")
    directory.mkdir(parents=True, exist_ok=True)
    header = list(demo_row(1))
    with (directory / "source.csv").open("w", newline="", encoding="utf-8") as a, (directory / "target.csv").open("w", newline="", encoding="utf-8") as b:
        source, target = csv.DictWriter(a, header), csv.DictWriter(b, header)
        source.writeheader()
        target.writeheader()
        for i in range(1, rows + 1):
            row = demo_row(i)
            if i == 10:
                row["customer_name"] = ""
            source.writerow(row)
            if i == 8:
                source.writerow(row)
            if i == 2:
                continue
            migrated = dict(row)
            if i in (3, 12):
                migrated["amount"] = add_cents(row["amount"], 2)
            if i == 4:
                migrated["customer_name"] = '<script>alert("synthetic")</script>'
            if i == 6:
                migrated["amount"] = "NOT_A_DECIMAL"
            if i == 7:
                migrated["invoice_id"] = " "
            if i == 9:
                migrated["company_code"] = f" {row['company_code'].upper()} "
            if i == 10:
                migrated["customer_name"] = "NULL"
            if i == 11:
                migrated["amount"] = add_cents(row["amount"], 1)
            target.writerow(migrated)
            if i == 5:
                target.writerow(migrated)
        target.writerow(demo_row(rows + 1))
    expected = {"source_raw_rows": rows + 1, "target_raw_rows": rows + 1, "matched_rows": rows - 5, "changed_rows": 3,
                "matched_clean_rows": rows - 8, "source_only_rows": 2, "target_only_rows": 1,
                "source_blocked_rows": 2, "target_blocked_rows": 1, "field_differences": 3,
                "exception_counts": {"blocked_by_quarantine": 3, "duplicate_key": 4, "field_difference": 3, "invalid_key": 1, "invalid_value": 1, "source_only": 2, "target_only": 1}}
    (directory / "contract.json").write_text(json.dumps(DEMO_CONTRACT, indent=2) + "\n", encoding="utf-8")
    (directory / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return expected
