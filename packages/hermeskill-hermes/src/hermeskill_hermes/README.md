# hermeskill-hermes

[Hermeskill](https://github.com/theopitori/hermeskill) apoptosis supervision
for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Drops in as
a plugin: Hermeskill watches every tool call and LLM turn in your Hermes session
and terminates the agent cleanly if it enters a runaway loop, exceeds its
cost/token cap, runs past a wall-clock deadline, or calls a tool outside the
policy allowlist.

## Install & enable (zero config)

```bash
# Install Hermes with the Hermeskill plugin in its environment:
uv tool install hermes-agent --with hermeskill-hermes

# Install the Hermeskill CLI (`--with hermes-agent` lets enable-hermes read
# Hermes' config), then put uv's tool dir on PATH and restart your shell:
uv tool install hermeskill --with hermes-agent
uv tool update-shell

# Enable it (one shot — flips plugins.enabled in your Hermes config):
hermeskill enable-hermes
```

That's it. **No API key, no control plane, no env vars.** Hermes auto-discovers
the plugin via the `hermes_agent.plugins` entry-point group; `hermeskill
enable-hermes` adds `hermeskill` to `plugins.enabled`. Run `hermes` and every
session is supervised. When a runaway is killed, the death certificate prints to
your terminal and saves to `~/.hermeskill/kills/`.

> **Why `hermeskill enable-hermes` and not `hermes plugins enable hermeskill`?** The
> latter (and the interactive `hermes plugins` UI) only manage **git-installed**
> plugins under `~/.hermes/plugins/` — they don't see pip/entry-point plugins
> like this one. `hermeskill enable-hermes` writes the supported `plugins.enabled`
> config key for you. (To do it by hand: add `hermeskill` to `plugins.enabled` in
> `~/.hermes/config.yaml`, Windows `%LOCALAPPDATA%\hermes\config.yaml`.)

## Configure (optional — for a control plane)

Everything above works with nothing set. These add control-plane archival, a
fleet view, manual kill, and grants:

```bash
export HERMESKILL_API_KEY=sk-...                                   # ⇒ enables the control plane; unset = local-only
export HERMESKILL_BASE_URL=https://your-control-plane.example.com  # default localhost:8000
export HERMESKILL_AGENT_NAME=my-coding-agent                       # display name
export HERMESKILL_POLICY=coding-default                            # policy
export HERMESKILL_LOCAL_CERT=0                                     # disable the local cert print/save (default: on)
```

Or add the same keys to `~/.hermes/.env`, or run `hermeskill init` once to persist
them to `~/.hermeskill/config.toml`. With a key set, every session is also
queryable via the operator CLI (`hermeskill fleet`).

## What it does

| Condition | What happens |
|-----------|-------------|
| Agent calls the same tool 5× in a row with identical inputs | Kill (`loop`) |
| Cumulative LLM cost exceeds policy cap | Kill (`token_runaway`) |
| Session runs longer than policy wall-clock cap | Kill (`wall_clock`) |
| Agent calls a tool not in the policy allowlist | Kill (`tool_scope_violation`) |
| Operator issues `hermeskill kill <agent_id>` | Kill (`manual_kill`) |
| Operator issues a grant | Suppress one symptom type for up to 24 h |

## How the kill works

Hermes hooks are non-blocking — they can't raise out of the agent loop.
Hermeskill uses Hermes' canonical interception path: when an apoptosis check
fires, the plugin's `pre_tool_call` callback returns

```python
{"action": "block", "message": "hermeskill apoptosis: <reason>. End the session."}
```

Hermes refuses to run the tool and surfaces that message as the tool error
to the LLM. The harm is halted **immediately** — no further tool execution,
no further cost — and every subsequent tool call also blocks until the
agent's loop ends naturally. At session end, `on_session_end` fires and the
plugin posts a death certificate (full symptom log, shutdown sequence,
feedback URL) to the control plane.

This is the same pattern Hermes' built-in `security-guidance` plugin uses
for its strict block mode, and it's documented in PR #26759 as the canonical
interception path for "rate limiting, security restrictions, approval
workflows."

## Policies

Shipped defaults:

| Policy | Loop cap | Cost cap | Wall-clock cap |
|--------|----------|----------|----------------|
| `strict` | 3 repeats / 15 actions | $2.00 | 5 min |
| `coding-default` | 5 repeats / 20 actions | $25.00 | 30 min |
| `permissive` | 10 repeats / 40 actions | $100.00 | 2 h |

## Operator CLI

```bash
hermeskill fleet
hermeskill logs <agent_id>
hermeskill kill <agent_id> --reason "infinite loop in file search"
hermeskill grant <agent_id> --symptoms loop --duration 1h --reason "known flaky task"
hermeskill revoke <grant_id>
```

See the [repo root README](https://github.com/theopitori/hermeskill#readme)
for the full operator workflow, security model, and deployment guide.

## Hermes hooks used

The plugin attaches to five hooks (see `hermes_cli/plugins.py::VALID_HOOKS`):

| Hook | Why |
|---|---|
| `pre_tool_call` | The checkpoint — runs all symptom checks; returns the block directive if armed |
| `post_tool_call` | Records tool outcome; re-runs cost/wall-clock checks |
| `pre_llm_call` | Lifecycle marker (model name) |
| `post_api_request` | Token + cost accounting (this hook carries `usage` in v0.14, not `post_llm_call`) |
| `on_session_end` | Flush death cert, tear down background worker |

## License

[MIT](https://github.com/theopitori/hermeskill/blob/main/LICENSE) © 2026 Hermeskill Contributors
