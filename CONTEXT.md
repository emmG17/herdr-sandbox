# Project context

A **source repository** is the Git repository supplied by the user or inferred from the active Herdr workspace. Worker creation reads it but does not change its files, refs, or Git metadata.

A **worker** is an independent disposable clone stored at `$HERDR_PLUGIN_STATE_DIR/workers/<id>/repo`. Its branch is `agent/<id>`. The adjacent `worker.json` records the source, requested base ref, resolved base commit, branch, and creation state so later lifecycle actions can recover it.

Herdr's plugin config directory holds persistent settings and the dedicated Codex home. Herdr's plugin state directory holds disposable worker repositories and their metadata. Plugin actions are fixed commands; the optional `[create]` config table supplies a requested ID, source, and base to the noninteractive create action.

The **Herdr control plane** is the trusted host-side Herdr session and plugin
actions that create, inspect, fetch, integrate, and destroy workers. A worker
is not part of that control plane and has no source-repository integration
authority.
