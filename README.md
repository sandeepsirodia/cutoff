<h1 align="center">cutoff</h1>

<p align="center">
  <em>Does the model know your library, or the version from 2024?</em>
</p>

<p align="center">
  <a href="https://github.com/sandeepsirodia/cutoff/actions/workflows/ci.yml"><img src="https://github.com/sandeepsirodia/cutoff/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/dependencies-0-111111?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/for-library%20maintainers-111111?style=flat-square" alt="For library maintainers">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT">
</p>

---

You shipped 3.0. You renamed `timeout_s` to `timeout`, wrote the migration guide, and bumped the major version. You did everything right.

Then the issues start:

> **TypeError: connect() got an unexpected keyword argument 'timeout_s'**
> *"I followed the example Claude gave me…"*

The model learned your API from two years of blog posts, Stack Overflow answers and old READMEs. It writes your *old* API, confidently, for every one of your users. That's a new kind of bug report, and until now there was no test for it.

**cutoff is that test.** It asks models to write small programs with your library, **runs them against your real current version**, and shows exactly which models reach for removed or deprecated APIs. Then it drafts the fix and **proves the fix works.**

## A real run: httpx 0.28

httpx 0.28 (November 2024) removed the `proxies=` and `app=` arguments and deprecated `verify=<string>`. I asked three Claude models for small httpx programs, 5 samples per probe, and ran every one against the real httpx 0.28.1:

<p align="center"><img src="https://raw.githubusercontent.com/sandeepsirodia/cutoff/main/assets/httpx.svg" alt="Stale httpx calls: Claude Haiku 10 of 15, Claude Sonnet 0 of 15, Claude Opus 0 of 15" width="760"></p>

```console
$ cutoff run --model claude:haiku --model claude:sonnet --model claude:opus -k 5
Stale-API rate (removed or deprecated calls), by model:
  claude:haiku            67%  (10/15, 95% CI 42–85%)
  claude:opus              0%  (0/15, 95% CI 0–20%)
  claude:sonnet            0%  (0/15, 95% CI 0–20%)

What stale code looks like:
  [claude:haiku] proxy
      client = httpx.Client(proxies="http://localhost:8080")
      → TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
  [claude:haiku] custom-ca
      client = httpx.Client(verify=certifi.where())
      → DeprecationWarning: `verify=<str>` is deprecated. Use `verify=ssl.create_default_context(cafile=...)` …
```

The bigger models write current httpx. The small, fast one writes the 2023 API: every single time for proxies, and every time for custom CA bundles.

## The fix loop

`cutoff fix` picks the lines of **your own changelog** that mention the APIs models got wrong (the newest release that mentions each one, plus any line saying what to use *instead*), reruns only the failing probes with that context, and shows before → after:

<p align="center"><img src="https://raw.githubusercontent.com/sandeepsirodia/cutoff/main/assets/httpx-fix.svg" alt="With the changelog snippet, stale proxies= calls fell from 10 of 10 to 0 of 10, and stale verify= calls from 10 of 10 to 4 of 10" width="760"></p>

```console
$ cutoff fix --model claude:haiku -k 10
Before → after, on the probes that went stale:
  custom-ca            claude:haiku       stale 10/10 → 4/10
  proxy                claude:haiku       stale 10/10 → 0/10
```

No LLM writes the snippet, so it can't invent API advice. For `proxies=`, two sentences from httpx's changelog fixed it completely. For `verify=`, they helped but didn't finish the job, and cutoff shows you that instead of hiding it. The config, all 45 raw samples and the fix output are in [`examples/httpx/`](examples/httpx/).

## Set it up in two minutes

```bash
uv tool install git+https://github.com/sandeepsirodia/cutoff     # Python 3.11+
cutoff init                                                       # drafts cutoff.toml from your CHANGELOG
```

`init` reads your changelog's Removed / Deprecated / Changed entries and drafts one probe per renamed or removed API, each marked `# drafted, review me`. You fill in what each probe should do and how to check it:

```toml
[library]
name = "mylib"
python_path = ["src"]                  # or: install = "pip install mylib==3.0"
removed = ["timeout_s", "old_helper"]
deprecated = ["legacy_mode"]

[[probe]]
id = "connect-timeout"
task = "Connect to host 'db' with a 5 second timeout and store the connection in `result`."
check = """
import solution
assert solution.result.timeout == 5
"""
```

## Keep it green: CI mode

```bash
cutoff run --model claude:haiku -k 10 --update-baseline   # once
cutoff run --model claude:haiku -k 10 --ci                # every release: exit 1 if models got more stale
```

## How it judges code, and why you can trust the verdict

| Verdict | Meaning |
|---|---|
| `removed-api` | Failed with `AttributeError` / `ImportError` / `TypeError` naming an API from your `removed` list |
| `deprecated-api` | Triggered a `DeprecationWarning` (runs with warnings as errors) |
| `wrong` | Ran, but your check's assertion failed |
| `crash` | Anything else, including timeouts and attempted network access |
| `pass` | Your check passed against your real current version |

- **Real execution, not pattern matching.** A stale API is only flagged if running it against your library actually fails.
- **Sandboxed enough.** Each program runs in a temp directory, with network access blocked and a timeout that kills the whole process group.
- **Honest numbers.** Stale rates come with 95% Wilson intervals, because 1/10 and 10/100 are not the same claim.
- **Any model.** `claude` / `claude:<model>` built in, or `name=cmd:<any shell command>` for anything else (the prompt arrives in `$CUTOFF_PROMPT`).

## Honest limits

- **Warnings:** `DeprecationWarning`, `PendingDeprecationWarning` and `FutureWarning` are all errors in the generated program. A library that signals deprecation some other way (a log line, a custom exception) needs that listed in `removed`.
- **Python libraries only** for now. JS/TS needs its own isolation, so it gets its own version rather than a half-working one here.
- The network block covers Python sockets, not subprocesses a program might spawn. It's a guard against accidents, not a security sandbox. Don't point cutoff at models you don't trust with code execution on your machine.
- A `removed-api` verdict needs the symbol in your `removed` list; unlisted breakages show up as `crash` or `wrong`.
- The httpx numbers are one library, three probes and small samples (5 or 10 per cell); read them as a demonstration of the method, not a model ranking.

## Prior art, and what's new here

- **Research:** [*LLMs Meet Library Evolution*](https://arxiv.org/html/2406.09834) (ICSE 2025) measured deprecated-API use by LLMs across popular Python libraries. cutoff turns that kind of study into a tool one maintainer can run in CI.
- **[APIScanner](https://arxiv.org/abs/2102.09251)** flags deprecated API use in your editor. It's static, and aimed at users rather than maintainers.
- **Docs-in-context services** (llms.txt, MCP doc servers) *deliver* current docs to models. cutoff *measures* whether models get your API wrong, and whether a snippet fixes it.

<details>
<summary><b>Development</b></summary>

```bash
python -m unittest discover -s tests -v
```

Tests map 1:1 to [SPEC.md](SPEC.md). They use a tiny fixture library (`fixturelib` 2.0, with a renamed argument, a removed function and a deprecated flag) and fake models that return canned programs, so there are no API calls and the runs are deterministic.

</details>

<p align="center"><sub>MIT © Sandeep Sirodia · Maintain a library models keep getting wrong? Try it, and open an issue with what you find. A ⭐ helps other maintainers find it.</sub></p>
