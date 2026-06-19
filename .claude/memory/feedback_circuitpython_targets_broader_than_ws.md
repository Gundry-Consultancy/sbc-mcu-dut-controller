---
name: feedback-circuitpython-targets-broader-than-ws
description: This controller targets the full CircuitPython matrix, not just WipperSnapper — don't drop STM32/nRF/dfu-util support from planning
metadata:
  type: feedback
---

The `usbip-hil-controller` is a **general HIL platform**, not a
WipperSnapper-specific tool. Even when a task is framed around the
four current WipperSnapper MCU families (ESP32, SAMD51, RP2040,
RP2350), the broader CircuitPython target matrix (STM32, nRF52, and
others) is also in-scope as future-supported families.

Do not remove STM32/nRF/dfu-util from architecture plans, milestone
non-goals, or design docs — they belong as **planned-future** rows
(post-M4), not as "drop unless a real DUT appears" non-goals.

**Why:** caught 2026-06-07. While expanding the M4 milestone I
demoted `DfuUtilFlasher` from "planned" to "non-goal unless a real
DUT appears" because no WipperSnapper target needs it. User
correction: "DO NOT remove the other targets for circuitpython
(stm32/nrf etc), which this platform will also test in future, but
it's okay for wippersnapper." The WS scoping is fine for the M4
milestone bullet, but the broader CircuitPython matrix needs to
stay visible in the design.

**How to apply:**
- When defining a "supported targets" matrix, label it explicitly —
  `WipperSnapper target matrix` vs `CircuitPython target matrix` vs
  `(general) HIL target matrix`. Don't conflate.
- A WipperSnapper non-goal is **not** a CircuitPython non-goal.
- For `FlasherProtocol`-style abstractions, keep the design extensible
  to STM32/nRF/dfu-util even when the immediate concretes only cover
  WS families. The point of the abstraction is exactly to admit
  future families as single-file adds.
- When unsure whether a target is in-scope, default to "planned,
  post-M4" rather than dropping it from the roadmap.
