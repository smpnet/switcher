# switcher

Switch between AI-agent configuration profiles in one command.

`switcher` atomically re-points each managed tool's live config directory at a
profile directory under a state store, so you can move between, say, a "full
setup" with plugins and hooks and a "vanilla" clean slate — without losing
credentials and without re-authenticating.

**Day-one tools:** Claude Code, GitHub Copilot CLI. Additional tools are
user-extensible via TOML files (see "Adding a tool").

**Supported OSes:** macOS, Linux, Windows.

## Install

The repo is private during the v0.1.0 scaffolding phase, so installing requires
an authenticated GitHub remote (SSH key or personal access token). Pick whichever
matches how you already authenticate to GitHub:

```bash
# SSH (recommended if you already use SSH for GitHub)
pipx install git+ssh://git@github.com/smpnet74/switcher.git@v0.1.0

# HTTPS with a personal access token (PAT must have repo:read on this repo)
pipx install git+https://<token>@github.com/smpnet74/switcher.git@v0.1.0

# Latest main (substitute for the @v0.1.0 ref above)
pipx install git+ssh://git@github.com/smpnet74/switcher.git
```

`switcher` lands on PATH globally. Works the same on macOS, Linux, and Windows
(pipx ships per-OS bin dirs).

## Usage

```bash
# One-time setup: detect installed tools, snapshot current config, create vanilla.
switcher init

# Show what's active for each tool
switcher status

# List every profile (active ones marked with *)
switcher list

# Switch all tools to vanilla
switcher use vanilla

# Switch only Claude
switcher use vanilla --only claude

# Snapshot whatever is live right now
switcher save before-experiment

# Create an empty profile that inherits credentials from the active profile
switcher create experiment
switcher use experiment

# See which profile a tool uses
switcher which claude

# Rename (auto-relinks if active)
switcher rename experiment client-A

# Delete (refuses if profile is active; --force skips confirmation)
switcher delete old-profile
switcher delete old-profile --force

# List supported tools and per-OS config paths
switcher tools

# Print version
switcher version
```

## How it works

`switcher init` performs a one-time setup:

1. Detects which AI tools are installed (by checking their first config dir).
2. Moves each tool's live config dirs into `<state_dir>/profiles/<dated>-current/`.
3. Creates symlinks (or junctions on Windows) from the original paths back into the profile.
4. Creates a `vanilla` profile containing only credential files — no plugins, hooks, or extensions.
5. Records `<dated>-current` as active for every detected tool.

After that, `switcher use <name>` is a single atomic symlink swap per managed dir.

The state directory is chosen by `platformdirs.user_data_dir("switcher")`:

| OS | Path |
|---|---|
| Linux | `~/.local/share/switcher` |
| macOS | `~/Library/Application Support/switcher` |
| Windows | `%LOCALAPPDATA%\switcher` |

Override with `SWITCHER_STATE_DIR=<path>`.

## Adding a tool

Drop a TOML in `<state_dir>/registry.d/` matching the schema. Generate a stub
with `switcher tools scaffold <id>`:

```bash
switcher tools scaffold gemini
# Edit the stub at <state_dir>/registry.d/gemini.toml
switcher tools         # confirms the new tool shows up
```

The TOML schema (full form):

```toml
id = "gemini"
name = "Gemini CLI"

[[config_dirs]]
posix_path = "~/.gemini"
windows_path = "%USERPROFILE%\\.gemini"
profile_subdir = "gemini"
env_override = "GEMINI_HOME"   # optional

[[credentials]]
config_dir = "gemini"
path = "oauth_creds.json"
```

For single-dir tools, the shorthand also works:

```toml
id = "gemini"
name = "Gemini CLI"
credential_files = ["oauth_creds.json"]   # auto-expands

[[config_dirs]]
posix_path = "~/.gemini"
windows_path = "%USERPROFILE%\\.gemini"
profile_subdir = "gemini"
```

## Development

This is a [pixi](https://pixi.sh) workspace.

```bash
pixi install              # one-time setup
pixi run test             # unit tests
pixi run test-integration
pixi run test-e2e
pixi run lint
pixi run typecheck
pixi run ci               # all of the above + verify-windows + build
pixi run build            # build wheel into dist/
```

## License

MIT — see [LICENSE](LICENSE).
