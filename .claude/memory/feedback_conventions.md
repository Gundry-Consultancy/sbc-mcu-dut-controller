---
name: feedback-conventions
description: Established working conventions for this project
metadata:
  type: feedback
---

- No emojis anywhere — replies, files, commit messages, docs.
- Terse, complete-sentence updates. No running commentary.
- Architecture-first for new features; code when user confirms.
- Commit message style: `feat:` / `ci:` / `docs:` / `fix:` prefix, terse imperative body.
- TDD required: write tests before implementation.
- Memories live in the repo at `.claude/memory/`, not in `~/.claude/`. Read `.claude/memory/MEMORY.md` at session start.
- Update `docs/ARCHITECTURE.md` milestone statuses and `docs/AGENT_HANDOFF.md` checklist whenever milestones complete — not just the local memory files.

- Always push to origin after committing — no need to ask.

**Why:** User confirmed memories-in-repo preference and always-push rule 2026-05-22.

**How to apply:** Follow unconditionally. After any milestone: update docs, update `.claude/memory/project_state.md`, commit, push.
