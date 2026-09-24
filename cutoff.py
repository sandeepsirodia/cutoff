"""cutoff: does the model know your library, or the version from 2024?

For library maintainers. Asks models to write small programs against your library, runs them against
your *real current version* (network off, warnings as errors), and reports which models reach for
removed or deprecated APIs. `cutoff fix` drafts a context snippet from your changelog and re-runs the
failing probes to prove it helps. Python libraries; standard library only (Python 3.11+ for tomllib).
"""
import argparse
import ast
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
from statistics import NormalDist

__version__ = "0.1.0"
CONFIG = "cutoff.toml"
BASELINE = "cutoff-baseline.json"
CONTEXT = "cutoff-context.md"
STALE = ("removed-api", "deprecated-api")
# A model that never answered says nothing about what it knows. Such samples are excluded from every rate.
UNSCORED = ("model-error", "no-code")
RATE_LIMIT_RE = re.compile(r"session limit|usage limit|rate.?limit|too many requests|\b429\b|overloaded|"
                           r"quota|credit balance|resets? (at )?\d", re.I)


class ModelError(RuntimeError):
    """The model call itself failed (limit, outage, CLI missing)."""


class ModelUnavailable(ModelError):
    """A limit or outage: stop the whole run instead of recording garbage."""

PROMPT = """Write a complete, self-contained Python module named solution.py that uses the `{lib}` library \
(assume the latest released version is installed) to do the following:

{task}

Output only the code, in a single ```python block."""

CONTEXT_PROMPT = """Notes from the {lib} maintainers about the current API:

{context}

"""

# Blocks outbound connections inside generated programs (injected via PYTHONPATH as sitecustomize).
NO_NETWORK = '''import socket
def _blocked(*a, **k):
    raise OSError("cutoff: network access is disabled for generated programs")
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
'''


# ------------------------------------------------------------------ config

def load_config(repo):
    with open(os.path.join(repo, CONFIG), "rb") as f:
        cfg = tomllib.load(f)
    lib = cfg.get("library", {})
    if "name" not in lib:
        raise SystemExit("%s needs [library] name = \"…\"" % CONFIG)
    lib.setdefault("python_path", [])
    lib.setdefault("removed", [])
    lib.setdefault("deprecated", [])
    probes = cfg.get("probe", [])
    for p in probes:
        for key in ("id", "task", "check"):
            if key not in p:
                raise SystemExit("every [[probe]] needs id, task and check (missing %r)" % key)
    return lib, probes


# ------------------------------------------------------------------ changelog → probes

SECTION_RE = re.compile(r"^#{2,4}\s+\[?(Removed|Deprecated|Changed|Breaking[^\n]*)\]?\s*$", re.I | re.M)


def changelog_entries(text):
    """Bullets under Removed / Deprecated / Changed / Breaking headings, plus any bullet that says
    removed / deprecated / renamed. Returns [(kind, line, [symbols])]."""
    out, kind = [], None
    for line in text.splitlines():
        h = re.match(r"^#{1,6}\s+(.*)$", line)
        if h:
            m = SECTION_RE.match(line)
            kind = m.group(1).split()[0].lower() if m else None
            continue
        if not re.match(r"\s*[-*]\s+", line):
            continue
        low = line.lower()
        k = kind or ("removed" if "removed" in low else "deprecated" if "deprecat" in low
                     else "changed" if "renamed" in low else None)
        if not k:
            continue
        syms = [re.sub(r"\(.*$|=.*$", "", s).strip() for s in re.findall(r"`([^`]+)`", line)]
        out.append((k, line.strip(), [s for s in syms if s]))
    return out


def draft_config(lib_name, changelog_text):
    entries = changelog_entries(changelog_text)
    removed, deprecated, probes = [], [], []
    for kind, line, syms in entries:
        if not syms:
            continue
        old = syms[0]
        (deprecated if kind == "deprecated" else removed).append(old)
        probes.append((re.sub(r"\W+", "-", old).strip("-").lower() or "probe", line, old))
    toml = ['[library]', 'name = %s' % json.dumps(lib_name),
            '# Where to import the library from, relative to this repo (or use install = "pip install …")',
            'python_path = ["src"]',
            'removed = %s' % json.dumps(sorted(set(removed))),
            'deprecated = %s' % json.dumps(sorted(set(deprecated))), '']
    for pid, line, old in probes:
        toml += ['# drafted, review me: from changelog entry: %s' % line.replace("\n", " "),
                 '[[probe]]', 'id = %s' % json.dumps(pid),
                 'task = %s' % json.dumps("TODO: describe a small task a user would naturally solve with the "
                                          "replacement for `%s` (without naming either)." % old),
                 "check = '''\nimport solution  # TODO: assert on what solution.py produced\n'''", '']
    return "\n".join(toml), len(probes)


# ------------------------------------------------------------------ running generated code

def extract_code(text):
    """The model's code, or None if it didn't write any (prose, an error message, a refusal)."""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len)
    try:
        ast.parse(text)
    except SyntaxError:
        return None
    return text if text.strip() else None


def run_program(repo, lib, code, check, timeout):
    """Run generated `code` as solution.py, then the probe's `check`. Returns (status, detail, stale_line)."""
    with tempfile.TemporaryDirectory(prefix="cutoff-") as d:
        with open(os.path.join(d, "solution.py"), "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(d, "check.py"), "w", encoding="utf-8") as f:
            f.write(check)
        site = os.path.join(d, "_cutoff_site")
        os.makedirs(site)
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write(NO_NETWORK)
        paths = [site, d] + [os.path.join(repo, p) for p in lib["python_path"]]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths), PYTHONDONTWRITEBYTECODE="1")
        python = lib.get("_python", sys.executable)
        # DeprecationWarning is for developers; libraries such as pandas use FutureWarning for end users.
        p = subprocess.Popen([python, "-W", "error::DeprecationWarning", "-W", "error::FutureWarning",
                              "-W", "error::PendingDeprecationWarning", "check.py"], cwd=d, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return "crash", "timeout after %ss" % timeout, None
        if p.returncode == 0:
            return "pass", "", None
        return classify(err, lib, code)


def classify(stderr, lib, code):
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    frames = re.findall(r'File ".*?solution\.py", line (\d+)', stderr)
    src = code.splitlines()
    stale_line = src[int(frames[-1]) - 1].strip() if frames and int(frames[-1]) <= len(src) else None
    if re.search(r"(Pending)?DeprecationWarning|FutureWarning", last):
        return "deprecated-api", last, stale_line
    if re.match(r"(AttributeError|ImportError|ModuleNotFoundError|TypeError|NameError)", last):
        for sym in lib["removed"] + lib["deprecated"]:
            if re.search(r"\b%s\b" % re.escape(sym.split(".")[-1]), last):
                return "removed-api", last, stale_line
    if "cutoff: network access is disabled" in stderr:
        return "crash", "tried to use the network", stale_line
    if last.startswith("AssertionError"):
        return "wrong", last, stale_line
    return "crash", last, stale_line


# ------------------------------------------------------------------ models

class Model:
    def __init__(self, spec):
        name, _, rest = spec.partition("=")
        if rest.startswith("cmd:"):
            self.name, self.kind, self.arg = name, "cmd", rest[4:]
        elif spec.split(":")[0] == "claude":
            self.name, self.kind, self.arg = spec, "claude", spec.partition(":")[2] or None
        else:
            raise ValueError("unknown model %r (use claude, claude:<model>, or name=cmd:<shell>)" % spec)

    def ask(self, prompt, timeout, sample):
        if self.kind == "claude":
            # Clean room: no user hooks, CLAUDE.md or output styles, so results measure the model, not your setup.
            cmd = ["claude", "-p", prompt, "--tools", "", "--output-format", "json", "--no-session-persistence",
                   *(["--bare"] if os.environ.get("ANTHROPIC_API_KEY") else ["--setting-sources", ""])]
            cmd += ["--model", self.arg] if self.arg else []
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except OSError as e:
                raise ModelUnavailable("could not start claude: %s" % e) from e
            try:
                data = json.loads(r.stdout)
            except ValueError as e:
                raise ModelError("claude returned no JSON (exit %s): %s" % (r.returncode, (r.stderr or r.stdout)[:200])) from e
            if data.get("is_error") or r.returncode != 0:
                msg = str(data.get("result") or r.stderr or "claude failed")[:300]
                raise (ModelUnavailable if RATE_LIMIT_RE.search(msg) else ModelError)("%s: %s" % (self.name, msg))
            return data.get("result", "")
        env = dict(os.environ, CUTOFF_PROMPT=prompt, CUTOFF_SAMPLE=str(sample))
        with tempfile.TemporaryDirectory(prefix="cutoff-model-") as d:
            p = subprocess.Popen(["sh", "-c", self.arg], cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
            try:
                out, err = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)   # the whole group: model scripts spawn children
                p.communicate()
                raise ModelError("%s: timed out after %ss" % (self.name, timeout)) from None
        if p.returncode == 127:
            raise ModelUnavailable("%s: command not found: %s" % (self.name, err.strip()[:200]))
        if p.returncode != 0:
            msg = (err.strip() or out.strip())[:300] or "exit %s" % p.returncode
            raise (ModelUnavailable if RATE_LIMIT_RE.search(err + out) else ModelError)("%s: %s" % (self.name, msg))
        return out


# ------------------------------------------------------------------ stats (vendored from lucky)

def wilson(k, n, conf=0.95):
    if n == 0:
        return 0.0, 1.0
    z = NormalDist().inv_cdf(1 - (1 - conf) / 2)
    p = k / n
    denom, centre = 1 + z * z / n, p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (0.0 if k == 0 else max(0.0, (centre - half) / denom)), (1.0 if k == n else min(1.0, (centre + half) / denom))


# ------------------------------------------------------------------ core

def probe_all(repo, lib, probes, models, samples, timeout, context=None, only=None):
    """Returns [{probe, model, sample, status, detail, stale_line}]."""
    results = []
    for p in probes:
        for m in models:
            if only is not None and (p["id"], m.name) not in only:
                continue
            for k in range(samples):
                prompt = (CONTEXT_PROMPT.format(lib=lib["name"], context=context) if context else "") + \
                         PROMPT.format(lib=lib["name"], task=p["task"])
                try:
                    code = extract_code(m.ask(prompt, timeout, k))
                    if code is None:
                        status, detail, line = "no-code", "the model returned no Python code", None
                    else:
                        status, detail, line = run_program(repo, lib, code, p["check"], timeout)
                except ModelUnavailable:
                    raise
                except (ModelError, subprocess.TimeoutExpired) as e:
                    status, detail, line = "model-error", str(e)[:200], None
                results.append({"probe": p["id"], "model": m.name, "sample": k, "status": status,
                                "detail": detail, "stale_line": line})
    return results


def stale_rates(results):
    out = {}
    for m in sorted({r["model"] for r in results}):
        rs = [r for r in results if r["model"] == m and r["status"] not in UNSCORED]
        skipped = sum(r["model"] == m and r["status"] in UNSCORED for r in results)
        k = sum(r["status"] in STALE for r in rs)
        out[m] = {"stale": k, "n": len(rs), "skipped": skipped, "rate": k / len(rs) if rs else None,
                  "ci": wilson(k, len(rs))}
    return out


def per_probe(results):
    out = {}
    for r in results:
        if r["status"] in UNSCORED:
            continue
        d = out.setdefault(r["probe"], {"stale": 0, "n": 0})
        d["n"] += 1
        d["stale"] += r["status"] in STALE
    return {k: dict(v, rate=v["stale"] / v["n"]) for k, v in out.items()}


def draft_context(lib, results, changelog_text):
    """Deterministic: the changelog lines that mention the symbols models actually tripped over."""
    hit = set()
    for r in results:
        if r["status"] in STALE:
            for sym in lib["removed"] + lib["deprecated"]:
                if re.search(r"\b%s\b" % re.escape(sym.split(".")[-1]), (r["detail"] or "") + " " + (r["stale_line"] or "")):
                    hit.add(sym)
    # match on the whole changelog line: `connect(timeout_s=…)` mentions timeout_s even though its symbol is connect
    lines = [line for _, line, _ in changelog_entries(changelog_text)
             if any(re.search(r"\b%s\b" % re.escape(h.split(".")[-1]), line) for h in hit)]
    if not lines:
        return None
    return "# %s: API changes that AI assistants get wrong\n\n%s\n" % (lib["name"], "\n".join(
        l if l.startswith(("-", "*")) else "- " + l for l in lines))


def read_changelog(repo):
    for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "NEWS.md", "CHANGELOG.rst"):
        p = os.path.join(repo, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return ""


# ------------------------------------------------------------------ output

def report(results, out):
    rates = stale_rates(results)
    out.write("Stale-API rate (removed or deprecated calls), by model:\n")
    for m, r in sorted(rates.items(), key=lambda kv: -(kv[1]["rate"] if kv[1]["rate"] is not None else -1)):
        if r["rate"] is None:
            out.write("  %-22s no scored samples (%d call(s) errored or returned no code)\n" % (m, r["skipped"]))
            continue
        out.write("  %-22s %3.0f%%  (%d/%d, 95%% CI %.0f–%.0f%%)%s\n" % (
            m, 100 * r["rate"], r["stale"], r["n"], 100 * r["ci"][0], 100 * r["ci"][1],
            "  ⚠ %d more sample(s) errored or returned no code and are excluded" % r["skipped"] if r["skipped"] else ""))
    probes = sorted({r["probe"] for r in results})
    models = sorted(rates)
    out.write("\n| Probe | " + " | ".join(models) + " |\n|---|" + "---|" * len(models) + "\n")
    for p in probes:
        cells = []
        for m in models:
            rs = [r for r in results if r["probe"] == p and r["model"] == m]
            scored = [r for r in rs if r["status"] not in UNSCORED]
            cells.append("%d/%d stale" % (sum(r["status"] in STALE for r in scored), len(scored)) if scored else "—")
        out.write("| %s | %s |\n" % (p, " | ".join(cells)))
    worst = [r for r in results if r["status"] in STALE and r["stale_line"]]
    if worst:
        out.write("\nWhat stale code looks like:\n")
        seen = set()
        for r in worst:
            if (r["model"], r["stale_line"]) in seen:
                continue
            seen.add((r["model"], r["stale_line"]))
            out.write("  [%s] %s\n      %s\n      → %s\n" % (r["model"], r["probe"], r["stale_line"], r["detail"]))
            if len(seen) >= 5:
                break


# ------------------------------------------------------------------ install into a venv

class Environment:
    """If the config has `install = "pip install mylib==3.0"`, run it in a fresh venv (with the venv's bin
    first on PATH, so `pip` is the venv's) and run generated programs with that venv's python."""

    def __init__(self, repo, lib):
        self.repo, self.lib, self.dir = repo, lib, None

    def __enter__(self):
        if self.lib.get("install"):
            self.dir = tempfile.TemporaryDirectory(prefix="cutoff-venv-")
            subprocess.run([sys.executable, "-m", "venv", self.dir.name], check=True, capture_output=True)
            bindir = os.path.join(self.dir.name, "Scripts" if os.name == "nt" else "bin")
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), VIRTUAL_ENV=self.dir.name)
            r = subprocess.run(self.lib["install"], shell=True, cwd=self.repo, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                self.dir.cleanup()
                raise SystemExit("install failed: %s\n%s" % (self.lib["install"], r.stderr[-2000:]))
            self.lib["_python"] = os.path.join(bindir, "python")
        return self.lib

    def __exit__(self, *exc):
        if self.dir:
            self.dir.cleanup()
        self.lib.pop("_python", None)


# ------------------------------------------------------------------ CLI

def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="cutoff", description="Does the model know your library, or the version from 2024?")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="draft cutoff.toml probes from your CHANGELOG")
    i.add_argument("--name", help="library import name (default: repo folder name)")
    for name, help_ in (("run", "probe the models"), ("fix", "draft a context snippet and prove it helps")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--model", action="append", required=True, help="claude | claude:<model> | name=cmd:<shell>")
        s.add_argument("-k", "--samples", type=int, default=5, help="samples per probe and model (default 5)")
        s.add_argument("--timeout", type=int, default=120)
        s.add_argument("--json", action="store_true")
        if name == "run":
            s.add_argument("--ci", action="store_true", help=f"exit 1 if stale rates rose vs {BASELINE}")
            s.add_argument("--update-baseline", action="store_true", help=f"write {BASELINE}")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)

    if a.cmd == "init":
        target = os.path.join(repo, CONFIG)
        if os.path.exists(target):
            raise SystemExit("%s already exists; not overwriting" % CONFIG)
        text, n = draft_config(a.name or os.path.basename(repo), read_changelog(repo))
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        out.write("Drafted %d probe(s) into %s from your changelog. Review every one: fill in task and check.\n" % (n, CONFIG))
        return 0

    lib, probes = load_config(repo)
    models = [Model(m) for m in a.model]
    with Environment(repo, lib):
        return _run_or_fix(a, repo, lib, probes, models, out)


def _run_or_fix(a, repo, lib, probes, models, out):
    try:
        return _run_or_fix_inner(a, repo, lib, probes, models, out)
    except ModelUnavailable as e:
        out.write("\nStopped: %s\nNo results were recorded. A limit or outage says nothing about what the model knows; "
                  "wait, fix the cause, and run again.\n" % e)
        return 3


def _run_or_fix_inner(a, repo, lib, probes, models, out):
    results = probe_all(repo, lib, probes, models, a.samples, a.timeout)

    if a.cmd == "run":
        if a.json:
            out.write(json.dumps({"stale": stale_rates(results), "results": results}, indent=2) + "\n")
        else:
            report(results, out)
        current = {"per_probe": per_probe(results), "models": {m: r["rate"] for m, r in stale_rates(results).items()}}
        code = 0
        if a.ci:
            try:
                with open(os.path.join(repo, BASELINE), encoding="utf-8") as f:
                    base = json.load(f)
            except FileNotFoundError:
                raise SystemExit("no %s yet: run with --update-baseline first" % BASELINE)
            regressed = [p for p, v in current["per_probe"].items()
                         if v["rate"] > base["per_probe"].get(p, {}).get("rate", 0.0)]
            if regressed:
                out.write("\nStale rate went up vs baseline on: %s\n" % ", ".join(sorted(regressed)))
                code = 1
            else:
                out.write("\nNo probe got more stale than the baseline.\n")
        if a.update_baseline:
            with open(os.path.join(repo, BASELINE), "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            out.write("Wrote %s\n" % BASELINE)
        return code

    # fix: context from the changelog, then re-run only the failing (probe, model) pairs
    context = draft_context(lib, results, read_changelog(repo))
    if not context:
        out.write("Nothing to fix: no stale calls matched your changelog.\n" if any(r["status"] in STALE for r in results)
                  else "Nothing to fix: no model wrote stale code.\n")
        return 0
    failing = {(r["probe"], r["model"]) for r in results if r["status"] in STALE}
    after = probe_all(repo, lib, probes, models, a.samples, a.timeout, context=context, only=failing)
    out.write("Context snippet drafted from your changelog:\n\n%s\n" % context)
    out.write("Before → after, on the probes that went stale:\n")
    for pid, mname in sorted(failing):
        b = [r for r in results if (r["probe"], r["model"]) == (pid, mname)]
        f_ = [r for r in after if (r["probe"], r["model"]) == (pid, mname)]
        out.write("  %-20s %-18s stale %d/%d → %d/%d\n" % (pid, mname, sum(r["status"] in STALE for r in b), len(b),
                                                           sum(r["status"] in STALE for r in f_), len(f_)))
    with open(os.path.join(repo, CONTEXT), "w", encoding="utf-8") as f:
        f.write(context)
    out.write("\nWrote %s. Paste it into your llms.txt or docs; cutoff never edits them for you.\n" % CONTEXT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
