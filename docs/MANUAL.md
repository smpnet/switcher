# switcher — manual

The full command catalog, flag matrices, recovery procedures, and architecture
reference. If you're new, start with the [README](../README.md) for the
guided journey, then come back here when you need the details.

---

## Table of contents

- [Switch profiles](#switch-profiles)
- [Save and manage profiles](#save-and-manage-profiles)
- [Add another tool later](#add-another-tool-later)
- [Stop managing one tool](#stop-managing-one-tool)
- [Stop using switcher entirely](#stop-using-switcher-entirely)
- [Inspect and diagnose](#inspect-and-diagnose)
- [Maintenance and recovery](#maintenance-and-recovery)
  - [Destructive recovery (last resort)](#if-everything-else-fails-destructive-recovery)
  - [Migrating from the legacy two-dir Copilot builtin](#migrating-from-the-legacy-two-dir-copilot-builtin)
- [How it works](#how-it-works)
- [Hot-swap hazards](#hot-swap-hazards)
- [Adding a tool](#adding-a-tool)
- [Upgrading switcher](#upgrading-switcher)

---

## Switch profiles

```bash
switcher use vanilla                  # all currently-managed tools
switcher use vanilla --only claude    # one tool only
```

`use` switches **only currently-managed tools**. Tools removed via `unmanage`
stay removed across `use` calls — the durability invariant added in v0.1.4.
To bring a tool back under management, run `switcher rescan --only <tool>`.

> **⚠ Don't run `switcher use` while a managed tool has a live session open.**
> The swap is global, not shell-scoped — running tools see the symlink change
> and end up with state split across both profiles, silently. See
> [Hot-swap hazards](#hot-swap-hazards) below.

---

## Save and manage profiles

`save` and `create` are different operations:

- `switcher save <name>` snapshots the **current live config** of every managed
  tool into a new profile.
- `switcher create <name>` scaffolds a new **empty** profile, seeded with the
  active profile's credential files. Use this when you want a fresh profile to
  populate from scratch.

```bash
switcher save before-experiment       # capture the moment, then experiment
switcher create experiment            # empty profile, credentials seeded
switcher use experiment

switcher rename experiment client-A   # auto-relinks active profiles
switcher delete old-profile           # refused if active; switch off first
switcher delete old-profile --force   # suppresses y/N prompt; active still blocks
```

---

## Add another tool later

`init` is a one-shot. To bring a newly-installed tool under management:

```bash
switcher rescan --only gemini         # canonical "add a tool" verb
switcher rescan                       # TTY-interactive: yes/no per detected tool
switcher rescan --all                 # non-interactive, capture everything
```

If `rescan` is interrupted by a process kill or transient FS error,
`switcher status` reports it and you resolve with `switcher rescan --continue`
(finish the partial capture) or `switcher rescan --abort` (reverse it).

---

## Stop managing one tool

```bash
switcher unmanage copilot             # restores ~/.copilot to a real directory,
                                      # removes Copilot from the active map
```

Subsequent `use` calls won't re-activate Copilot. To bring it back, run
`switcher rescan --only copilot`.

---

## Stop using switcher entirely

```bash
switcher uninstall                    # every live symlink → real directory;
                                      # state directory preserved
switcher uninstall --purge            # same, plus `rm -rf <state_dir>`
```

This restores `switcher`-managed config paths to real directories and
(optionally) removes the state directory. It does **not** uninstall the
`switcher` Python package — for that, run `pipx uninstall switcher`.

---

## Inspect and diagnose

```bash
switcher status                       # active profile per tool
switcher status -v                    # plus cached live-path state
switcher which claude                 # which profile a tool is on
switcher tools                        # registered tools, INSTALLED + MANAGED columns
switcher list                         # all profiles, active marked with *
switcher version                      # package version
```

The `tools` `INSTALLED` column is `✓` when the tool's live config dir exists;
`MANAGED` is `✓` when the tool is in the active map.

---

## Maintenance and recovery

```bash
switcher prune                        # delete profiles not active for any tool
switcher prune --dry-run
switcher prune --force                # skip the confirmation prompt
```

**Interrupted operations.** v0.1.5 adds guided recovery for interrupted `init`,
`rename`, and `rescan` operations via a per-state-directory op-log journal:

- `rename` auto-compensates on the next `switcher` command — idempotent
  roll-forward, no user input.
- `init` and `rescan` interruptions are surfaced by `switcher status` and
  resolved with `--continue` / `--abort` flags on the matching command.
- `--abort` is best-effort: it refuses to act on ambiguous on-disk states
  rather than guessing.

### If everything else fails: destructive recovery

When the normal commands can't repair the state — a half-finished `init` that
even `--abort` can't resolve, dangling links after a manual `rm -rf`, registry
drift past what `unmanage` handles — fall through to the destructive procedure.

The v0.1.0 wipe procedure is retained for cases where `uninstall` can't run —
e.g. live config paths manually broken, state-dir contents corrupted past
recognition, or a half-finished `init` that `uninstall`'s classifier rejects.
This is an exceptional manual procedure with multiple failure modes, not a
routine operation. **You will lose every saved profile, your live tool config,
AND any user-added tool registry entries if you do not back up the entire
`<state_dir>` first.** The `<dated>-current` profile is where your real
Claude / Copilot / Codex config lives after `init` (the live `~/.claude`,
`~/.copilot`, `~/.codex`, etc. are just symlinks into it);
`<state_dir>/registry.d/` holds user-added tool definitions;
`<state_dir>/profiles/` holds every saved profile. Read the whole procedure
before running any of the steps.

1. **Record the active profile per tool, then back up the entire state
   directory.** The active profile is what each tool's live config actually
   points at right now — and it may differ across tools (e.g. Claude on
   `vanilla`, Copilot on `experiment`). Restoring from the wrong profile
   silently discards newer changes:

   ```bash
   switcher status      # capture this output — it tells you which
                        # profile to restore from for each tool
   ```

   Then back up the whole state tree (not just `profiles/`) so user-added
   registry entries and the active map come along:

   ```bash
   # macOS example — adjust the source path per the per-OS table below
   cp -R "$HOME/Library/Application Support/switcher" ~/switcher-state-backup
   ```

   > **🔒 The backup contains credentials.** Per the seed-not-share model,
   > every profile in `<state_dir>/profiles/` carries its own copy of every
   > tool's credential files (OAuth tokens, API keys, etc.). Treat
   > `~/switcher-state-backup` as secret material: restrict permissions, do
   > not commit it, and delete it once recovery is complete. On POSIX:
   > `chmod -R go-rwx ~/switcher-state-backup` after copying.

2. **Restore each tool's active-profile config to its live path before
   wiping.** After `init`/`use`, each managed tool's live config dir is a
   symlink/junction pointing into `<state_dir>`. If you delete `<state_dir>`
   while those links exist, they dangle — and `switcher init` will then
   refuse to run with `AlreadyLinkedError`. For each tool, look at the profile
   name from step 1's `switcher status` output and restore from
   `profiles/<that-profile>/<config_subdir>/`. The link-removal step is
   OS-specific:

   > **One `<active-profile>` per tool, not one for the whole step.** If
   > `switcher status` showed `claude → vanilla` and `copilot → experiment`,
   > restore Claude from `profiles/vanilla/claude/` and Copilot from
   > `profiles/experiment/copilot/`. Substituting the same value for both is
   > the most likely way to lose work.

   ```bash
   # macOS / Linux — symlinks: use `rm` (NOT rmdir).
   # Substitute <active-profile> per `switcher status` output for this tool.
   rm ~/.claude
   cp -R ~/switcher-state-backup/profiles/<active-profile>/claude ~/.claude
   ```

   ```powershell
   # Windows — junctions: use `rmdir` from cmd, or Remove-Item from PowerShell.
   # `rm`/`del` will fail or behave unexpectedly on a junction.
   cmd /c rmdir "$env:USERPROFILE\.claude"
   Copy-Item -Recurse "$env:USERPROFILE\switcher-state-backup\profiles\<active-profile>\claude" "$env:USERPROFILE\.claude"
   ```

   Repeat for every tool listed in `switcher status` — each may need a
   different `<active-profile>` source.

3. **Verify, then wipe `<state_dir>` and re-run `switcher init`.** Before
   wiping, confirm each tool's live config path is now a real directory (not a
   symlink/junction) — `ls -lh ~/.claude` on POSIX or `Get-Item ~/.claude |
   Select Mode` in PowerShell will show this. If any path is still a link,
   repeat step 2 for that tool. With real config dirs at every live path,
   init captures every installed tool fresh. After init, run
   `switcher status` to confirm the expected active profiles per tool before
   moving on to step 4.

4. **Restore user-added registry entries and additional profiles.** Copy any
   TOMLs from your backup's `registry.d/` into the new
   `<state_dir>/registry.d/` so user-added tools are recognized again, then
   copy non-current profile directories from your backup's `profiles/` into
   the new `<state_dir>/profiles/`. Restored profiles reappear in
   `switcher list` but are inert until you `switcher use <name>` them — the
   fresh `init` resets the active map to the new dated-current; prior active
   state is not preserved.

   **Note on credentials in restored profiles.** Per the seed-not-share
   credential model described in [How it works](#how-it-works), a restored
   profile carries the credentials it was created with — which may be stale
   if tokens have rotated since the backup. If `switcher use <restored>`
   followed by the tool's first action triggers a re-auth prompt, that's
   expected; completing the auth updates the live config dir, which IS the
   restored profile while it's active, so the new credentials persist in
   that profile.

### Migrating from the legacy two-dir Copilot builtin

v0.1.4 rewrites the bundled `copilot` builtin to target the standalone
`copilot` binary's single config dir (`~/.copilot`). Profiles created before
v0.1.4 — when the builtin captured both `~/.copilot` AND
`~/.config/github-copilot` (POSIX) / `%LOCALAPPDATA%\github-copilot`
(Windows) — keep working: their cached live paths still resolve and the
`copilot-auth/` subdir under each profile is harmless dead data (switcher no
longer visits it).

If you want to fully migrate to the single-dir shape and drop the legacy
`copilot-auth/` subdir from new profiles:

```bash
switcher unmanage copilot                  # restores both legacy live paths
switcher rescan --only copilot             # captures just ~/.copilot
```

The unmanage step's pre-flight uses cached live paths, so registry drift
doesn't break it. After `rescan`, new profiles created from the standalone
Copilot CLI's data carry only `copilot-config/`.

If you use the deprecated `gh copilot` extension instead, see
[Adding a tool](#adding-a-tool) below — register it as a user-local tool with
the explicit two-dir shape rather than re-using the `copilot` id.

---

## How it works

`switcher init` performs a one-time setup:

1. Detects which managed tools are installed by looking for an existing
   configuration directory each tool registers.
2. Moves each tool's live config dirs into
   `<state_dir>/profiles/<dated>-current/`.
3. Creates symlinks (or junctions on Windows) from the original paths back
   into the profile.
4. Creates a `vanilla` profile containing only credential files — no plugins,
   hooks, or extensions.
5. Records `<dated>-current` as active for every detected tool.

After that, `switcher use <name>` re-links each managed dir to the new profile
— one atomic per-directory swap each.

**Atomicity.** Each per-directory swap is atomic (a `replace`-style symlink
rename on POSIX gives kernel-level atomicity; on Windows the implementation
hedges by removing the existing junction and recreating it, which is *not* a
single atomic operation but completes in well under the typical observation
window — see `scripts/verify_junction.py` for the probabilistic atomicity
probe). Multi-dir / multi-tool sequencing is best-effort; pre-flight
validation runs before any mutation, and v0.1.5's op-log journal gives guided
recovery if a multi-step operation is interrupted.

**`init` is one-shot.** It snapshots whichever managed tools are installed at
the moment you run it, and then refuses to run again
(`StateAlreadyInitialized`). Two cases worth knowing:

- *No managed tools installed yet:* `init` still succeeds — but with empty
  profiles and no active tools, so `status` will show "no active profiles."
- *Only some managed tools installed:* only those are captured; the rest are
  simply not in the active map.

In both cases, a tool installed *after* `init` is not retroactively picked up
by `init` itself. Use `switcher rescan` to capture newly-installed tools.

**Profile contents.** Each profile is a directory of full per-tool config
trees. Credential files (declared in each tool's `[[credentials]]` block) are
*seeded across profiles*, not shared at runtime — `create` copies them in
from whichever profile is currently active, and `init` carries them into
`vanilla`. After seeding, each profile owns its own credential files; if you
re-auth while a profile is active, that profile's copy is updated, but other
profiles' copies remain untouched. The seeding model is what gives the
"switch without re-auth" guarantee day-to-day. Everything else (plugins,
hooks, settings, history) is profile-specific from the start.

**State directory location.** Chosen by
`platformdirs.user_data_dir("switcher")`:

| OS | Path |
|---|---|
| Linux | `~/.local/share/switcher` |
| macOS | `~/Library/Application Support/switcher` |
| Windows | `%LOCALAPPDATA%\switcher` |

Override with `SWITCHER_STATE_DIR=<path>`.

---

## Hot-swap hazards

`switcher use` rewrites a single global symlink per managed tool. That symlink
is process-global — every shell, every running tool, every process on your
account shares it. There is no per-terminal or per-session scoping.

If a managed tool has a live session running when you call `switcher use`,
the running process does not switch cleanly. It ends up in **split-brain**:

| Process behavior at the time of swap | Where the I/O lands afterward |
|---|---|
| Open file descriptors (sqlite, session logs, transcripts) | Original profile — fds are inode-pinned at open time |
| Path-resolved syscalls under the tool's config dir (`mkdir`, `open`, `stat`) | New profile — symlink re-resolves on every call |
| In-memory cached config | Original profile |
| Fresh config reads from disk | New profile |

The running process gets a mix of "original" and "new" state with **no error
signal** — no `database is locked`, no crash, no warning. The session keeps
working; it just silently writes some things to the wrong place.

This is **not specific to any one tool.** Any switcher-managed tool whose
running session does path-based file operations under its config dir will
behave this way — Codex skills writing memory files, Claude Code skills /
hooks / MCP-config reloads, Copilot mid-session config refreshes, and so on.

**Practical rule.** Switch profiles between runs of a tool, not during. The
supported flow is:

```
switcher use baseline
codex                  # do work
# exit codex
switcher use exp1
codex                  # do exp1 work
```

The unsupported flow (silent corruption):

```
# terminal A
switcher use baseline
codex                  # working on baseline

# terminal B (while codex still running)
switcher use exp1
codex                  # running concurrently — terminal A is now split-brain
```

You cannot run two profiles side by side, cannot leave a tool open across a
profile switch, and cannot switch profiles in one shell while the same tool
is doing work in another. Each violation is silent. This is an inherent
property of the symlink-swap model, not a bug — the same model is what
keeps switcher invisible to the tools themselves (they never need to know
the dir is symlinked).

---

## Adding a tool

Beyond listing what's registered, `switcher tools` also has a `scaffold`
subcommand for generating new registry entries. To add a tool, drop a TOML in
`<state_dir>/registry.d/` matching the schema — generate a stub with
`switcher tools scaffold <id>`:

> **Same `init`-is-one-shot caveat applies.** Registering a new tool TOML
> after `init` makes it visible to `switcher tools` and `switcher list`, but
> it is NOT automatically captured into existing profiles or the active map.
> Run `switcher rescan` to capture the newly-registered tool's live config
> dir into a fresh `<today>-rescan-N` profile (or `switcher rescan --into
> <profile>` to consolidate into an existing profile).

```bash
switcher tools scaffold gemini
# Edit the stub at <state_dir>/registry.d/gemini.toml
switcher tools         # confirms the registry entry loads (schema validation only;
                       # does not check whether the target paths exist on disk)
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
`config_dirs[0].profile_subdir`). This is the common case — the shorthand is
intended for single-dir tools, but it also works for multi-dir tools whose
credentials happen to all live in the first dir. If credentials live in a
non-first config dir, use the explicit `[[credentials]]` form so you can name
the right `config_dir`:

```toml
id = "gemini"
name = "Gemini CLI"
credential_files = ["oauth_creds.json"]   # auto-expands

[[config_dirs]]
posix_path = "~/.gemini"
windows_path = "%USERPROFILE%\\.gemini"
profile_subdir = "gemini"
```

A worked multi-dir example (illustrative — not a real tool). Suppose some
hypothetical CLI keeps shell config in `~/.foocli/` but stashes its OAuth
token in `~/.config/foocli/auth.json`. The shorthand wouldn't fit (the
credential isn't in the first registered dir), so write the `[[credentials]]`
block explicitly and set `config_dir` to the matching `profile_subdir`:

```toml
id = "foocli"
name = "Foo CLI"

[[config_dirs]]
posix_path = "~/.foocli"
windows_path = "%USERPROFILE%\\.foocli"
profile_subdir = "foocli"

[[config_dirs]]
posix_path = "~/.config/foocli"
windows_path = "%APPDATA%\\foocli"
profile_subdir = "foocli-xdg"

[[credentials]]
config_dir = "foocli-xdg"        # matches the second config_dirs entry
path = "auth.json"
```

### Worked example: deprecated `gh copilot` extension

The bundled `copilot` builtin targets the standalone `copilot` binary (single
config dir at `~/.copilot`). The deprecated `gh copilot` extension uses two
dirs and is **not** bundled — register it as a user-local tool at
`<state_dir>/registry.d/gh-copilot.toml` with a distinct id so it doesn't
collide with the standalone builtin:

```toml
id = "gh-copilot"
name = "GitHub Copilot (gh extension, deprecated)"

[[config_dirs]]
posix_path = "~/.config/github-copilot"
windows_path = "%LOCALAPPDATA%\\github-copilot"
profile_subdir = "gh-copilot-auth"

[[config_dirs]]
posix_path = "~/.copilot"
windows_path = "%USERPROFILE%\\.copilot"
profile_subdir = "gh-copilot-config"

[[credentials]]
config_dir = "gh-copilot-auth"
path = "apps.json"
```

> **Conflict with the standalone CLI.** Both products use `~/.copilot`. If
> you have both installed, complete the migration to the standalone CLI
> first (uninstall the deprecated extension, install standalone), then run
> `switcher rescan --only copilot`. Running both side-by-side against the
> same `~/.copilot` will produce confused state.

---

## Upgrading switcher

`pipx` remembers the URL the package came from. To pull a newer commit on
the same ref, run `pipx upgrade switcher` — but note that pinned tags
(`@v0.1.5`) won't move past the tag. To switch to a different tag, reinstall
with the new ref:

```bash
pipx install --force git+ssh://git@github.com/smpnet/switcher.git@<tag-or-commit>
```

While the repo is private, both upgrade and reinstall still need the same
GitHub auth that worked at install time (SSH key in your agent, or
`gh auth status` showing a valid login). If `pipx upgrade switcher` fails
with a `git clone`-style auth error, that's a GitHub auth problem, not a
`switcher` bug — refresh the SSH agent or re-run `gh auth login`.

### Windows-specific install notes

The two install options work on Windows, but you need a working git auth
setup first. Pick the one matching your existing setup:

- **SSH:** install [Git for Windows](https://git-scm.com/download/win)
  (ships OpenSSH), generate a key, add it to your GitHub account, and ensure
  `ssh-agent` is running. Then use the SSH `pipx install` line from the
  README from PowerShell or Git Bash.
- **HTTPS via gh:** install [GitHub CLI](https://cli.github.com/), run
  `gh auth login` then `gh auth setup-git`, then use the HTTPS `pipx
  install` line from the README.

`pipx` itself works the same across macOS, Linux, and Windows (it ships
per-OS bin dirs). The Windows code paths (junctions, `%LOCALAPPDATA%`,
`%USERPROFILE%` env-var expansion) are covered by the test suite, which
runs on a Windows runner in CI (see `.github/workflows/ci.yml`).
