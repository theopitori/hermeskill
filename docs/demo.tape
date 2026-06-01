# Hermeskill demo recording — the offline, one-command loop kill.
#
# Renders docs/demo.gif: `python -m demo` boots an in-process control plane,
# drives the real engine into a loop, issues the block directive, and files a
# death certificate. This is the GIF that belongs at the top of the README.
#
# Unlike the old tape, this needs NO LLM key, NO Postgres, NO running control
# plane, and NO hardcoded agent id — the demo is fully self-contained and
# deterministic, so `vhs docs/demo.tape` runs unattended and reproducibly.
#
# Record with VHS (https://github.com/charmbracelet/vhs):
#
#     uv sync          # once, so the workspace venv exists
#     vhs docs/demo.tape
#
# The only tuning knob is the Sleep below — the demo runs in well under a
# second, so the Sleeps are purely for human-readable pacing, not to wait on a
# model.

Output docs/demo.gif

Set Shell powershell
Set FontSize 18
Set Width 1200
Set Height 820
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

# --- The whole story, in one command ---
Type "# strict policy: 3 identical tool calls and Hermeskill pulls the plug"
Enter
Sleep 1500
Type "uv run python -m demo"
Enter

# The demo boots the control plane, runs the rogue loop, files the death
# certificate, and prints the autopsy — all locally, in ~1s. Give the frames
# room to breathe so a viewer can read the kill + certificate.
Sleep 6s

# Let the final death-certificate frame linger before the loop restarts.
Sleep 3s
