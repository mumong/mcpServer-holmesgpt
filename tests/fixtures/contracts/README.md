# Fact Ledger test snapshot

`fact-ledger.schema.json` is a byte-for-byte test-only snapshot, not a new
authoritative contract. Authority remains in the sibling `data` repository.

- Source repository: `data` in the `combine-aiops-mcp` workspace
- Source commit: `b08807dfe5ca7815a3cd1e363eb391ba5d2f543b`
- Source path: `specs/004-canonical-fact-ledger/contracts/fact-ledger.schema.json`
- Raw SHA256: `091178c3eecd3d051c1291f1df0940b6de3dc9a492952e7a8bb1437b8d22d0e9`
- Contract ID: `https://aiops.local/contracts/aiops.fact-ledger.v1.schema.json`

Tests always validate against this fixed local snapshot. When the upstream file
is present, a separate test requires byte equality; otherwise only that optional
consistency check is skipped. No runtime code loads this fixture.

Update only after reviewing an intentional upstream contract change. Copy the
exact committed source bytes, update this provenance and the pinned test hash,
and review validation changes together. Do not rewrite or automatically refresh
the fixture to make tests pass. Integration CI with the upstream checkout must
continue checking for drift that an isolated checkout cannot detect.
