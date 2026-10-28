# Explain the project in an interview

## A 45-second explanation

"I built a contract-driven CSV migration reconciler inspired by the problem of validating source and target exports. It uses disk-backed DuckDB SQL to classify rows, quarantine ambiguous keys, compare exact decimals and generate reviewable exceptions. Each run records input hashes and checks row-accounting identities. The repository contains independent synthetic data generation, an anomaly oracle, automated tests and a measured local benchmark. This portfolio implementation contains no employer or client code or data."

## Decisions to be ready to defend

1. **Why not join the raw tables immediately?** Two source duplicates and three target duplicates create six apparent comparisons. The engine quarantines every row carrying that valid duplicate key before joining. A unique opposite row is marked blocked, preventing a false "missing record" diagnosis.
2. **Why decimal and strict lexical validation?** Binary floats do not represent every currency amount exactly. `DECIMAL(28, scale)` provides exact comparison; lexical checks reject excess fractional digits before DuckDB can round a value. The tolerance boundary is inclusive: difference `<= tolerance` passes. The supported scale is 0-12, with precision headroom for subtraction.
3. **Why composite columns rather than a joined string key?** `('A|B','C')` and `('A','B|C')` must remain different. The join and duplicate partition use separate normalized SQL columns. `001` remains different from `1`.
4. **Does the memory setting prove constant memory?** No. Raw, typed and classified tables are persisted in a temporary database and DuckDB may spill intermediate operations. Its memory setting does not cover every allocation. No peak RSS was measured. Wide records, extreme skew, disk pressure and high exception rates still need separate benchmarks.
5. **What makes the audit reproducible?** Canonical comparison is deterministic, output exceptions are sorted and input/contract SHA256 hashes are recorded. Wall-clock measurement belongs in the benchmark JSON, not the deterministic audit. The engine rechecks input hashes before publishing reports to catch concurrent edits.
6. **Why are exception counts different from row counts?** A row can have multiple invalid cells or multiple field differences. Exclusive row status uses a documented precedence; cast errors are also counted independently so an invalid decimal on a duplicate row remains visible.
7. **What does a PASS mean?** Every configured gate is within its declared limit, not necessarily zero anomalies. Reports record each threshold and observed count. Quality failure returns exit 1; malformed inputs or execution failure return exit 2.

## Improvements worth implementing yourself

- Benchmark skewed keys, wide Unicode rows and high mismatch rates with process-level peak-memory measurement.
- Add a contract migration/versioning policy before supporting additional comparison types.
- Build a review lifecycle that stores resolution evidence and separates validation from approval.
- Validate a schema mapping layer against independently prepared fixtures before reading real exports.
- Add a separate timestamp type only after deciding timezone and precision semantics.

## Resume wording and boundaries

Use measured numbers from `artifacts/benchmark-1000000.json`, including the actual machine and scope. Describe this as an independent portfolio project. Keep work experience separate: this repo does not prove that its implementation ran on an employer's production exports or that it delivered a particular client time saving.

Before claiming personal ownership in an interview, run the CLI, change a contract, reproduce a failure and explain the SQL classification and tests. AI-assisted code should become code you can inspect, maintain and defend.
