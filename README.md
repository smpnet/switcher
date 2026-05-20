<p align="center">
  <img src="docs/hero.webp" alt="switcher" width="640">
</p>

# switcher

Switch between AI-agent configuration profiles in one command.

You have one `~/.claude` directory. But you want different setups for different
work — a stripped-down config for client A, a heavyweight one with plugins and
hooks for personal projects, a vanilla one when you're debugging. `switcher`
lets you snapshot, swap between, and recover from those configurations without
re-authenticating each time you switch.

**Day-one tools:** Claude Code, GitHub Copilot CLI, and OpenAI Codex CLI ship
as built-in registry entries. Tools are user-extensible via TOML in
`<state_dir>/registry.d/` — see [`docs/MANUAL.md`](docs/MANUAL.md#adding-a-tool).

**Supported OSes:** macOS, Linux, Windows.

---

## Requirements

- **Python 3.13 or newer.**
  - macOS: `brew install python@3.13`
  - Linux: your distro's `python3.13` package, or `pyenv install 3.13`
  - Windows: the [official installer](https://www.python.org/downloads/)
- **pipx.**
  - macOS: `brew install pipx`
  - Linux: distro package, or `python3 -m pip install --user pipx`
  - Windows: `py -m pip install --user pipx`
  - Then run `pipx ensurepath` once to add pipx's bin dir to your PATH.
- **git.** Anything recent.
- **GitHub auth.** This repo is private. You need either an SSH key registered
  with GitHub OR the [`gh` CLI](https://cli.github.com/) authenticated and
  configured as git's credential helper.

## Install

Pick the path that matches how you already auth with GitHub:

```bash
# SSH (if you have a GitHub-registered key in your agent)
pipx install git+ssh://git@github.com/smpnet/switcher.git@v0.1.5

# OR via the gh credential helper
gh auth login          # one-time
gh auth setup-git      # one-time: registers gh as git's credential helper
pipx install git+https://github.com/smpnet/switcher.git@v0.1.5
```

Verify:

```bash
switcher version       # → switcher v0.1.5
```

---

## Your first session

> **⚠ Run `switcher init` only AFTER the tools you want to manage are already
> installed.** `init` captures whatever's on disk at the moment you run it.
> Tools you install later get picked up via `switcher rescan` separately.

```bash
switcher init
```

What just happened on disk:

1. Each detected tool's live config dir (e.g. `~/.claude`, `~/.copilot`) was
   moved into `<state_dir>/profiles/<today>-current/`.
2. Symlinks at the original paths now point into that profile.
3. A second `vanilla` profile was created carrying just your credential files
   (auth tokens, API keys) — your starting-from-scratch baseline.
4. The "active" map records each detected tool as currently using
   `<today>-current`.

Confirm it — for example, with both Claude and Copilot installed:

```bash
switcher status
# claude  → 2026-05-15-current
# copilot → 2026-05-15-current

switcher list
# * 2026-05-15-current
#   vanilla
```

If only one tool was installed when you ran `init`, only that tool appears in
`status`; the rest is the same.

You can keep working with Claude as usual — the symlink is invisible to the
tool. `~/.claude/settings.json` is still the file Claude reads; it just
happens to live under the switcher state directory now.

---

## Branching: experiments

Suppose you want to try a different Claude setup — a new `CLAUDE.md`, a different
`settings.json`, an experimental plugin — without losing your current state.

```bash
switcher save baseline           # snapshot RIGHT NOW into a profile named "baseline"
switcher create experiment       # new profile, credentials copied from current
switcher use experiment          # flip: ~/.claude now points into "experiment"
```

You're now running on the `experiment` profile. Any changes you make to
`~/.claude` (new files, edits to settings, plugin installs) write into the
experiment profile's data. The `baseline` profile stays frozen.

Compare runs by flipping:

```bash
switcher use baseline            # back to baseline; experiment data is preserved
switcher use experiment          # forward again
```

**Per-tool experiments.** If both Claude and Copilot are managed but you only
want to branch Claude:

```bash
switcher use experiment --only claude   # only Claude flips; Copilot stays put
```

The `experiment` profile carries both tools' metadata, but `use --only claude`
only swaps Claude's symlinks.

---

## Walking back

Three escape hatches, least to most aggressive.

### 1. Switch to a known-good profile

```bash
switcher use baseline
```

The day-to-day undo. The `experiment` profile still exists; you can come back
to it later. Use this when you just want to step away from an experiment.

### 2. Stop managing a single tool

```bash
switcher unmanage copilot
```

Restores `~/.copilot` to a real directory (the data from whichever profile was
currently active becomes the live config) and removes Copilot from switcher's
active map. Subsequent `switcher use` calls won't touch it. Claude stays
managed. To bring Copilot back later: `switcher rescan --only copilot`.

### 3. Uninstall switcher entirely

```bash
switcher uninstall              # every managed live path → real directory; state dir preserved
switcher uninstall --purge      # same, plus `rm -rf <state_dir>`
```

`uninstall` restores every managed config path to a real directory. Without
`--purge`, your profiles remain on disk under the state directory — you could
reinstall switcher later and rebuild. With `--purge`, the state directory is
also removed; nothing persists.

Note: this is separate from removing the Python package itself. To remove the
`switcher` binary: `pipx uninstall switcher`.

---

## Reference

This README covers the happy path. For the full command catalog, flag matrices,
recovery procedures when things go wrong, the state-directory layout, and how
to register a new tool, see [`docs/MANUAL.md`](docs/MANUAL.md).

## Contributing

Bug reports, feature requests, and PRs are welcome. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the dev environment setup, the test
layout, the spec→plan→implementation workflow, commit conventions, and a
walkthrough for adding a built-in tool.

## License

MIT — see [LICENSE](LICENSE).
