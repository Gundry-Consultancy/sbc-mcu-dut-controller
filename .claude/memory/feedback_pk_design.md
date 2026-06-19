---
name: feedback-pk-design
description: Never use natural/composite PKs on attribute columns; surrogate integer PKs + UNIQUE constraints
metadata:
  type: feedback
---

Do not propose composite primary keys made of attribute columns (e.g. `(device_id, vid, pid, iserial)`). Use a surrogate integer PK and express dedup with a `UNIQUE` constraint instead. iSerial in particular must never be part of a PK.

**Why:** Attribute-based PKs conflate identity with mutable state — a PID change rewrites the row's identity, breaks FKs, and makes audit/history rows pointless. The user reacted strongly ("FFS") to seeing `(device_id, vid, pid, iserial)` as a PK in a schema plan.

**How to apply:** When designing any table in this project, default to `id INTEGER PRIMARY KEY AUTOINCREMENT` (or TEXT id where the domain genuinely owns the identifier, like `devices.id`). For natural-key dedup, use `UNIQUE (...)` indexes. See [[project-state]] for the existing schema conventions.
