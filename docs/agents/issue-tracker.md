# Issue tracker: Beads

Issues and specs for this repo live in the local Beads database. Use `bd` for issue operations. There is currently no Git remote, so issues remain local until a remote and sync workflow are configured.

## Conventions

- Find available work: `bd ready`.
- Find an issue: `bd search <query>` or `bd list`; read it with `bd show <id>`.
- Publish an issue or spec: `bd create --title="..." --description="..." --type=task`. Use `feature` or `bug` when appropriate.
- Claim work: `bd update <id> --claim`.
- Record discussion: `bd comment <id> "..."`.
- Resolve work: `bd close <id> --reason="..."`.
- Record dependencies: `bd dep add <issue> <blocker>`. Inspect blockers with `bd show <id>` or `bd blocked`.

When a skill says “publish to the issue tracker,” create a Beads issue and return its ID. When it says “fetch the relevant ticket,” run `bd show <id>`.

## Wayfinding operations

- Create the map as a parent Beads issue. Keep Notes, Decisions-so-far, and Fog in its description or notes.
- Create each child ticket with `bd create ... --parent=<map-id>` and set its type to `research`, `prototype`, `grilling`, or `task` in its description when that value is not a supported Beads issue type.
- Use Beads dependencies for blocking. `bd ready` identifies work without active blockers.
- Claim the selected child with `bd update <id> --claim`.
- Resolve the child with a comment containing its answer, then close it. Add a short result and the child ID to the map’s notes.
