# Offline engine demo (`python -m demo`)

This is the **reproducible half** of Hermeskill's proof. It runs the *real*
detection engine — the same `WatcherState`, symptom checks, policies, and
death-certificate builder the Hermes plugin uses — against a **scripted**
agent, with no LLM key and no Postgres. It's deterministic, so it's what CI
records and asserts on.

> **What it is, and what it isn't.** The detection, the block directive, and the
> forensic certificate are all real code paths. What's *scripted* is the agent:
> instead of an LLM choosing tools, the demo drives the engine directly. For a
> **real** agent (GPT-4o via Hermes) getting killed by this same engine — nothing
> scripted — see **[real-kill.md](real-kill.md)**.

## Run it (60 seconds, no API key, no Postgres)

```bash
uv sync
uv run python -m demo
```

This boots an in-process SQLite control plane, drives the engine into a loop,
and files a death certificate:

```text
  HERMESKILL  ·  offline apoptosis demo
  policy: strict   scenario: loop
  ────────────────────────────────────────────────────────────

▸ booting in-process control plane (sqlite, no postgres) …
  ✓ control plane up at http://localhost:8000
▸ registering agent demo-rogue-coder …
  ✓ agent e39b0772-…-6865eb2be8c0 registered

  strict policy caps identical tool calls at 3 — the agent gets stuck
  re-reading the same file and Hermeskill pulls the plug on the 3rd call.

  the agent starts working, then misbehaves:

  01  read_file(path='README.md')                  ok
  02  read_file(path='README.md')                  ok
  03  read_file(path='README.md')                  ☠ LOOP

  ⚡ apoptosis: signature 'read_file|…' repeated 3x in last 3 actions (cap 3)
  block directive → {'action': 'block', 'message': 'hermeskill apoptosis: … End the session.'}

▸ posting death certificate …
  ✓ kill_event #1 filed

  ┌─ DEATH CERTIFICATE ───────────────────────────────────────
  │ agent      e39b0772-…-6865eb2be8c0
  │ trigger    auto / loop
  │ reason     signature 'read_file|…' repeated 3x in last 3 actions (cap 3)
  │ symptoms   1 terminal
  │   • loop  signature 'read_file|…' repeated 3x …
  │ shutdown   1 step(s)
  │   • apoptosis_requested
  └──────────────────────────────────────────────────────────
```

## Scenarios

Each is deterministic and offline. Run with `--scenario <name>` (or `--list`):

| Scenario | Symptom it demonstrates |
|---|---|
| `loop` *(default)* | Identical tool call repeated past the policy cap |
| `cost` | Cumulative LLM cost crosses the policy cap (`token_runaway`) |
| `scope` | Agent calls a tool outside the policy allowlist (`tool_scope_violation`) |
| `wall_clock` | Session exceeds the policy wall-clock cap |
| `manualkill` | Operator override via `hermeskill kill` — the whole operator→agent path, offline |
| `hardkill` | **L3 process supervisor** sends a real SIGTERM→SIGKILL to a wedged child process the cooperative path provably can't touch |

```bash
uv run python -m demo --scenario hardkill
uv run python -m demo --scenario calibrate   # files kills, labels them, prints the calibration report
uv run python -m demo --list
```

## Smoke test

The demo is guarded in CI as an offline smoke test (no Postgres):

```bash
uv run pytest demo/tests -q
```

## Regenerating the demo GIF

The recording is produced with [VHS](https://github.com/charmbracelet/vhs):
`vhs docs/demo.tape` renders `docs/demo.gif` from `python -m demo`. It's fully
deterministic and offline, so the recording runs unattended — no LLM key, no
hardcoded ids.
