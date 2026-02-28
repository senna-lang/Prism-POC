
## Logosyncx

Use `logos` CLI for session context management.
Full reference: `.logosyncx/USAGE.md`

**MANDATORY triggers:**

- **Start of every session** → `logos ls --json` (check past context before doing anything)
- User says "save this session" / "記録して" → `logos save --topic "..." --section "Summary=..." --section "Key Decisions=..."`
- User says "make that a task" / "タスクにして" → `logos task create --title "..." --section "What=..."`
- User says "continue from last time" / "前回の続き" → `logos ls --json` then `logos refer --name <name> --summary`

Never use positional arguments. Never use `--body` or `--description`. All body content goes in `--section "Name=content"` flags.
