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

```console
$ cutoff run --model claude:sonnet --model claude:haiku -k 10
Stale-API rate (removed or deprecated calls), by model:
  claude:haiku            40%  (4/10, 95% CI 17–69%)
  claude:sonnet           10%  (1/10, 95% CI 2–40%)

What stale code looks like:
  [claude:haiku] connect-timeout
      result = mylib.connect("db", timeout_s=5)
      → TypeError: connect() got an unexpected keyword argument 'timeout_s'
```
<sub>*Illustrative output, from the test fixture's shape. Run it on your library for real numbers.*</sub>

## The fix loop

```console
$ cutoff fix --model claude:haiku -k 10
Context snippet drafted from your changelog:

  # mylib: API changes that AI assistants get wrong
  - `connect(timeout_s=…)` was renamed to `connect(timeout=…)`.

Before → after, on the probes that went stale:
  connect-timeout      claude:haiku       stale 4/10 → 0/10

Wrote cutoff-context.md. Paste it into your llms.txt or docs; cutoff never edits them for you.
```

The snippet isn't written by an LLM. It's the exact lines of **your own changelog** that mention the APIs models actually tripped over, and nothing more. cutoff then reruns only the failing probes with that context and shows you before → after. If the snippet doesn't help, you'll see that too.

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

- **Python libraries only** for now. JS/TS needs its own isolation, so it gets its own version rather than a half-working one here.
- The network block covers Python sockets, not subprocesses a program might spawn. It's a guard against accidents, not a security sandbox. Don't point cutoff at models you don't trust with code execution on your machine.
- A `removed-api` verdict needs the symbol in your `removed` list; unlisted breakages show up as `crash` or `wrong`.
- No real-library results are published here yet. The first ones will be run with maintainers' permission and linked from this README.

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
