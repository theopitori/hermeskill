# stasis-hermes

[Caspase](https://github.com/seijeupessoal-ui/Stasis) apoptosis supervision for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). Drops in as a
plugin: Caspase watches every tool call and LLM turn in your Hermes session and
terminates the agent cleanly if it enters a runaway loop, exceeds its
cost/token cap, runs past a wall-clock deadline, or calls a tool outside the
policy allowlist.

## Install

```bash
pip install stasis-hermes
```

Then drop the plugin into Hermes:

```bash
python -c "
import stasis_hermes, pathlib, shutil
src = pathlib.Path(stasis_hermes.__file__).parent
dst = pathlib.Path.home() / '.hermes' / 'plugins' / 'caspase'
shutil.copytree(src, dst, dirs_exist_ok=True)
print('installed →', dst)
"
```

## Configure

```bash
export STASIS_API_KEY=sk-...
export STASIS_BASE_URL=https://your-control-plane.example.com  # optional
export STASIS_AGENT_NAME=my-coding-agent                       # optional display name
export STASIS_POLICY=coding-default                            # optional policy
```

Or add the same keys to `~/.hermes/.env`. The env-var prefix is still
`STASIS_*` until the code rename lands and clients have had a deprecation
window.

## Run

```bash
hermes
```

Caspase activates automatically. Every session appears in the dashboard.

## What it does

| Condition | What happens |
|-----------|-------------|
| Agent calls the same tool 5× in a row with identical inputs | Kill (`loop`) |
| Cumulative LLM cost exceeds policy cap | Kill (`token_runaway`) |
| Session runs longer than policy wall-clock cap | Kill (`wall_clock`) |
| Agent calls a tool not in the policy allowlist | Kill (`tool_scope_violation`) |
| Operator issues `stasis kill <agent_id>` | Kill (`manual_kill`) |
| Operator issues a grant | Suppress one symptom type for up to 24 h |

On kill, Caspase posts a death certificate with a full symptom log, shutdown
sequence, and a one-click feedback URL so operators can label the verdict.

## Policies

Shipped defaults:

| Policy | Loop cap | Cost cap | Wall-clock cap |
|--------|----------|----------|----------------|
| `coding-default` | 5 repeats / 20 actions | $2.00 | 30 min |
| `coding-permissive` | 8 repeats / 40 actions | $10.00 | 2 h |

## Operator CLI

```bash
stasis agents list
stasis logs <agent_id>
stasis kill <agent_id> --reason "infinite loop in file search"
stasis grants create <agent_id> --symptom loop --duration 1h --reason "known flaky task"
stasis grants revoke <grant_id>
```

See the [repo root README](https://github.com/seijeupessoal-ui/Stasis#readme)
for the full operator workflow, security model, and deployment guide.

## License

[MIT](https://github.com/seijeupessoal-ui/Stasis/blob/main/LICENSE) © 2026 Caspase Contributors
