# Logosyncx Usage for AI Agents

You have access to the `logos` CLI for managing session context.
`logos` is a shell binary — call it via terminal/shell commands.

---

## MANDATORY Triggers

The following are **not optional**. When any condition below occurs, you MUST run the corresponding command.

| Condition | You MUST run |
|---|---|
| Starting any work session | `logos ls --json` (always, before doing anything else) |
| User says "save this session", "log this", "記録して", "セッションを保存して" | `logos save --topic "..." --section "Summary=..." ...` |
| User says "make that a task", "add a TODO", "タスクにして", "issueにして" | `logos task create --title "..." --section "What=..." ...` |
| User says "continue from last time", "前回の続き", "show past context" | `logos ls --json` then `logos refer --name <name> --summary` |
| User mentions a past feature, decision, or discussion | `logos ls --json` and check excerpts for relevance |

> **Never say "I can't save sessions" or "I don't have access to logos."**
> The `logos` binary is installed and available via shell. Always use it.

### Syntax rules (apply to every command)

- **Always use flags** — never pass positional arguments.
- **Section content via `--section` only** — `--body`, `--body-stdin`, and `--description` do not exist.
- All section names must be defined in `.logosyncx/config.json`.

---

## Workflow for finding relevant context

1. Run `logos ls --json` to get all sessions with excerpts
2. Read the `topic`, `tags`, and `excerpt` fields to judge relevance yourself
3. Run `logos refer --name <filename> --summary` on relevant sessions to get details
4. If you want to narrow down by keyword first, use `logos search --keyword <keyword>`

## Workflow for saving context

```
logos save --topic "short description of the session" \
           --tag go --tag cli \
           --agent claude-code \
           --section "Summary=What happened in this session." \
           --section "Key Decisions=- Decision one"
```

For longer content, use a variable:
```
logos save --topic "short description" \
           --section "Summary=Implemented the auth flow. Chose JWT over sessions because of stateless requirements." \
           --section "Key Decisions=- JWT over sessions\n- RS256 algorithm"
```

## Commands

### List sessions
```
logos ls                    # human-readable table
logos ls --tag auth         # filter by tag
logos ls --since 2025-02-01 # filter by date
logos ls --json             # structured output with excerpts (preferred for agents)
```

### Read a session
```
logos refer --name <filename>            # full content
logos refer --name <partial-name>        # partial match
logos refer --name <filename> --summary  # key sections only (saves tokens, prefer this)
```

### Save a session
```
# topic only, no body sections
logos save --topic "..."

# with section content (--section is the only way to add body content)
logos save --topic "..." --section "Summary=text"
logos save --topic "..." \
           --tag go --tag cli \
           --agent claude-code \
           --related 2026-01-01_previous.md \
           --task <partial-task-name> \
           --section "Summary=What happened." \
           --section "Key Decisions=- Decision A"
```

Use `--task` to link this session to one or more existing tasks (partial name match, repeatable).
The resolved task filenames are stored in the session's `tasks:` frontmatter field.

Allowed section names are defined in `.logosyncx/config.json` under `sessions.sections`.
Unknown section names are rejected with an error.

### Search (keyword narrowing)
```
logos search --keyword "keyword"              # search on topic, tags, and excerpt
logos search --keyword "auth" --tag security
```

## Check uncommitted changes

```
logos status
```

Shows all files under `.logosyncx/` that are staged, unstaged, or untracked —
grouped by state. Useful for agents to confirm that `logos save` or
`logos task create` actually persisted before ending a session.

Output example:
```
Staged (ready to commit):
  (added)      sessions/2026-02-28_my-session.md

Untracked (not staged):
  (new)        tasks/open/2026-02-28_my-task.md

Run `git add .logosyncx/ && git commit` to commit the above.
```

This command is informational and never modifies any file or git state.

## Sync index

If you manually edit, add, or delete session or task files, run:

```
logos sync
```

This rebuilds both `index.jsonl` and `task-index.jsonl` from the filesystem so that
`logos ls` and `logos task ls` return accurate results.

## Archive stale sessions (GC)

Over time sessions accumulate. Use `logos gc` to move stale sessions to
`sessions/archive/` without permanently deleting them.

```
logos gc --dry-run                  # preview candidates, do nothing
logos gc                            # move stale sessions to sessions/archive/
logos gc --linked-days 14           # override: archive linked sessions after 14 days
logos gc --orphan-days 60           # override: archive orphan sessions after 60 days
logos gc purge                      # confirm and permanently delete archived sessions
logos gc purge --force              # skip confirmation
```

A session is a **strong candidate** (default: 30 days) when all its linked tasks are
`done` or `cancelled` and at least `--linked-days` have passed since the last task completed.

A session is a **weak candidate** (default: 90 days) when it has no linked tasks and is
older than `--orphan-days`.

Sessions with at least one linked task still `open` or `in_progress` are **protected** and
will never be archived automatically.

> Tip: link sessions to tasks via `logos save --task <partial>` so GC can use task
> completion as the archival signal instead of raw age.

## Token strategy
- Use `logos ls --json` first to scan all sessions cheaply via excerpts
- Use `--summary` on `refer` unless you need the full conversation log
- Only use full `refer` when the summary is insufficient

## Tasks

Action items, implementation proposals, and TODO items that arise during a session can be saved as tasks.
Tasks are always linked to a session — the session serves as the rationale for why the task exists.

### When to create a task

- When the user says "make that a task", "do that later", or "add a TODO"
- When you propose an implementation plan, improvement, or refactoring idea
- After saving a session, when you want to preserve a specific proposal for later

### Workflow for creating a task

```
logos task create --title "Implement the thing" \
                  --priority high \
                  --tag go --tag cli \
                  --session <partial-session-name> \
                  --section "What=Add X so that Y." \
                  --section "Why=Required for the new auth flow."
```

Allowed section names are defined in `.logosyncx/config.json` under `tasks.sections`.
Unknown section names are rejected with an error.

> All fields are passed as flags — never use positional arguments.
> Section content must be provided via `--section "Name=content"`. There is no `--description` flag.

### Workflow for checking tasks

1. Run `logos task ls --status open --json` to get a list of outstanding tasks
2. Read `title` and `excerpt` to judge which tasks are relevant
3. Run `logos task refer --name <name> --with-session` to get full task details plus the linked session summary

### Commands

```
# List tasks
logos task ls                              # human-readable table
logos task ls --status open               # filter by status (open, in_progress, done, cancelled)
logos task ls --session <name>            # filter by linked session
logos task ls --priority high             # filter by priority (high, medium, low)
logos task ls --tag <tag>                 # filter by tag
logos task ls --json                      # structured output with excerpts (preferred for agents)

# Read a task
logos task refer --name <name>                   # full content
logos task refer --name <name> --summary         # key sections only (saves tokens)
logos task refer --name <name> --with-session    # append linked session summary

# Create a task
logos task create --title "..."                                        # title only, empty body
logos task create --title "..." --section "What=..." --priority high --tag <tag>
logos task create --title "..." --session <name>                       # link to a session
logos task create --title "..." \
                  --section "What=Implement X." \
                  --section "Why=Needed for Y." \
                  --section "Checklist=- [ ] step one\n- [ ] step two"

# Update a task
logos task update --name <name> --status in_progress        # moves file to tasks/in_progress/
logos task update --name <name> --status done               # moves file to tasks/done/
logos task update --name <name> --priority high
logos task update --name <name> --assignee <assignee>
logos task update --name <name> --add-session <partial>     # link a session to this task

# Delete a single task
logos task delete --name <name>           # prompts for confirmation
logos task delete --name <name> --force   # skip confirmation

# Bulk-delete all tasks with a given status
logos task purge --status done            # shows list + confirmation prompt
logos task purge --status done --force    # skip confirmation
logos task purge --status cancelled --force

# Search tasks
logos task search --keyword "keyword"                    # search title, tags, and excerpt
logos task search --keyword "keyword" --status open
logos task search --keyword "keyword" --tag <tag>
```
