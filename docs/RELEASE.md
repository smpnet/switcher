# Releasing switcher

Operational doc for cutting a release. Recipe-first; rationale below.

## Release notes

### v0.1.7 — 2026-05-20

**Added**

- OpenAI Codex CLI as a built-in tool. `switcher init` on a fresh state-store now detects `~/.codex` automatically — or the path indicated by `CODEX_HOME` if it is set in the user's environment, matching codex's own upstream resolution (the same pattern switcher already uses for `CLAUDE_CONFIG_DIR`). Existing initialized state-stores can bring codex under management with `switcher rescan --only codex`.

**Internal**

- `tests/conftest.py` `tmp_home` fixture seeds `~/.codex` so `test_full_lifecycle` covers the new builtin generically (see CONTRIBUTING §"Adding a built-in tool" step 3 carve-out).

**Schema**

- **No state-file schema change.** v0.1.7 reads and writes the v0.1.6 shape unchanged. Codex's TOML uses only `config_dirs`; the `config_files` schema added in v0.1.6 is not exercised by this builtin.

---

### v0.1.6 — 2026-05-20

**Added**

- Profile-isolated `~/.claude.json`. Claude Code keeps MCP servers (`.mcpServers`, `.projects[].mcpServers`) and the OAuth account (`.oauthAccount`) in a single JSON file alongside `~/.claude/`; switcher now isolates those subtrees per profile while leaving everything else (lastSessionId, onboarding flags, telemetry counters) machine-global. Switching profiles no longer leaks MCPs across personas, and a fresh-cloned machine restores per-project MCP registrations through the same flow.
  - New `ConfigFile` registry entry on the bundled `claude` tool declares the owned JSON subtrees and the on-disk snapshot location (`<profile>/.switcher/config_files/claude/claude.json`).
  - `save` / `use` / `init` / `rescan` capture the owned subtrees on save and overlay them on use via a narrow jq-ish walker (`.key`, `[]` over JSON objects, composition); never-extracted live keys are preserved as machine-global.
  - Missing snapshot ⇒ warn-and-skip on apply (lets older v0.1.5 profiles roll forward without wiping live state on the first `use`; run `switcher save` to materialize a snapshot for an existing profile).
  - Op-log compensation extended: `init --continue` / `init --abort` / `rescan --continue` / `rescan --abort` recover ConfigFile snapshots alongside the existing dir mappings, driven from a journaled `_ConfigFileMappingIntent` so registry drift between intent-write and recovery doesn't change the captured shape.
- Coverage gate in CI: 85% total line coverage + 90% diff-cover patch coverage (excludes `*cli.py`). Regressions on either dimension fail the `Coverage (pytest + diff-cover)` check before merge.
- README hero image (`docs/hero.webp`).

**Changed**

- README rewritten as a user journey (install → init → switch → save → maintain). Reference material (full command list, flags, schema) moved to `docs/MANUAL.md`. The README now answers "what is this and how do I start" inside one screen.
- `ProfileService.detect_installed()` reports a tool installed when EITHER its first `config_dir` exists OR any of its `config_files` live paths exist. A Claude Code user whose machine carries only `~/.claude.json` (no `~/.claude/` directory yet) now goes through the normal `init` / `rescan` flow instead of being silently filtered out.
- `init()` runs a pure-read ConfigFile preflight (symlink rejection, regular-file gate, UTF-8 + JSON-object parse, owned-path walker against live) BEFORE any dir mutation. A deterministic ConfigFile validation failure aborts cleanly instead of leaving the dated profile persisted, live dirs flipped to managed links, and an in-flight oplog record behind.
- The bundled `claude` builtin now declares its `ConfigFile` entry pointing at `~/.claude.json` (POSIX) / `%USERPROFILE%\.claude.json` (Windows) with `owned_json_paths = [".mcpServers", ".projects[].mcpServers", ".oauthAccount"]`.

**Fixed**

- ConfigFile snapshot containment hardening: a symlink/junction at any reserved ancestor (`<state>/profiles/<name>`, `.switcher`, `.switcher/config_files`, `.switcher/config_files/<subdir>`) is rejected at the path-builder chokepoint (`FileProfileStore.config_file_snapshot_path`) and surfaced as `AMBIGUOUS` by the op-log classifier. Closes a class of "snapshot I/O escapes the state store via redirected ancestor" reproductions that the leaf-only `is_symlink` check missed.
- `Tool` model rejects duplicate `config_dirs[*].profile_subdir` within a tool. Pre-fix, two dir mappings sharing a `profile_subdir` deterministically tripped `ProfileTargetExistsError` mid-init AFTER the first live dir had already been swapped to a link.
- Cross-tool ConfigFile uniqueness uses the same expansion semantics as `PathResolver.expand` (`~`, `~/...`, `~\...`, `$HOME/...`, `${HOME}/...`, `%USERPROFILE%\...`) so two tools can't silently co-manage the same `~/.claude.json` through different spellings.
- POSIX `canonicalize_path_for_uniqueness` only folds the leading separator after `~`, not arbitrary backslashes inside the path body. Pre-fix, `/tmp/foo\bar.json` and `/tmp/foo/bar.json` collapsed to the same uniqueness key on POSIX despite resolving to different live files.

**Internal**

- New `src/switcher/json_paths.py`: the owned-path grammar parser, `extract_owned_paths`, `apply_owned_paths`, and `validate_snapshot_against_owned_paths` (used at every owned-path entry point so shape-incompatible snapshots fail at the validation boundary rather than mid-merge).
- `src/switcher/links.py` gains `atomic_write_file` (tmp-file + `rename` with mode preservation + symlink rejection) and `snapshot_ancestor_link` (the reserved-ancestor containment helper).
- `src/switcher/oplog.py`: new `ConfigFileDiskState` three-state classifier (`COMPLETE` / `UNTOUCHED` / `AMBIGUOUS`) and `_ConfigFileMappingIntent` model journaled into `_InitOp` / `_RescanOp` records.
- Service boundary catches `UnsupportedWalkTargetError` AND `InvalidOwnedPathError` at every walker entry point (`_capture_config_files`, `_extract_and_write_config_file_snapshot`, `_plan_config_file_applies`); normalized to `StorageError` with file context.
- Removed `docs/v0.1.3.md` from git tracking (file no longer used; was leftover from the v0.1.3 release flow).

**Schema**

- New reserved subtree per profile: `<state_dir>/profiles/<name>/.switcher/config_files/<subdir>/<filename>`. `validate_safe_name` rejects leading dots, so no tool's `profile_subdir` can collide. Existing v0.1.5 profiles have no `.switcher/` subdir; `use` warns and preserves live for those profiles until a `save` materializes the snapshot.
- `oplog.json` records (`_InitOp`, `_RescanOp`) gain an optional `config_file_mappings: list[_ConfigFileMappingIntent]` field. Empty list is the v0.1.5-equivalent shape; older switcher versions reading the file ignore the unknown key. Forward-compat is preserved.
- `config.json` unchanged.

---

### v0.1.5 — 2026-05-15

**Added**

- Guided recovery for interrupted `init`, `rename`, and `rescan` operations via a per-state-dir op-log journal at `<state_dir>/oplog.json`. Reduces manual recovery when a command is interrupted (process kill, crash, mid-op FS error).
  - `rename` auto-compensates on the next state-touching `switcher` command — idempotent roll-forward, no user input. (`switcher version` intentionally bypasses the hook.)
  - `init` and `rescan` interruptions are surfaced by `switcher status` (and any other state-touching command) and resolved with new `--continue` / `--abort` recovery flags. These are the only new CLI flags in v0.1.5; everyday workflow is unchanged.
  - Best-effort restoration: `--abort` uses per-mapping `original_kind` metadata to avoid corrupting originally-missing paths and refuses on ambiguous on-disk states rather than guessing.
- CI build job now produces a downloadable `switcher-dist` artifact (wheel + sdist) per push, built once on `ubuntu-latest` instead of three times across the OS matrix.
- New `CONTRIBUTING.md` at the repo root covering dev environment setup, test layout, the spec→plan→implementation workflow, pre-PR and open-PR review loops, commit conventions, a walkthrough for adding a built-in tool, and an architecture overview.

**Changed**

- README `## Usage` rewritten as eight task-oriented sections: start, switch, save and manage, add a tool, stop managing one, stop entirely, inspect, maintain. The lifecycle is now the primary framing. README `## Development` collapses to a one-paragraph `## Contributing` pointer.
- CI test matrix invokes `pixi run check` (was `pixi run ci`). `pixi run ci` remains the local-dev one-stop and is unchanged.
- Pinned action SHAs bumped: `actions/checkout` to the current v4 SHA, `prefix-dev/setup-pixi` to the current v0.8.x SHA. Pixi version pin unchanged (still `v0.66.0`).

**Internal**

- New `src/switcher/oplog.py` module: Pydantic models for `_InitOp` / `_RenameOp` / `_RescanOp` (discriminated union), `MappingDiskState` four-state classifier (`COMPLETE` / `MOVE_DONE_LINK_MISSING` / `UNTOUCHED` / `AMBIGUOUS`), and an `OpLogIO` thin wrapper for atomic read/append/mark-completed/vacuum operations.
- `service.py` `init` / `rename` / `rescan` write an intent record before any FS mutation and mark it completed after the final atomic state write. Compensation derives progress from disk on every pass — never from in-record progress booleans — so a crash between any FS step and the subsequent journal update is handled deterministically.
- New CLI `_detect_or_compensate_oplog` hook invoked at the top of every state-touching command callback (`switcher version` is the one intentional exception per spec §5.2): auto-compensates `_RenameOp` records on every such command (incl. read-only `status`), surfaces in-flight `_InitOp`/`_RescanOp` as exit 3 (read-only) or `InitInProgressError`/`RescanInProgressError` (mutating), and vacuums completed records on mutating commands.
- Two `.coderabbit.yaml` `custom_checks` added (mode: error): active-profile-guard on `delete`, orphan-relink on `rename`. Merge-blocking on regression.

**Schema**

- **No state-file schema change.** `config.json` is unchanged. The new `oplog.json` is a sibling file at the state-dir level; older switcher versions ignore it. Op-log file absence is the normal pre-v0.1.5 state and remains a valid v0.1.5 state.

---

### v0.1.4 — 2026-05-11

**Added**

- `switcher init --only <ids>` / `--skip <ids>` / `--interactive` — declare which tools switcher should manage at init time.
- `switcher unmanage <tool>` — restore a single tool's live path(s) and remove it from the active map.
- `switcher tools` now shows `INSTALLED` and `MANAGED` columns alongside ID / Name / Paths. Pathological "managed but live path missing" rows render as `⚠` with a footer pointer to `unmanage` or restore.
- `switcher rescan` (no flags) is now per-tool interactive on a TTY; new `--all` flag forces non-interactive capture-all (suppresses the non-TTY warning).

**Changed**

- `switcher use <profile>` now defaults to switching only currently-managed tools (`profile.tools ∩ active.keys()`). Makes `unmanage` durable across profile switches.
- `switcher save` now snapshots only managed tools. Tools installed AFTER `init` are no longer implicitly added to new snapshots — run `switcher rescan --only <tool>` first.
- The bundled `copilot` builtin now targets the standalone `copilot` binary (single `~/.copilot` config dir, no credentials block). Existing profile data carrying the legacy `copilot-auth` subdir keeps working — the subdir is dead data, not visited. To migrate a legacy profile fully to the new shape, run `switcher unmanage copilot` then `switcher rescan --only copilot`.
- `switcher status` on an empty active map now distinguishes the two reachable empty-states. After init, when every tool has been unmanaged (or after `uninstall` without `--purge`), it prints `"No tools currently managed. Run 'switcher rescan' to discover installed tools."` Pre-init (no profiles on disk yet) it prints `"No tools currently managed. Run 'switcher init' to set up switcher."` — the previous wording recommended `rescan` even when `rescan` would error with `StateNotInitializedError`.

**Fixed**

- Legacy state migration (v0.1.0–v0.1.2 → v0.1.3+) now uses filesystem inspection over the current registry, bounded by per-tool historical metadata (a `_HISTORICAL_PROFILE_SUBDIRS` map of subdir names plus per-platform `_HISTORICAL_LIVE_PATHS_POSIX` / `_HISTORICAL_LIVE_PATHS_WINDOWS` tables of EXACT full live-path templates). Fixes the orphan-symlink data-loss path from v0.1.3 PR #4 review.
- `rescan --dry-run` output grammar: "would capture X" (was: "would captured X").
- `unmanage <tool> --force` on an orphan-no-cache tool (no registry entry, no cached live_paths) now refuses when any owned profile subdir for the tool still has data on disk. The previous behavior dropped the tool from the active map, hiding it from `uninstall --purge`'s skipped-tool guard — and the subsequent purge silently destroyed the data. The user's escape: restore the registry TOML and re-run normal `unmanage`, or delete the subdir(s) manually first.
- `_classify_uninstall_mapping` SYMLINK branch tightened: a regular file at the link's `profile_target` is now classified as CORRUPT in pre-flight instead of being passed through to `copytree()` (which would crash mid-uninstall).

**Internal**

- `UninstallMappingState` enum renamed from `_UninstallMappingState`. CLI switches from `.value` string compares to enum compares.
- `.coderabbit.yaml` `path_instructions` added for `tests/unit/**` and `tests/integration/**`.
- FS-truth discovery uses EXACT candidate paths instead of a (parents × basenames) cross-product: replaced `_candidate_parents_for` + `_candidate_live_basenames_for` with a single `_candidate_live_paths_for` returning concrete `Path` objects. Eliminates a destructive false-positive class where a user-created symlink at one of the phantom cross-product paths (e.g. `~/github-copilot`) whose target resolved into an owned profile subdir would be classified as managed and later mutated by `uninstall`.
- New sentinel tests (`tests/unit/test_migration_metadata_sentinel.py`) keep the historical-metadata tables in sync with the bundled builtins: every current `profile_subdir` must be representable in `_HISTORICAL_PROFILE_SUBDIRS`, and every entry in the per-platform full-path tables must expand cleanly AND not duplicate a current registry path. Future builtin path rewrites surface here.
- CLI (`switcher.cli`) reconfigures `sys.stdout` / `sys.stderr` to UTF-8 (`errors="replace"`) at import time so the tools-table glyphs (`✓ — ⚠`) and Rich's box-drawing characters survive Windows' default cp1252 stdout encoding without `UnicodeEncodeError`.

**Schema**

- **No state-file schema change.** v0.1.4 reads and writes the v0.1.3 shape unchanged.

---

## How to cut a release

Sync local `main` first, then tag and push:

```bash
git fetch origin
git switch main
git pull --ff-only
git tag -a v0.1.X -m "v0.1.X"
git push origin v0.1.X
# then watch the Release workflow run on the Actions tab
```

The workflow builds and publishes automatically; no further manual steps. The GitHub Release object will appear under [Releases](../../releases) with the `.whl` and `.tar.gz` attached and auto-generated notes.

> **Always sync first.** The release workflow only verifies that the tag commit is *an ancestor of* `main` (not that it equals the tip), so a stale local `main` will publish whatever older commit was checked out. The fetch + ff-only pull preamble is the safe default for every recipe in this doc.
>
> **Intentional back-tagging is an explicit exception.** If you specifically want to release an older `main` commit (for instance, to ship a hotfix from a known-good earlier point when newer work isn't ready), skip the sync, check out the target commit, and tag from there. The ancestor check in `release.yml` allows it; the burden is on the operator to know they're doing it.

> **Do not use `git push --tags`.** It pushes every local tag, which can accidentally trigger the release workflow on stale or experimental tags that happen to be sitting in the local repo (especially after RCs and hotfixes accumulate). Push the exact tag, every time.

## Prerelease recipe

For a release candidate, append `-rc1`, `-rc2`, etc. (or `-alpha1` / `-beta1`). Sync `main` first (same reason as above):

```bash
git fetch origin
git switch main
git pull --ff-only
git tag -a v0.1.X-rc1 -m "v0.1.X-rc1"
git push origin v0.1.X-rc1
```

The release workflow detects the suffix and creates the Release with the **Pre-release** label — it does **not** become the "Latest release" badge on the repo home page.

**For the first release after meaningful changes to `release.yml`, `pyproject.toml`'s build configuration, or `pixi.toml`'s task aggregates, cut `vX.Y.Z-rc1` first to validate the pipeline before the stable tag.** Workflow bugs surface on the prerelease, not on the public stable release.

If the rc1 build is broken, fix on `main`, sync, then force-move the tag (keep it annotated — `-fa` not `-f`, otherwise you silently degrade to a lightweight tag):

```bash
git fetch origin
git switch main
git pull --ff-only
git tag -fa v0.1.X-rc1 -m "v0.1.X-rc1"
git push --force origin v0.1.X-rc1
```

(Tag protection allows admin bypass per the existing tag ruleset.) Iterate until rc1 is clean, then cut the stable tag.

## Hotfix recipe

Bug found post-release? Fix on `main` via a normal PR, then sync, tag the next patch version, and ship. For example, if v0.1.2 just shipped and you're cutting v0.1.3 as a hotfix:

```bash
git fetch origin
git switch main
git pull --ff-only
git tag -a v0.1.3 -m "v0.1.3"
git push origin v0.1.3
```

(Substitute the actual next-patch tag for your situation. The `+1` notation is *not* a valid tag string under the release workflow's tag-shape regex; always use a concrete `vX.Y.Z`.)

Release branches become the answer when feature work for the next minor accumulates on `main` *before* a hotfix is wanted. We're not there yet — for now, hotfixes go on `main`.

## Semver guidance for switcher

Decision rule, switcher-specific:

- **Patch** (`v0.1.2` → `v0.1.3`): bug fixes; doc-only changes; CI/release-engineering updates that don't change runtime behavior.
- **Minor** (`v0.1.x` → `v0.2.0`): additive features — new commands, new options that don't break existing flows, new managed tools added as built-ins.
- **Major** (`v0.x.y` → `v1.0.0` and beyond): breaking CLI changes (renamed/removed commands, changed option semantics), state-format changes that require migration, registry-schema breaking changes.

**0.x leeway:** additive features can land in patch bumps when they share a coherent thesis with the milestone. Example: v0.1.3 ships `uninstall` + `rescan` + `prune` as a coherent recovery-story group on a patch bump because the three commands together resolve a single user-facing problem (broken init recoveries are no longer destructive).

## Trunk-based development

PRs target `main`, accumulate on `main`, and the **tag is the release boundary**. There are no release branches today.

For outside contributors: "which release does my PR ship in?" → whichever release tag comes after the merge. If `main` carries unreleased work at the time of your merge, your change ships in the next tag along with everything else accumulated since the last tag.

## What's NOT automated

The following are deliberately excluded from `release.yml`. Future-you / new contributors should not re-implement them without revisiting why they're absent:

- **Tag protection on `v*`** — already configured as a tag ruleset (Settings → Rules → Rulesets → tag ruleset with Restrict creations/updates/deletions; admin bypass available). GitHub Team plan only.
- **Branch protection on `main`** — already configured (PR-required, status checks for the test matrix on Ubuntu/macOS/Windows, conversation resolution, no force push, no deletion).
- **PyPI publishing** — deferred until the repo goes public. Switcher is currently distributed via `pipx install git+...` with the install commands documented in the README.
- **Provenance / signing (SLSA, sigstore)** — deferred until the repo goes public; folded into the v0.1.5 security review.
- **`CHANGELOG.md`** — deliberately not maintained at 0.1.x velocity. Release notes are auto-generated from PR/commit history via `gh release create --generate-notes`. Re-evaluate at the going-public boundary.

## Workflow internals (when the pipeline misbehaves)

`release.yml` runs twelve steps in order. If a release fails, the failed step name in the workflow log tells you what went wrong:

| Step | Failure mode | What to check |
|---|---|---|
| Checkout at tag | Tag doesn't exist | The tag wasn't pushed to origin (`git push origin <tag>` from your local clone) |
| Validate tag shape | Tag matches neither stable nor prerelease pattern | Typo in tag name; check capitalization, suffix shape, version segment count |
| Verify tag is on main | Tag points at a commit not on `main` | Stray branch HEAD or typo'd SHA when tagging; retag pointing at a `main` commit |
| Setup pixi | Pixi setup composite action broke | Check `.github/actions/setup-pixi-pinned/action.yml` for changes; check pixi version pin |
| Re-run validation | A test or lint check failed on the tagged commit | Drift between merged commit and tagged commit — investigate what changed |
| Compute expected version from tag | Shouldn't fail (deterministic sed against an already-shape-validated tag) | If it does, an upstream regex change let a malformed tag through, or the `sed` substitutions aren't covering a new prerelease shape |
| Build artifacts | `pixi run build` failed | Most likely a hatch-vcs config issue or fetch-depth problem; check the build logs |
| Verify dist contents | `dist/` doesn't contain exactly one wheel and one sdist | `python -m build` regression or hatchling/hatch-vcs config drift dropped one of the two artifact shapes |
| Verify artifact version matches tag | Wheel or sdist filename version doesn't match the expected version derived from `GITHUB_REF_NAME` | hatch-vcs picked up the wrong tag (most likely cause: same-commit dual-tag ambiguity in a re-run, or the `fallback-version` activated because tag history was unreachable). Inspect the build logs for what version hatch-vcs resolved |
| Wheel install smoke test | Wheel installs but `switcher version` mismatches filename, or `switcher tools` fails | Packaging issue: missing builtin TOMLs, wrong entry point, broken dep |
| Determine prerelease flag | Shouldn't fail (deterministic regex) | If it does, the regex itself has a typo |
| Create or update GitHub Release | `gh release create` / `gh release edit` returned an error | Check `permissions: contents: write` is still on the workflow; check `GH_TOKEN` env on the step |
