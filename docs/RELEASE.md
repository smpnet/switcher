# Releasing switcher

Operational doc for cutting a release. Recipe-first; rationale below.

## Release notes

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
- `switcher status` on an empty active map prints `"No tools currently managed. Run 'switcher rescan' to discover installed tools."` instead of the bare `"no active profiles"`.

**Fixed**

- Legacy state migration (v0.1.0–v0.1.2 → v0.1.3+) now uses filesystem inspection over the current registry, with a per-tool historical-subdir map and a hardcoded legacy-parent scan. Fixes the orphan-symlink data-loss path from v0.1.3 PR #4 review.
- `rescan --dry-run` output grammar: "would capture X" (was: "would captured X").

**Internal**

- `UninstallMappingState` enum renamed from `_UninstallMappingState`. CLI switches from `.value` string compares to enum compares.
- `.coderabbit.yaml` `path_instructions` added for `tests/unit/**` and `tests/integration/**`.
- New sentinel test (`tests/unit/test_migration_metadata_sentinel.py`) fails CI if a builtin TOML adds a `profile_subdir` or live-path basename not represented in `_HISTORICAL_PROFILE_SUBDIRS` / `_HISTORICAL_LIVE_PATH_BASENAMES`. Future builtin path rewrites must update both.

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
