# cutoff — SPEC

> Does the model know your library, or the version from 2024?

For library maintainers. cutoff asks each model to write small programs against your library, **runs them against your real current version**, and shows which models reach for deprecated or removed APIs. Then it drafts the smallest `AGENTS.md` / `llms.txt` addition that fixes it, and **re-runs the test to prove the fix works.**

Every popular library now gets bug reports that start with "I asked Claude and…". This turns that into a test suite.

## Who it's for
Maintainers of libraries that changed their API since the models' training cutoffs, and the users who hit those errors.

## Must have (v1)
1. **Probe source, `cutoff.toml` in your repo:** a list of probes. Each probe is a task prompt ("create a client with a 5s timeout and retry on 503") plus a check: a small script or pytest snippet run against the generated code. `cutoff init` drafts probes from your changelog's "Deprecated" / "Removed" / "Changed" sections for you to edit (drafted, never trusted blindly).
2. **Isolated runner:** each generated program runs in a temp directory against your library, either imported from your repo (`python_path`) or installed into a fresh venv (`install = "pip install …"`). Network is blocked inside the program, `DeprecationWarning` is an error, and every run has a timeout.
3. **Failure classification:**
   - `pass`
   - `removed-api`: AttributeError/ImportError on a symbol listed in your changelog as removed
   - `deprecated-api`: a DeprecationWarning from your library, captured with warnings set to error
   - `wrong`: the check failed
   - `crash`: anything else
4. **Models:** the `claude` CLI (text-only, no tools) and any `name=cmd:<shell>` adapter, as in homefield. k samples per probe, reusing `lucky`'s intervals.
5. **Report:** a probes × models grid, with each model's "stale rate" and a 95% interval. The worst offenders show the actual stale line they wrote.
6. **Fix loop, `cutoff fix`:**
   - drafts a minimal context snippet (e.g. "`Client(timeout=…)` replaced `Client(timeout_s=…)` in 3.0") from the failing probes and the changelog
   - re-runs the failing probes with the snippet provided as context
   - reports before → after per model
   - writes the snippet to `cutoff-context.md` for the maintainer to paste into `llms.txt` or docs; never edits docs automatically
7. **CI mode:** `cutoff --ci` fails if the stale rate goes up versus the committed baseline (`cutoff-baseline.json`), so a new release notices when models are about to get it wrong.
8. **Stdlib only** (Python 3.11+, for `tomllib`; venvs use `python -m venv`).

## Won't do (v1)
- Languages other than Python. JS/TS needs its own isolation (warnings, module loading, network), so it's v2 rather than half-done in v1.
- Hosting a public "which model knows which library" leaderboard (tempting for v2).
- Editing the maintainer's docs automatically.

## Expectations → test cases
The fixture is a tiny local library, `fixturelib`, with v1 and v2. v2 renames `connect(timeout_s)` to `connect(timeout)`, removes `old_helper()`, and deprecates `legacy_mode=True`. Fake models are scripts that return canned programs.

| ID | Given | When | Then |
|---|---|---|---|
| E1 | A fake model that writes `connect(timeout_s=5)` | Probe against v2 | `removed-api` (or `wrong`), with the offending line captured |
| E2 | A fake model that uses `legacy_mode=True` | Probe | `deprecated-api` (the DeprecationWarning is turned into an error and caught) |
| E3 | A fake model that writes correct v2 code | Probe | `pass` |
| E4 | A generated program that tries network access | Run | Blocked by default; classified `crash` with a clear reason |
| E5 | A generated program that loops forever | Run | Killed at the timeout; process group gone |
| E6 | A changelog with Deprecated/Removed sections | `cutoff init` | Drafts one probe per removed or renamed symbol, marked `# drafted, review me` |
| E7 | A fake model that goes stale without context but is correct when given `cutoff-context.md` | `cutoff fix` | Shows before `removed-api` → after `pass`, and writes the snippet file |
| E8 | k=10 samples, 4 stale | Report | Stale rate 40%, with a Wilson interval matching `lucky` |
| E9 | Committed baseline at 10% stale; a new run at 30% | `--ci` | Exit 1, naming the regressed probes |
| E10 | Any run | After | No venvs or temp dirs left; the user's environment untouched |
| E11 | The same seeds and fake models | Run twice | Identical reports |

## Launch number
Pick 3 popular libraries with recent breaking changes (ask their maintainers first, or run it on public changelogs yourself). Publish each model's stale rate per library, plus one fix-loop before/after. Offer the maintainers the generated `cutoff-context.md` as a PR. Merged PRs are the launch story.

## Done when
E1–E11 pass. The README opens with a real stale line a real model wrote for a real library, and the one-paragraph fix that stopped it.
