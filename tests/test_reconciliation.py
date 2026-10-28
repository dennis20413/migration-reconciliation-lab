from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from reconcile_lab.cli import main
from reconcile_lab.contract import InputError
from reconcile_lab.demo import generate_demo
from reconcile_lab.engine import file_hash, reconcile


@pytest.fixture
def fixture_files(tmp_path):
    data = tmp_path / "data"
    expected = generate_demo(data, 100)
    return data, expected


def run(data, output=None):
    return reconcile(data / "source.csv", data / "target.csv", data / "contract.json", output or data.parent / "report")


def write_rows(path: Path, rows: list[dict], header=None):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, header or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def simple(tmp_path, source, target, **contract_overrides):
    data = tmp_path / "data"
    data.mkdir()
    contract = {"keys": {"columns": ["company", "id"], "trim": True, "case": "upper"}, "null_tokens": ["", "NULL"],
                "fields": [{"name": "amount", "type": "decimal", "scale": 2, "tolerance": "0.01"}, {"name": "name", "type": "string"}]}
    contract.update(contract_overrides)
    (data / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
    write_rows(data / "source.csv", source)
    write_rows(data / "target.csv", target)
    return data


def row(company="A", id="001", amount="100.00", name="Alice"):
    return {"company": company, "id": id, "amount": amount, "name": name}


def test_demo_oracle_and_accounting(fixture_files):
    data, expected = fixture_files
    audit = run(data)
    for metric, value in expected.items():
        if metric in audit["comparison"]:
            assert audit["comparison"][metric] == value
    assert audit["source"]["raw_rows"] == expected["source_raw_rows"]
    assert audit["target"]["raw_rows"] == expected["target_raw_rows"]
    assert audit["exception_counts"] == expected["exception_counts"]
    assert not audit["passed"]
    assert audit["accounting_invariants_passed"]
    with (data.parent / "report/exceptions.csv").open(encoding="utf-8", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == sum(expected["exception_counts"].values())


@pytest.mark.parametrize("value,changes", [("100.01", 0), ("99.99", 0), ("100.02", 1), ("99.98", 1)])
def test_exact_tolerance_boundaries(tmp_path, value, changes):
    audit = run(simple(tmp_path, [row()], [row(amount=value)]))
    assert audit["comparison"]["changed_rows"] == changes
    assert audit["passed"] == (changes == 0)


def test_decimal_subtraction_has_precision_headroom(tmp_path):
    maximum = "99999999999999999999999999.99"
    audit = run(simple(tmp_path, [row(amount=maximum)], [row(amount="-" + maximum)]))
    assert audit["comparison"]["changed_rows"] == 1
    assert audit["source"]["invalid_cast_cells"] == 0


@pytest.mark.parametrize("value", ["1.001", "1e2", "nonsense", "99999999999999999999999999999.00", "NaN", "1,000.00"])
def test_invalid_decimal_is_an_exception_not_null(tmp_path, value):
    audit = run(simple(tmp_path, [row()], [row(amount=value)]))
    assert audit["target"]["invalid_cast_cells"] == 1
    assert audit["comparison"]["matched_rows"] == 0
    assert audit["comparison"]["source_blocked_rows"] == 1
    assert audit["exception_counts"]["invalid_value"] == 1


def test_nulls_are_equal_but_one_null_is_changed(tmp_path):
    a = [row(id="001", amount="", name=" NULL "), row(id="002", amount="", name="")]
    b = [row(id="001", amount="NULL", name=""), row(id="002", amount="0.00", name="")]
    audit = run(simple(tmp_path, a, b))
    assert audit["comparison"]["matched_rows"] == 2
    assert audit["comparison"]["changed_rows"] == 1
    assert audit["target"]["invalid_cast_cells"] == 0


def test_composite_keys_do_not_collide_and_leading_zeros_are_preserved(tmp_path):
    a = [row(company="A|B", id="C"), row(company="A", id="B|C"), row(company="A", id="001"), row(company="A", id="1")]
    b = [row(company="a|b", id="C"), row(company=" a ", id=" B|C "), row(company="A", id="001"), row(company="A", id="1")]
    audit = run(simple(tmp_path, a, b))
    assert audit["comparison"]["matched_rows"] == 4
    assert audit["passed"]


def test_all_duplicate_rows_quarantined_without_join_explosion(tmp_path):
    audit = run(simple(tmp_path, [row(), row(), row(id="002")], [row(), row(), row(), row(id="002")]))
    assert audit["source"]["duplicate_key_rows"] == 2
    assert audit["target"]["duplicate_key_rows"] == 3
    assert audit["comparison"]["matched_rows"] == 1
    assert audit["comparison"]["field_differences"] == 0
    assert audit["exception_counts"]["duplicate_key"] == 5


def test_invalid_cast_on_duplicate_row_remains_visible(tmp_path):
    audit = run(simple(tmp_path, [row()], [row(amount="invalid"), row()]))
    assert audit["target"]["duplicate_key_rows"] == 2
    assert audit["target"]["invalid_value_rows"] == 0
    assert audit["target"]["invalid_cast_cells"] == 1
    assert audit["quality_gates"]["max_invalid_value_rows"]["observed"] == 1
    assert audit["exception_counts"]["invalid_value"] == 1


def test_blank_key_is_quarantined(tmp_path):
    audit = run(simple(tmp_path, [row()], [row(id=" ")]))
    assert audit["target"]["invalid_key_rows"] == 1
    assert audit["comparison"]["source_only_rows"] == 1
    assert audit["comparison"]["matched_rows"] == 0


def test_blank_key_is_invalid_even_without_null_tokens(tmp_path):
    audit = run(simple(tmp_path, [row()], [row(id=" ")], null_tokens=[]))
    assert audit["target"]["invalid_key_rows"] == 1


def test_empty_string_field_can_be_literal_when_contract_requests(tmp_path):
    audit = run(simple(tmp_path, [row(name="")], [row(name="NULL")], null_tokens=[]))
    assert audit["comparison"]["changed_rows"] == 1


def test_missing_columns_fail_before_reporting(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    write_rows(data / "target.csv", [{"company": "A", "id": "001"}])
    with pytest.raises(InputError, match="missing required columns"):
        run(data)
    assert not (tmp_path / "report/audit.json").exists()


def test_duplicate_csv_headers_rejected(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    (data / "target.csv").write_text("company,id,amount,name,name\nA,001,1.00,A,A\n")
    with pytest.raises(InputError, match="duplicate CSV header"):
        run(data)


def test_html_escapes_untrusted_cells(fixture_files):
    data, _ = fixture_files
    run(data)
    rendered = (data.parent / "report/report.html").read_text(encoding="utf-8")
    assert '<script>alert("synthetic")</script>' not in rendered
    assert "&lt;script&gt;" in rendered


def test_rerun_reports_byte_identical_and_inputs_unchanged(fixture_files):
    data, _ = fixture_files
    before = {p.name: file_hash(p) for p in data.iterdir()}
    run(data, data.parent / "first")
    run(data, data.parent / "second")
    for name in ("audit.json", "exceptions.csv", "report.html"):
        assert (data.parent / "first" / name).read_bytes() == (data.parent / "second" / name).read_bytes()
    assert before == {p.name: file_hash(p) for p in data.iterdir()}


def test_input_hash_change_changes_run_id(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    first = run(data)
    write_rows(data / "target.csv", [row(name="Bob")])
    second = run(data)
    assert first["run_id"] != second["run_id"]
    assert first["inputs"]["target"]["sha256"] != second["inputs"]["target"]["sha256"]


def test_contract_hash_change_changes_run_id(tmp_path):
    data = simple(tmp_path, [row()], [row(amount="100.02")])
    first = run(data)
    c = json.loads((data / "contract.json").read_text())
    c["fields"][0]["tolerance"] = "0.02"
    (data / "contract.json").write_text(json.dumps(c))
    second = run(data)
    assert first["run_id"] != second["run_id"]
    assert not first["passed"] and second["passed"]


@pytest.mark.parametrize("code,invalid", [(0, False), (1, True), (2, "missing")])
def test_cli_exit_codes(tmp_path, code, invalid):
    data = simple(tmp_path, [row()], [row(amount="invalid") if invalid else row()])
    if invalid == "missing":
        (data / "target.csv").unlink()
    args = ["reconcile", "--source", str(data / "source.csv"), "--target", str(data / "target.csv"), "--contract", str(data / "contract.json"), "--out", str(tmp_path / "out")]
    assert main(args) == code


def test_generator_does_not_overwrite_raw_inputs(fixture_files):
    data, _ = fixture_files
    original = file_hash(data / "source.csv")
    with pytest.raises(InputError, match="already contains"):
        generate_demo(data, 100)
    assert original == file_hash(data / "source.csv")


def test_gate_can_allow_known_difference(tmp_path):
    audit = run(simple(tmp_path, [row()], [row(name="Bob")], gates={"max_changed_rows": 1}))
    assert audit["passed"]
    assert audit["comparison"]["changed_rows"] == 1


def test_input_report_path_collision_is_rejected(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    source = data / "audit.json"
    source.write_bytes((data / "source.csv").read_bytes())
    before = file_hash(source)
    with pytest.raises(InputError, match="overwrite"):
        reconcile(source, data / "target.csv", data / "contract.json", data)
    assert file_hash(source) == before


def test_hardlinked_output_cannot_overwrite_an_input(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    output = tmp_path / "report"
    output.mkdir()
    try:
        os.link(data / "source.csv", output / "exceptions.csv")
    except OSError:
        pytest.skip("Filesystem does not permit test hardlinks")
    before = file_hash(data / "source.csv")
    with pytest.raises(InputError, match="overwrite"):
        run(data, output)
    assert file_hash(data / "source.csv") == before


@pytest.mark.parametrize("update", [{"keys": {"columns": ["id", "id"]}}, {"fields": [{"name": "amount", "type": "decimal", "scale": 2, "tolerance": "0.001"}]}, {"fields": [{"name": "amount", "type": "decimal", "scale": 2, "tolerance": "100000000000000000000000000.00"}]}, {"fields": ["not an object"]}, {"gates": {"max_changed_rows": -1}}, {"keys": {"columns": ["id"], "case": "lower"}}])
def test_malformed_contract_is_input_error(tmp_path, update):
    data = simple(tmp_path, [row()], [row()], **update)
    with pytest.raises(InputError):
        run(data)


def test_empty_files_with_headers_can_pass(tmp_path):
    data = simple(tmp_path, [row()], [row()])
    write_rows(data / "source.csv", [], list(row()))
    write_rows(data / "target.csv", [], list(row()))
    audit = run(data)
    assert audit["passed"] and audit["comparison"]["matched_rows"] == 0
