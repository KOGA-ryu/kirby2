# WO38-D test-only loopback PKI

This frozen certificate packet is an audit fixture, not an operator trust root.
It is valid only when the caller explicitly selects `AUDIT_LOOPBACK_FIXTURE`, opts
in to fixture use, and binds/connects through loopback. The coordinator identity is
`coordinator.test.kirby2.invalid`; the worker identity is
`worker.test.kirby2.invalid`.

Every private key in this directory is public source material. It provides no
authentication, confidentiality, authorization, or security claim on a real LAN.
Production startup rejects these certificates by DER fingerprint even if they are
copied outside the repository. Operators must provision their own CA and leaf
credentials outside the repository.

`fixture_manifest.json` freezes the exact file and certificate identities. The CA
private key is retained solely to make the fixture packet self-contained for audit
maintenance; runtime code never needs it.
