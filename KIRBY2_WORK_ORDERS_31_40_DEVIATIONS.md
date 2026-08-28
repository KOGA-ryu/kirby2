# Kirby2 Work Orders 31-40 Deviation Ledger

This append-only ledger records unforeseen prerequisite repairs encountered while
executing `KIRBY2_WORK_ORDERS_31_40_GOAL.md`. Records are never renumbered or edited
after their repair commit.

## DEV-0001 — Bind explicit CLI modules to audit provenance

- Interrupted canonical card: `K2X-02`
- Exact first-parent predecessor: `2e8969baa2c3cf436c176e18a3ac21391cda2ee0`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab`
- Observed terminal result:
  `MODEL_RISK_LAB_AUDIT FAIL cases=21 failures=1`
- Observed failing case: `byte_bound_provenance_and_orthogonal_gate_truth`
- Root cause: K2X-02 introduces the explicit `kirby2/cli` source package. The
  inherited byte-bound provenance manifest enumerates explicit package roots and did
  not yet include `cli`, so the three loaded CLI modules were correctly reported as
  unbound even though their behavior and legacy compatibility checks passed.
- Repair: add `cli` to `PROVENANCE_PACKAGE_ROOTS`. This makes the existing generic
  manifest and its independent model-risk oracle bind every CLI source byte without
  weakening loaded-module checks or special-casing filenames.
- Owned repair path: `kirby2/auditlab/runner.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0001` through the in-progress K2X-02 explicit expansion
  seam. The registration source lands with the resumed K2X-02 commit because that
  seam does not exist in the predecessor tree.
- Inherited gates: K2X-02 legacy CLI projection, deterministic smoke pair, sealed
  `.kirby2` tree, and all model-risk cases remain unchanged.
- Exact commit subject: `Bind CLI sources to audit provenance`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0001
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'from pathlib import Path; from kirby2.auditlab.runner import PROVENANCE_PACKAGE_ROOTS,_implementation_manifest; root=Path.cwd(); manifest,links,errors=_implementation_manifest(root); expected={"kirby2/cli/__init__.py","kirby2/cli/expansion.py","kirby2/cli/registry.py"}; assert PROVENANCE_PACKAGE_ROOTS.count("cli")==1; assert expected.issubset(manifest); assert not links and not errors; print("CLI_PROVENANCE_BINDING PASS files=3")'
git diff --check
```

Acceptance: `cli` occurs exactly once in the explicit provenance roots; the three
K2X-02 CLI source files are byte-bound by the generic implementation manifest;
loaded-module binding passes without exclusion or unloading; the model-risk command
returns zero with `MODEL_RISK_LAB_AUDIT PASS cases=21 failures=0`; and no `.kirby2`
artifact changes.
