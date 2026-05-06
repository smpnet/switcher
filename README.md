# switcher

Switch between AI-agent configuration profiles in one command.

`switcher` re-points each managed tool's live config directory at a profile
directory under a state store, so you can move between, say, a "full setup"
with plugins and hooks and a "vanilla" clean slate. Credential files are
copied into each new profile at create-time (seeded from whichever profile
is active), so day-to-day switching doesn't re-prompt for auth — every
profile starts life carrying the same credential snapshot.

Each per-directory swap is atomic (a `replace`-style symlink rename on POSIX,
a junction recreate on Windows — the platform-specific atomicity scope is
covered in the design doc). Multi-dir / multi-tool sequencing is best-effort;
pre-flight validation runs before any mutation, but `init` and `rename` have
narrow documented failure windows where a partial state may need manual
reconciliation. Day-to-day `use` and `save` are the well-trodden paths.

**Day-one tools:** Claude Code and GitHub Copilot CLI ship as built-in
registry entries (in `src/switcher/builtins/`). Additional tools are
user-extensible via TOML files dropped into `<state_dir>/registry.d/`
(see "Adding a tool").

**Supported OSes:** macOS, Linux, Windows.

## Install

**Prerequisites:** Python 3.13 or newer (matches the project's pinned
target in `pyproject.toml` and the pixi dev environment), `pipx`, and
`git`. The HTTPS install option also needs
[`gh`](https://cli.github.com/) for the credential helper.

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

While the repo is private, both upgrade and reinstall still need the same
GitHub auth that worked at install time (SSH key in your agent, or
`gh auth status` showing a valid login). If `pipx upgrade switcher` fails
with a `git clone`-style auth error, that's a GitHub auth problem, not a
`switcher` bug — refresh the SSH agent or re-run `gh auth login`.

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

> **⚠ Destructive recovery — last resort.** This is an exceptional
> manual procedure with multiple failure modes, not a routine
> operation. **You will lose every saved profile, your live tool
> config, AND any user-added tool registry entries if you do not back
> up the entire `<state_dir>` first.** The `<dated>-current` profile is
> where your real Claude/Copilot config lives after `init` (the live
> `~/.claude` etc. are just symlinks into it); `<state_dir>/registry.d/`
> holds user-added tool definitions; `<state_dir>/profiles/` holds every
> saved profile. Read the whole procedure before running any of the
> steps. A non-destructive `rescan` command on the v0.2.0 roadmap will
> replace this dance.
>
> 1. **Back up the entire state directory** — non-negotiable first step.
>    Copy the whole tree, not just `profiles/`, so user-added registry
>    entries and the active map come along:
>    ```bash
>    # macOS example — adjust the source path per the per-OS table below
>    cp -R "$HOME/Library/Application Support/switcher" ~/switcher-state-backup
>    ```
>    Find the actual `<dated>-current` directory name (referenced
>    throughout the steps below) by running `switcher status` before
>    wiping, or by `ls "$HOME/Library/Application Support/switcher/profiles"`
>    — it'll be something like `2026-05-06-current`.
> 2. **Restore the active tool configs to their live paths first.** After
>    `init`, each managed tool's live config dir is a symlink/junction
>    pointing into `<state_dir>`. If you delete `<state_dir>` while those
>    links exist, they dangle — and `switcher init` will then refuse to
>    run with `AlreadyLinkedError`. Before wiping, remove the link and
>    copy each captured tool's config back to its live path. The link-
>    removal step is OS-specific:
>    ```bash
>    # macOS / Linux — symlinks: use `rm` (NOT rmdir)
>    rm ~/.claude
>    cp -R ~/switcher-state-backup/profiles/<dated>-current/claude ~/.claude
>    ```
>    ```powershell
>    # Windows — junctions: use `rmdir` from cmd, or Remove-Item from PowerShell.
>    # `rm`/`del` will fail or behave unexpectedly on a junction.
>    cmd /c rmdir "$env:USERPROFILE\.claude"
>    Copy-Item -Recurse "$env:USERPROFILE\switcher-state-backup\profiles\<dated>-current\claude" "$env:USERPROFILE\.claude"
>    ```
>    Repeat for every tool that `init` originally captured (check `switcher
>    status` output before wiping to know which tools are managed).
> 3. **Wipe `<state_dir>` and re-run `switcher init`.** With real config
>    dirs at the live paths, init captures every installed tool fresh.
> 4. **Restore user-added registry entries and additional profiles.**
>    Copy any TOMLs from your backup's `registry.d/` into the new
>    `<state_dir>/registry.d/` so user-added tools are recognized again,
>    then copy non-current profile directories from your backup's
>    `profiles/` into the new `<state_dir>/profiles/`. Restored profiles
>    reappear in `switcher list` but are inert until you
>    `switcher use <name>` them — the fresh `init` resets the active map
>    to the new dated-current; prior active state is not preserved.
>
>    **Note on credentials in restored profiles.** Per the seed-not-share
>    credential model described in "How it works," a restored profile
>    carries the credentials it was created with — which may be stale
>    if tokens have rotated since the backup. If `switcher use <restored>`
>    followed by the tool's first action triggers a re-auth prompt,
>    that's expected; completing the auth updates the live config dir,
>    which IS the restored profile while it's active, so the new
>    credentials persist in that profile.

Profile contents (what `save`/`create`/`use` move around): each profile is
a directory of full per-tool config trees. Credential files (declared in
each tool's `[[credentials]]` block) are *seeded across profiles*, not
shared at runtime — `create` copies them in from whichever profile is
currently active, and `init` carries them into `vanilla`. After seeding,
each profile owns its own credential files; if you re-auth while a profile
is active, that profile's copy is updated, but other profiles' copies
remain untouched. The seeding model is what gives the "switch without
re-auth" guarantee day-to-day. Everything else (plugins, hooks, settings,
history) is profile-specific from the start.

The state directory is chosen by `platformdirs.user_data_dir("switcher")`:

| OS | Path |
|---|---|
| Linux | `~/.local/share/switcher` |
| macOS | `~/Library/Application Support/switcher` |
| Windows | `%LOCALAPPDATA%\switcher` |

Override with `SWITCHER_STATE_DIR=<path>`.

## Adding a tool

Beyond listing what's registered, `switcher tools` also has a `scaffold`
subcommand for generating new registry entries. To add a tool, drop a TOML
in `<state_dir>/registry.d/` matching the schema — generate a stub with
`switcher tools scaffold <id>`:

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

For tools whose credential files all live in their first registered config
directory, a `credential_files` shorthand works in place of the
`[[credentials]]` block (it auto-expands at registry-load time, anchored to
`config_dirs[0].profile_subdir`). This is the common case — the shorthand
is intended for single-dir tools, but it also works for multi-dir tools
whose credentials happen to all live in the first dir. If credentials live
in a non-first config dir, use the explicit `[[credentials]]` form so you
can name the right `config_dir`:

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
