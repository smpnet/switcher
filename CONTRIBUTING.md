# Contributing to switcher

Thanks for considering a contribution. This document covers the
dev environment, test layout, review expectations, commit
conventions, and a walkthrough for adding a built-in tool.

## Quick start (for changes you can ship today)

```bash
# 1. Clone (or fork + clone).
git clone https://github.com/<owner>/switcher.git
cd switcher

# 2. Install the toolchain via pixi. One-time setup.
pixi install

# 3. Make your change in src/, add tests in tests/.

# 4. Run the local gate before pushing.
pixi run check

# 5. Push, then open a PR — via the GitHub web UI or `gh pr create`.
git push -u origin <your-branch>
```

`pixi run check` is the only required local gate. Everything else
in this doc is context for non-trivial contributions.

## Dev environment

This is a [pixi](https://pixi.sh) workspace. The toolchain is
declared in `pixi.toml`; `pixi install` materializes it.

Per-task commands:

| Task | When to run |
|---|---|
| `pixi run test` | Unit tests (`tests/unit/`) |
| `pixi run test-integration` | Integration tests (`tests/integration/`) — real filesystem, full lifecycle |
| `pixi run test-e2e` | End-to-end tests via subprocess |
| `pixi run lint` | Ruff lint + format check |
| `pixi run format` | Auto-fix Ruff formatting |
| `pixi run typecheck` | basedpyright |
| `pixi run verify-windows` | Static check of Windows path TOMLs (skip-cleanly on non-Windows) |
| `pixi run check` | Everything in the CI test job (lint + typecheck + all test tiers + verify-windows) |
| `pixi run build` | Build wheel + sdist into `dist/` |
| `pixi run verify-junction` | Manual: Windows junction atomicity probe (not in CI) |

`pixi run check` is what CI runs against your PR. `pixi run build`
is the Linux-only wheel build that runs in a separate CI job.

## Test layout

Three test tiers, each with its own pytest mark and conventions:

- **`tests/unit/**`** — TDD-style. Behavior-named test functions
  (`test_unmanage_refuses_unknown_tool`), one assertion per behavior,
  no mocks unless unavoidable. Uses `tmp_path` fixtures; never
  touches real `$HOME`. Cross-platform branches gate on
  `sys.platform == "win32"`. See `.coderabbit.yaml` `path_instructions`
  for the full review pointer.
- **`tests/integration/**`** — marked `pytestmark =
  pytest.mark.integration`. Real filesystem via `tmp_home`/`tmp_state`
  fixtures (never real `$HOME`). May shell out via `subprocess`.
  Asserts disk state across full lifecycles (init → use → save →
  rescan → unmanage → uninstall), not just return values.
- **`tests/e2e/**`** — marked `pytestmark = pytest.mark.e2e`.
  Subprocess-driven; exercise the installed CLI binary end-to-end.

Adding a new tool registry entry, op-log shape, or service-method
behavior typically touches all three tiers.

## How features get designed and built

Non-trivial changes — anything touching 3+ files or changing
user-visible behavior — should include a short design rationale.
The maintainer's workflow uses:

- **Specs** at `docs/superpowers/specs/<date>-<topic>-design.md`
  (note: `docs/superpowers/` is gitignored — these are local
  working notes; the maintainer references them in PR descriptions
  rather than committing them).
- **Briefs** at `docs/<version>.md` for milestone-scoped roadmaps
  (these ARE committed; see `docs/v0.1.4.md`, `docs/v0.1.5.md`).
- **Optional tooling:** if you use Claude Code, the
  `superpowers:brainstorming` / `superpowers:writing-plans` /
  `superpowers:executing-plans` skills automate the workflow.
  External contributors are not required to use them — a
  well-written PR description with motivation, design notes,
  and a test plan covers the same ground.

For a worked example, see the v0.1.4 brief (`docs/v0.1.4.md`)
and the resulting PR (`#5`).

## Pre-PR (local) review loops

### Required before PR

`pixi run check` must pass. This runs lint, typecheck, all test
tiers, and verify-windows. It's the only required local gate.

### Optional public tooling

The public **CodeRabbit CLI** (free signup at coderabbit.ai) runs
a mechanical-review pass over your local diff before push —
unused imports, weak types, missing docstrings, edge-case gaps.
You don't need to install it: the CodeRabbit bot on the PR side
runs the same review automatically after `gh pr create`.

### Maintainer-only local loops

The project maintainer additionally runs `abby-review` — a private
Hermes-based intent-review container against the local diff. It is
**not required for external contributors** and not installable
without the maintainer's local Hermes setup. The PR-side bots
(below) cover intent review for outside contributions.

## Open-PR review cycle

After `gh pr create`, two bots review every PR automatically:

- **CodeRabbit bot:** mechanical and structural review comments
  per push. Configured at the repo level; runs on forks too.
- **Hermes webhook:** intent-level review comments per push.
  Also runs on every PR including fork-PRs.

To merge, you'll need to resolve actionable findings from both.
**Push back on findings you disagree with** — reply on the
GitHub thread with technical reasoning. Don't blindly apply every
suggestion; "right for this codebase" is the bar.

Bundle local fixes into one push per review cycle. Multiple rapid
pushes retrigger both bots and produce noisy comment history
("review flapping").

No installation required to receive these reviews. They run on
GitHub.

## Commit conventions

Conventional Commits format. Use one of these prefixes:

- `feat:` — new features or capabilities
- `fix:` — bug fixes
- `docs:` — documentation changes
- `chore:` — maintenance, dependency updates, tooling
- `refactor:` — code cleanup without behavior change
- `test:` — adding or updating tests
- `style:` — formatting, whitespace
- `perf:` — performance improvements
- `ci:` — CI/CD configuration changes

Format: `<prefix>: <description>` — lowercase after the colon,
under 70 characters for the subject line, body wraps at 72.

Examples from this repo:
- `feat: add per-tool selection at init time`
- `fix: rescan grammar in dry-run output`
- `ci: bump actions/checkout SHA pin to current v4`
- `refactor: extract op-log classifier helper`

Branch naming: `feat/<topic>` for features, `fix/<topic>` for
bugfixes. Stable releases use `vX.Y.Z` tags (see Release flow).

**Don't add `Co-Authored-By:` lines to commits** — repo convention.

## Adding a built-in tool

This is distinct from "Adding a tool" in the README, which covers
user-local TOMLs under `<state_dir>/registry.d/`. A built-in tool
ships as part of the package and is auto-detected on `init`.

Built-in treatment makes sense for **broadly-adopted CLIs with a
stable config-dir contract** (e.g. Claude Code, GitHub Copilot CLI,
OpenAI Codex CLI). Niche or evolving tools should stay user-local.

To add a builtin:

1. **Drop a TOML in `src/switcher/builtins/<id>.toml`.** Use the
   schema documented in the README's "Adding a tool" section.
2. **Pick a stable `profile_subdir`.** This name persists on disk
   under every profile; once a user has profiles with that subdir,
   renaming the subdir would break `use(old-profile)`. The v0.1.4
   Copilot rewrite is the cautionary tale: the live config dir
   was renamed but `profile_subdir = "copilot-config"` stayed put.
3. **Add an integration test** in `tests/integration/` asserting
   the full capture/restore cycle: init detects the tool, profile
   dir gets the right subdir, `use` swaps the symlink correctly,
   uninstall restores the real directory. *Exception:* if the
   builtin introduces no new behavioral surface (single-`config_dirs`
   entry, no credentials shorthand variation, no env-override edge
   case beyond what existing builtins exercise), seed its first live
   dir in `tests/conftest.py`'s `tmp_home` fixture instead. Most of
   the lifecycle (init / use / save / create / delete / rename) is
   then picked up automatically by
   `tests/integration/test_lifecycle.py::test_full_lifecycle`, which
   iterates `for tool in registry`. Uninstall is **not** in
   `test_full_lifecycle`'s sequence — add the new tool's live path
   to the assertion list in
   `tests/integration/test_uninstall.py::test_uninstall_default_restores_real_dirs_and_clears_active`
   so uninstall regressions surface for the new tool too. Codex is
   the worked example.
4. **Add the tool to `scripts/verify_windows_paths.py`** if its
   Windows path uses an environment variable that needs expansion.
5. **For deprecated upstream variants** (e.g. `gh copilot` ext vs
   standalone `copilot`), document the deprecated variant as a
   user-local TOML in the README's "Adding a tool" section,
   not as a second builtin. Builtins are for *current* upstream
   shapes.

## Architecture overview

One-page map of the codebase:

- **`models.py`** — Pydantic v2 models. Cross-platform name
  validation; `credential_path` validation; the `Tool` /
  `DirMapping` / `Credential` shapes.
- **`paths.py`** — `PathResolver`: the single source of truth for
  `expand()`, `tool_dir()`, `state_dir()`, `is_link()`. Uses
  host-platform env-var syntax; rejects `~username`.
- **`links.py`** — POSIX symlinks / Windows junctions. `swap_link`
  is kernel-atomic on POSIX; on Windows it hedges by remove +
  recreate (not strictly atomic but completes in well under typical
  observation windows — see `scripts/verify_junction.py`).
  `move_or_seed_dir` has four cases: missing→seed empty,
  link→AlreadyLinkedError, file→PathNotADirectoryError, real-dir→
  atomic move.
- **`store.py`** — persistence layer only. `profile_dir()` is the
  single path constructor; routes every name through
  `validate_safe_name`. Atomic writes via tmp+rename.
- **`registry.py`** — two-layer override (builtins + user TOMLs
  from `<state_dir>/registry.d/`). User TOML check runs BEFORE
  builtin check.
- **`service.py`** — policy layer. `ProfileService`. Owns
  multi-step operations (`init`, `use`, `save`, `rename`,
  `delete`, `rescan`, `uninstall`, `unmanage`). v0.1.5 adds the
  op-log compensation methods.
- **`oplog.py`** (v0.1.5) — op-log journal for guided recovery of
  interrupted `init` / `rename` / `rescan`. Pydantic discriminated
  union over `_InitOp` / `_RenameOp` / `_RescanOp`. Per-mapping
  disk-state classifier with four states (`COMPLETE` /
  `MOVE_DONE_LINK_MISSING` / `UNTOUCHED` / `AMBIGUOUS`).
- **`errors.py`** — `SwitcherError` hierarchy. Class names end in
  `Error` (ruff N818). New exceptions inherit from `SwitcherError`.
- **`cli.py`** — Typer app. `handle_errors` decorator catches
  `SwitcherError` → stderr + exit 1. `get_deps()` is the test
  override seam. `pretty_exceptions_enable=False` is intentional.
  Mutating commands flow through `ProfileService`; `list` /
  `status` / `tools` hit the store directly. v0.1.5 adds the
  `_detect_or_compensate_oplog` helper at the top of every command.

Cross-link: the `reviews.path_instructions` block in
`.coderabbit.yaml` documents the per-module review pointers —
those ARE the architectural invariants for each module and they
evolve with the code.

## Release flow

Releases are tag-triggered (see
`.github/workflows/release.yml`). To cut a release:

```bash
git tag v0.1.5
git push origin v0.1.5
```

Release notes live in `docs/RELEASE.md` — append a new entry
per the existing format before tagging.

**Caveat: rc1 + stable on the same commit.** `hatch-vcs` picks up
the rc1 tag if you tag stable on the same commit as a prior rc1.
Either land an empty-but-meaningful commit between rc1 and
stable, or delete the rc1 tag before tagging stable.

## Code of conduct / contact

Open an issue for design discussion or bug reports. Send a PR for
code contributions.
