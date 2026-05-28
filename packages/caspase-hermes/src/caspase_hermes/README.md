# caspase-hermes

[Caspase](https://github.com/theopidori/caspase) apoptosis supervision
for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Drops in as
a plugin: Caspase watches every tool call and LLM turn in your Hermes session
and terminates the agent cleanly if it enters a runaway loop, exceeds its
cost/token cap, runs past a wall-clock deadline, or calls a tool outside the
policy allowlist.

## Install

```bash
pip install caspase-hermes
```

Hermes auto-discovers the plugin via the `hermes_agent.plugins` entry-point
group — no directory copy required. Plugins are opt-in, so enable it by adding
`caspase` to `plugins.enabled` in your Hermes config:

```yaml
# ~/.hermes/config.yaml  (Windows: %LOCALAPPDATA%\hermes\config.yaml)
plugins:
  enabled:
    - caspase
```

> **Note:** `hermes plugins enable caspase` and the interactive `hermes
> plugins` UI only manage **git-installed** plugins under `~/.hermes/plugins/`.
> They do not list pip-installed (entry-point) plugins like this one — enable
> those via the `plugins.enabled` config key above. Once the name is there,
> Hermes discovers and loads the plugin automatically at session start.

## Configure

```bash
export CASPASE_API_KEY=sk-...
export CASPASE_BASE_URL=https://your-control-plane.example.com  # optional, default localhost:8000
export CASPASE_AGENT_NAME=my-coding-agent                       # optional display name
export CASPASE_POLICY=coding-default                            # optional policy
```

Or add the same keys to `~/.hermes/.env`.

## Run

```bash
hermes
```

Caspase activates automatically. Every session is queryable via the operator
CLI (`caspase fleet`).

## What it does

| Condition | What happens |
|-----------|-------------|
| Agent calls the same tool 5× in a row with identical inputs | Kill (`loop`) |
| Cumulative LLM cost exceeds policy cap | Kill (`token_runaway`) |
| Session runs longer than policy wall-clock cap | Kill (`wall_clock`) |
| Agent calls a tool not in the policy allowlist | Kill (`tool_scope_violation`) |
| Operator issues `caspase kill <agent_id>` | Kill (`manual_kill`) |
| Operator issues a grant | Suppress one symptom type for up to 24 h |

## How the kill works

Hermes hooks are non-blocking — they can't raise out of the agent loop.
Caspase uses Hermes' canonical interception path: when an apoptosis check
fires, the plugin's `pre_tool_call` callback returns

```python
{"action": "block", "message": "caspase apoptosis: <reason>. End the session."}
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
caspase fleet
caspase logs <agent_id>
caspase kill <agent_id> --reason "infinite loop in file search"
caspase grant <agent_id> --symptoms loop --duration 1h --reason "known flaky task"
caspase revoke <grant_id>
```

See the [repo root README](https://github.com/theopidori/caspase#readme)
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

[MIT](https://github.com/theopidori/caspase/blob/main/LICENSE) © 2026 Caspase Contributors
