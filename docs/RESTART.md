# Restart prompt — feat/claude-json-isolation (PR #13)

Paste the block at the bottom of this file as the next session's first
message. Everything above the `---` separator is context for the human;
everything below is what the AI needs to pick up cleanly.

---

## Where we are

- **Branch:** `feat/claude-json-isolation`
- **Worktree:** `/Users/scottpeterson/xdev/switcher/.worktrees/feat-claude-json-isolation/`
- **PR:** https://github.com/smpnet/switcher/pull/13 (v0.1.6 — ConfigFile / `~/.claude.json` profile isolation)
- **Last push:** `5481ba9` — "fix: hoist ConfigFile validation ahead of compensation mutations"
- **State:** CI re-running on `5481ba9`; awaiting next CR + Hermes review.

## What was just landed (this session's work)

Five review cycles deep (PR-1 → PR-5). Latest cycle's findings, all addressed:

- **CR PR-5 major** (one finding, four locations): `classify_config_file_mapping` was called LATE in
  all four compensation handlers (`_compensate_init_continue`, `_compensate_init_abort`,
  `_compensate_rescan_continue`, `_compensate_rescan_abort`) — AFTER the dir-mapping mutation pass.
  An AMBIGUOUS snapshot would raise only after `move_or_seed_dir` / `swap_link` had already
  mutated live, leaving recovery half-applied. Hoisted the preflight ABOVE the dir-mapping
  mutation in each handler; states are cached in `cf_states` and consumed by the post-mutation
  dispatch loop. Single classifier call per entry per handler.

Earlier-in-session highlights (look at recent commits for full list):
- `_seed_config_files` validates source snapshot against `owned_json_paths` shape, not just "dict".
- `_seed_config_files` uses `atomic_write_file` (writes validated bytes) instead of `shutil.copy2` (TOCTOU race).
- `_plan_config_file_applies` rejects link-shaped snapshot paths (broken-symlink → silent half-switch was the
  Hermes-flagged class).
- `use()` capture-loop is four-way: links match source → capture into source; match destination → capture
  into destination (stale-active drift); all dangling → skip (failed-rename recovery); else refuse.
- `_all_live_dirs_dangling` requires EVERY live dir to dangle (mixed dangling + resolves-elsewhere ≠ skip).
- `use()` preflight rejects link-shaped profile subdirs.
- Orphan `unmanage --force` extends the safety check to ConfigFile snapshot subdirs; for truly-unknown
  orphans (no registry, no historical anchor) the fallback enumerates actual subdirs minus any claimed
  by other registered/historical tools.
- `journal_id` preserved across `FileProfileStore.get`/`rename`/`update_profile_tools` rebuilds.
- `canonicalize_path_for_uniqueness` in `models.py` folds `~`, `$HOME`, `${HOME}`, `%USERPROFILE%`,
  `~\` → sentinel; both cross-tool and intra-tool validators route through it.
- `_POSIX_HOME_RE` uses `r"\$(?:HOME(?!\w)|\{HOME\})"` (no overmatch into `$HOME_BACKUP`).
- `owned_json_paths` validated at journal-parse time via custom Pydantic `AfterValidator`.
- `classify_config_file_mapping` rejects non-object JSON AND structurally-mismatched snapshots
  (e.g. `{"projects": []}` with `.projects[].mcpServers`).
- `_check_init_already_completed` / `_check_rescan_already_completed` short-circuits gate on
  `config_file_mappings` classification too.

## What might come next

- **Next CR/Hermes round** — they're on a tight feedback loop. Expect 0–3 findings. Address with
  the same shape: real bug → fix + regression test; style nit on stable earlier-batch code → defer
  with rationale in the commit message.
- **Eventually merge** — when reviews go LGTM, finish via `superpowers:finishing-a-development-branch`
  (worktree is already on the named branch, so present standard 4-option menu).

## Process notes / hard-won learnings

- **Single push per review cycle.** Batch all fixes locally, run `pixi run check` end-to-end, then push
  once. Successive rapid pushes cause "review flapping" — CR throttles, queues stale reviews.
- **Coverage gate is now load-bearing** (main commit `9c6577e` / PR #14): 85% total + 90% patch
  (`*cli.py` excluded). Already merged into this branch. Local check before push:
  ```
  pixi run -- pytest --cov=switcher --cov-branch --cov-report=xml -q
  pixi run -- diff-cover coverage.xml --compare-branch=origin/main --exclude '*cli.py' --fail-under=90
  ```
  Current state: **90% total, 91% patch** — comfortable margin.
- **`pixi install --locked` must pass after any `pyproject.toml` change.** Regenerate lock:
  `pixi install` (no `--locked`), then commit `pixi.lock`. Caught us twice this session.
- **CR's broad refactor suggestions need a sanity check.** They suggested "fail-closed: enumerate
  ALL profile subdirs" for the orphan-force fallback; that over-attributed siblings' subdirs to the
  orphan. Correct fix: enumerate actual subdirs, EXCLUDING any claimed by other registered/historical
  tools. Always trace through a multi-tool scenario before applying a broad-stroke fix.
- **Hermes posts parallel review threads.** Two near-simultaneous Hermes comments can disagree; each
  finding stands on its own merit, later does not supersede earlier.
- **Push back on shape-mismatch suggestions that conflict with semantics.** E.g., CR wanted the same
  strict assertion shape for `profB` switch test as for vanilla test, but profB's snapshot carries
  explicit `mcpServers: {}` while vanilla's doesn't — `delete-on-absence` semantics make them
  legitimately different. Reverted with a comment.
- **All link-rejection paths share one discipline.** If a reserved-state path can be a symlink/junction
  but the codepath assumes a real dir/file, reject it loudly. Already applied at:
  classifier, `_plan_config_file_applies` snap_path, `_seed_config_files` src, `use()` preflight
  profile-subdir, `--into` collision check. Future findings of this shape should land at the same
  layer of discipline.
- **Compensation handlers MUST be validate-then-mutate.** Preflight all states (dir mappings + ConfigFile
  mappings) BEFORE any FS mutation; cache the states; consume in mutation passes. The PR-5 hoist was
  the final piece of this discipline for ConfigFile.
- **Tests with `symlink_to()` need `@pytest.mark.skipif(IS_WINDOWS, ...)`.** Convention from
  `test_oplog_config_file_classifier.py`. Directory symlinks via `target_is_directory=True` also
  need this — Windows symlinks (file OR dir) need elevation.
- **Test fixtures use `tmp_path`, not `Path("/nonexistent")`.** Convention from
  `tests/unit/**` path-instructions in `.coderabbit.yaml`.
- **`reportUnknownArgumentType = "none"`** is in `pyproject.toml`'s `[tool.basedpyright]` — same
  pattern as the other three Unknown-* reports already disabled. JSON-walker code legitimately
  passes `dict[str, Any]` through helpers; the rule is too noisy.
- **`docs/superpowers/` is gitignored.** Plans/specs live there for the human; don't expect them in
  the worktree after a fresh checkout (path: `/Users/scottpeterson/xdev/switcher/docs/superpowers/plans/2026-05-18-claude-json-isolation.md`).

## Files of interest

- **Spec:** `docs/superpowers/specs/2026-05-17-claude-json-isolation-design.md` (in main checkout)
- **Plan:** `docs/superpowers/plans/2026-05-18-claude-json-isolation.md` (in main checkout)
- **This file:** `docs/RESTART.md` (in the worktree — committed)

## Quick recovery steps for the next session

1. `cd /Users/scottpeterson/xdev/switcher/.worktrees/feat-claude-json-isolation/`
2. Verify state: `git log --oneline -3` should show `5481ba9` at HEAD.
3. Check PR review state: `gh pr view 13 --json reviews | jq -r '.reviews | sort_by(.submittedAt) | last'`
4. Check Hermes: `gh pr view 13 --json comments | jq -r '.comments[] | select(.body | test("hermes")) | "\(.createdAt) — \(.body | .[0:200])"' | tail -3`
5. Check CI: `gh pr checks 13`
6. Address findings (if any). Always finish with:
   ```
   pixi run check  # must exit 0
   pixi run -- pytest --cov=switcher --cov-branch --cov-report=xml -q
   pixi run -- diff-cover coverage.xml --compare-branch=origin/main --exclude '*cli.py' --fail-under=90
   ```
7. If `pyproject.toml` changed: `pixi install` (regen lock), commit `pixi.lock`.
8. ONE push per cycle. Wait for the next round of reviews.

---

## Restart prompt — paste this as the first message of the next session

```
Continuing PR #13 (v0.1.6 ConfigFile / ~/.claude.json profile isolation) on
feat/claude-json-isolation.

Last push 5481ba9 — "fix: hoist ConfigFile validation ahead of compensation
mutations" (CR PR-5 major). Five review cycles in. Hermes has been LGTM-ish
on the last round; CR's last finding was the compensation-handler preflight
hoist which I just addressed.

State:
- Worktree at .worktrees/feat-claude-json-isolation/ (already on the branch).
- Local `pixi run check` green end-to-end.
- Coverage: 90% total, 91% patch vs origin/main — clears the 85/90 gate added
  in main commit 9c6577e (merged in 170ab91).
- pixi.lock in sync with workspace; `pixi install --locked` is happy.

Process:
1. Load skills: superpowers:using-superpowers, superpowers:using-git-worktrees
   (already in the worktree — Step 0 of the worktree skill will detect it,
   GIT_DIR != GIT_COMMON, branch feat/claude-json-isolation).
2. Check the PR for new reviews/comments:
   - `gh pr view 13 --json reviews | jq -r '.reviews | sort_by(.submittedAt) | last'`
   - `gh pr view 13 --json comments | jq -r '.comments[] | select(.body | test("hermes")) | "\(.createdAt) — \(.body | .[0:200])"' | tail -3`
   - `gh pr checks 13`
3. If CR/Hermes have new findings, triage and address all in one batch.
   - Real correctness bugs: fix + regression test.
   - Style nits on stable earlier-batch code: defer with rationale.
4. Before push, ALWAYS:
   - `pixi run check` (must be exit 0)
   - `pixi run -- pytest --cov=switcher --cov-branch --cov-report=xml -q`
   - `pixi run -- diff-cover coverage.xml --compare-branch=origin/main --exclude '*cli.py' --fail-under=90`
   - If pyproject.toml changed: `pixi install` (regen lock), commit pixi.lock.
5. ONE push per cycle. Then stop and wait for the next review round.
6. If both reviewers go LGTM, use superpowers:finishing-a-development-branch
   to wrap up.

Read docs/RESTART.md in the worktree for the full hand-off — process notes,
hard-won learnings, files of interest, and the full per-cycle changelog.
The Restart.md was committed as part of this hand-off; pick up from where it
ends.

Start by reading docs/RESTART.md, then check PR state.
```
