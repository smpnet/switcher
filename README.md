# switcher

Switch between AI-agent configuration profiles in one command.

`switcher` re-points each managed tool's live config directory at a profile
directory under a state store, so you can move between, say, a "full setup"
with plugins and hooks and a "vanilla" clean slate — credential files are
shared across profiles, so you don't re-authenticate when switching.

Each per-directory swap is atomic (a `replace`-style symlink rename on POSIX,
a junction recreate on Windows — the platform-specific atomicity scope is
covered in the design doc). Multi-dir / multi-tool sequencing is best-effort;
pre-flight validation runs before any mutation, but `init` and `rename` have
narrow documented failure windows where a partial state may need manual
reconciliation. Day-to-day `use` and `save` are the well-trodden paths.

**Day-one tools:** Claude Code, GitHub Copilot CLI. Additional tools are
user-extensible via TOML files (see "Adding a tool").

**Supported OSes:** macOS, Linux, Windows.

## Install

**Prerequisites:** Python 3.13 or newer, `pipx`, and `git`. The HTTPS
install option also needs [`gh`](https://cli.github.com/) for the
credential helper.

The repo is private during the v0.1.0 scaffolding phase, so `pipx` needs an
authenticated path to GitHub. Two safe options — pick whichever matches how
you already authenticate. **Don't embed a personal access token directly in
the URL** (it ends up in shell history and process listings).

### macOS / Linux

```bash
# Option A: SSH (recommended if you already use SSH for GitHub)
pipx install git+ssh://git@github.com/smpnet74/switcher.git@v0.1.0

# Option B: HTTPS via the gh credential helper (no token in argv)
gh auth login                            # one-time
gh auth setup-git                        # registers gh as git's credential helper
pipx install git+https://github.com/smpnet74/switcher.git@v0.1.0

# For a different version: replace @v0.1.0 with the desired tag
# (or omit the @<ref> entirely to install the latest main).
```

### Windows

The same two options work on Windows, but you need a working git auth setup
first. Pick the one matching your existing setup:

- **SSH:** install [Git for Windows](https://git-scm.com/download/win) (ships
  OpenSSH), generate a key, add it to your GitHub account, and ensure
  `ssh-agent` is running. Then use the SSH `pipx install` line above from
  PowerShell or Git Bash.
- **HTTPS via gh:** install [GitHub CLI](https://cli.github.com/), run
  `gh auth login` then `gh auth setup-git`, then use the HTTPS `pipx install`
  line above.

`pipx` itself works the same across macOS, Linux, and Windows (it ships
per-OS bin dirs). The Windows code paths (junctions, `%LOCALAPPDATA%`,
`%USERPROFILE%` env-var expansion) are covered by the test suite, which
runs on a Windows runner in CI (see `.github/workflows/ci.yml`).

### Upgrading

`pipx` remembers the URL the package came from. To pull a newer commit on
the same ref, run `pipx upgrade switcher` — but note that pinned tags
(`@v0.1.0`) won't move past the tag. To switch to a different tag, reinstall
with the new ref (`pipx install --force git+ssh://...@v0.2.0`).

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

# Delete a profile. Active profiles are always refused — switch them with
# `switcher use <other>` first. The --force flag only suppresses the
# interactive y/N prompt; it does not bypass the active-profile check.
switcher delete old-profile
switcher delete old-profile --force

# List supported tools and per-OS config paths
switcher tools

# Print version
switcher version
```

## How it works

`switcher init` performs a one-time setup:

1. Detects which managed tools are installed by looking for an existing
   configuration directory each tool registers.
2. Moves each tool's live config dirs into `<state_dir>/profiles/<dated>-current/`.
3. Creates symlinks (or junctions on Windows) from the original paths back into the profile.
4. Creates a `vanilla` profile containing only credential files — no plugins, hooks, or extensions.
5. Records `<dated>-current` as active for every detected tool.

After that, `switcher use <name>` re-links each managed dir to the new
profile — one atomic per-directory swap each.

**`init` is one-shot.** It snapshots whichever managed tools are installed
at the moment you run it, and then refuses to run again
(`StateAlreadyInitialized`). Two cases worth knowing:

- *No managed tools installed yet:* `init` still succeeds — but with empty
  profiles and no active tools, so `status` will show "no active profiles."
- *Only some managed tools installed:* only those are captured; the rest
  are simply not in the active map.

In both cases, a tool installed *after* `init` is not retroactively picked
up. The recovery path today is to delete the state directory (see the
per-OS table further down) and re-run `switcher init`.

> **Destructive recovery — last resort.** Deleting `<state_dir>` removes
> *all* saved profiles, not just the dated-current snapshot. Always back
> up first if you've built up profiles you care about:
>
> ```bash
> # macOS example — adjust the source path per the per-OS table below
> cp -R "$HOME/Library/Application Support/switcher/profiles" ~/switcher-profiles-backup
> ```
>
> After re-running `switcher init` against a fresh state dir, you can copy
> profile directories back from the backup into the new
> `<state_dir>/profiles/` and they will reappear in `switcher list`. A
> non-destructive "rescan" command is on the v0.2.0 roadmap so this whole
> dance won't be needed.

Profile contents (what `save`/`create`/`use` move around): each profile is
a directory of full per-tool config trees. Credential files (declared in
each tool's `[[credentials]]` block) are *shared across profiles* — `create`
seeds them from the active profile, and `init` carries them into `vanilla`
— so switching profiles never re-prompts for auth. Everything else (plugins,
hooks, settings, history) is profile-specific.

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
config_dir = "gemini"            # references a config_dirs[].profile_subdir
path = "oauth_creds.json"
```

`credentials[].config_dir` is *not* the tool `id` and not a filesystem path —
it must equal the `profile_subdir` of one of the `config_dirs` entries. That
identifies which managed dir the credential file lives in; `path` is then
relative to that dir.

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
