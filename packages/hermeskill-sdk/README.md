# hermeskill

The Hermeskill SDK — `WatcherState`, symptom checks, death certificates,
control-plane client, and the `hermeskill` operator CLI.

This is the core SDK — it imports bare, with no agent-framework dependencies.
Supervision attaches through the [`hermeskill-hermes`](../hermeskill-hermes)
plugin: `pip install hermeskill-hermes` to supervise Hermes Agent sessions.
Hermes is the supported runtime.

See the [repo root README](../../README.md) for product overview, install
walkthrough, environment variables, and operator workflows.
