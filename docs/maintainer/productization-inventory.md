# OpenEvo Productization Inventory

This file records the current pre-migration identity surface so the physical
migration can be audited without committing known-failing tests to `stable`.

Run:

```bash
python3 scripts/ci/audit_openevo_identity.py
```

The migration is complete only after the final identity guard passes without an
active product-surface match for historical runtime identity markers.

The audit report separates `active_matches` from `archived_matches`.
`active_matches` cover the current product surface; `archived_matches` cover
historical plans, specs, and debug notes that are not release-facing.
