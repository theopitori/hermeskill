# stasis-agent

The Caspase SDK — `WatcherState`, symptom checks, death certificates,
control-plane client, and the `stasis` operator CLI.

This is the framework-agnostic core. Use it via a framework adapter such as
[`stasis-hermes`](../stasis-hermes), or wire it in directly with `watch()` if
you're building a custom adapter.

> The product is **Caspase**. The PyPI distribution and Python import are still
> `stasis-agent` / `stasis_agent` while the code-rename PR is in flight.

See the [repo root README](../../README.md) for product overview, install
walkthrough, environment variables, and operator workflows.
