# Kirby2 Release Resource Preflight

Status: `PASS`

Protocol set SHA-256: `94f0050a592e3279a4b38b3d2e55b0ccfdc784202e67dca13e21a91fb631f9e8`
WO40-D protocol commit: `8730ba83b4f54beb2308d7ef710b29e06e99a9fb`

This inspection was read-only and no-network. It did not download, install, build, connect to a provider, or alter credentials.

| Resource | Target | Kind | Status | Detail |
| --- | --- | --- | --- | --- |
| `wo40-d-protocol-commit` | `all` | `PROTOCOL_COMMIT` | `PASS` | Resolved protocol revision contains every exact byte bound by the report. |
| `duckdb-1.5.5-cp314-cp314-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl` | `linux-x86_64` | `LOCKED_DEPENDENCY_WHEEL` | `PASS` | Exact local wheel digest matched. |
| `duckdb-1.5.5-cp314-cp314-macosx_11_0_arm64.whl` | `macos-arm64` | `LOCKED_DEPENDENCY_WHEEL` | `PASS` | Exact local wheel digest matched. |
| `git` | `build-host` | `EXTERNAL_PACKAGING_TOOL` | `PASS` | Executable is available and its local bytes were fingerprinted. |
| `project-wheel-frontend` | `build-host` | `EXTERNAL_PACKAGING_TOOL` | `PASS` | Executable is available and its local bytes were fingerprinted. |
| `clean-provider-inventory` | `all` | `CLEAN_PROVIDER_INVENTORY` | `PASS` | Secret-free provider inventory parsed and was fingerprinted. |
| `clean-provider-macos-arm64` | `macos-arm64` | `REAL_CLEAN_ENVIRONMENT_PROVIDER` | `PASS` | Provider satisfies the frozen clean-target capability contract. access=LOCAL_VM; system=Darwin; machine=arm64; runtime=CPython-3.14; available=true; credential_available=true; disk_bytes=47444193280; memory_bytes=10737418240; offline_install=true; clean_root=DISPOSABLE_VM_SNAPSHOT; evidence_return=LOCAL_ARTIFACT_EXPORT |
| `clean-provider-linux-x86_64` | `linux-x86_64` | `REAL_CLEAN_ENVIRONMENT_PROVIDER` | `PASS` | Provider satisfies the frozen clean-target capability contract. access=REMOTE_SSH; system=Linux; machine=x86_64; runtime=CPython-3.14; available=true; credential_available=true; disk_bytes=1754163216384; memory_bytes=29293547520; offline_install=true; clean_root=EPHEMERAL_HOST; evidence_return=SSH_ARTIFACT_RETURN |
| `starter-scenario-inventory` | `all` | `CANDIDATE_STARTER_PACK_INVENTORY` | `PASS` | Verified committed inventory with 5 members. |
| `starter-scenario-manifest` | `all` | `CANDIDATE_STARTER_MANIFEST` | `PASS` | Committed starter source-manifest bytes matched the frozen layout. |
| `starter-scenario-archive` | `all` | `CANDIDATE_STARTER_PACK_ARCHIVE` | `PASS` | In-memory deterministic starter archive verified with its owning adapter. |
| `starter-curriculum-inventory` | `all` | `CANDIDATE_STARTER_PACK_INVENTORY` | `PASS` | Verified committed inventory with 9 members. |
| `starter-curriculum-manifest` | `all` | `CANDIDATE_STARTER_MANIFEST` | `PASS` | Committed starter source-manifest bytes matched the frozen layout. |
| `starter-curriculum-archive` | `all` | `CANDIDATE_STARTER_PACK_ARCHIVE` | `PASS` | In-memory deterministic starter archive verified with its owning adapter. |

## Machine-readable missing items

```json
[]
```
