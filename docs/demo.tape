# Caspase demo recording — drives a real Hermes session into a loop kill.
#
# Renders docs/demo.gif: loop-bait prompt -> Caspase block directive ->
# death certificate in `caspase logs`. This is the GIF that belongs at the
# top of the README.
#
# Record with VHS (https://github.com/charmbracelet/vhs):
#
#     vhs docs/demo.tape
#
# PREREQUISITES (the tape does NOT set these up for you — do them once first):
#   1. `uv sync` has been run in the repo root.
#   2. Hermes is authed to a provider (e.g. `uv run hermes auth add openai`,
#      or an ANTHROPIC_API_KEY / OPENROUTER_API_KEY env var is exported).
#   3. The `caspase` plugin is in plugins.enabled (see README step 2).
#   4. The control plane is running in ANOTHER terminal:
#        uv run python -m demo.coding_agent._run_control_plane
#   5. These env vars are exported in the shell VHS launches:
#        CASPASE_API_KEY=sk_dev_developer_local_only_do_not_ship
#        CASPASE_BASE_URL=http://localhost:8000
#        CASPASE_POLICY=strict
#
# Tune the Sleep durations to match your model's latency before publishing.

Output docs/demo.gif

Set Shell powershell
Set FontSize 18
Set Width 1200
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

# --- The setup, shown briefly so viewers know the policy is tight ---
Type "# strict policy: 3 identical calls and Caspase pulls the plug"
Enter
Sleep 1500
Type "echo $env:CASPASE_POLICY"
Enter
Sleep 1200

# --- The money shot: bait Hermes into an identical-args read loop ---
Type 'uv run hermes chat -q "Read README.md six times with read_file, identical args every call. Do not summarise between calls."'
Enter

# Hermes turns + tool calls happen here. Caspase records each read,
# fires the loop check on the 3rd, and blocks the 4th with the apoptosis
# directive. Give the model room to run; adjust to taste.
Sleep 18s

# --- The autopsy ---
Type "uv run caspase fleet"
Enter
Sleep 3s

# Copy the agent id printed by `fleet` into the logs call. Hardcode the
# real id here before recording so the GIF runs unattended.
Type "uv run caspase logs <agent_id>"
Enter
Sleep 5s

# Let the final death-certificate frame breathe before the loop restarts.
Sleep 3s
