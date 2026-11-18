# Audit scopes

The `audit` kit (`LOYD_KITS=...,audit`) refuses to run without a scope file here,
written **before** the engagement. One file per engagement, named `<scope-id>.json`:

```json
{"id": "acme-2026-11", "client": "Acme Co",
 "authorized_by": "Jane Doe, IT Director",
 "authorization_ref": "SOW #123 / signed email 2026-11-17",
 "cidrs": ["192.168.1.0/24"], "domains": ["acme.com"],
 "starts": "2026-11-18", "expires": "2026-11-20", "intensity": "gentle"}
```

- Get the authorization in writing (who, what ranges, which dates) and reference it.
- `intensity`: `gentle` (default: 50 packets/s, top 100 ports) or `normal` (300/s, top 1000).
- Delete or edit the file to stop or narrow the audit; every call re-reads it.
- Findings accumulate in `<scope-id>.findings.jsonl`; `audit_report` writes `<scope-id>.report.md`.
- Scope files, findings and reports are client data: they are git-ignored. Keep them off shared drives.
