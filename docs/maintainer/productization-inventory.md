# OpenEvo Productization Inventory

Tracked by #121.

This file records the current pre-migration identity surface so the physical
migration can be audited without committing known-failing tests to `stable`.

Run:

```bash
python3 scripts/ci/audit_openevo_identity.py
```

The migration is complete only after the final identity guard in Task 9 passes
without an allowlist for public Polar runtime identity.
