"""Tests map 1:1 to SPEC.md (E1..E11). The fixture is a tiny local library, `fixturelib` v2, whose
changelog renamed connect(timeout_s) -> connect(timeout), removed old_helper() and deprecated
legacy_mode. Fake "models" are shell commands that return canned programs."""
import glob
import io
import json
import os
import shlex
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cutoff  # noqa: E402

LIB = textwrap.dedent('''\
    import warnings
    __version__ = "2.0.0"

    def connect(host, *, timeout=None, legacy_mode=False):
        if legacy_mode:
            warnings.warn("legacy_mode is deprecated; it will be removed in 3.0", DeprecationWarning, stacklevel=2)
        return {"host": host, "timeout": timeout}

    def helper(x):
        return x * 2
''')
CHANGELOG = textwrap.dedent('''\
    # Changelog

    ## 2.0.0

    ### Removed
    - `old_helper()` was removed; use `helper()` instead.

    ### Changed
    - `connect(timeout_s=…)` was renamed to `connect(timeout=…)`.

    ### Deprecated
    - `legacy_mode=True` is deprecated and will be removed in 3.0.

    ## 1.4.0
    - Faster connections.
''')
CONFIG = textwrap.dedent('''\
    [library]
    name = "fixturelib"
    python_path = ["src"]
    removed = ["old_helper", "timeout_s"]
    deprecated = ["legacy_mode"]

    [[probe]]
    id = "connect-timeout"
    task = "Connect to host 'db' with a 5 second timeout and store the connection in a variable named result."
    check = """
    import solution
    assert solution.result["timeout"] == 5
    """
''')

STALE_KW = 'import fixturelib\nresult = fixturelib.connect("db", timeout_s=5)\n'
STALE_DEPRECATED = 'import fixturelib\nresult = fixturelib.connect("db", timeout=5, legacy_mode=True)\n'
GOOD = 'import fixturelib\nresult = fixturelib.connect("db", timeout=5)\n'
WRONG = 'import fixturelib\nresult = fixturelib.connect("db", timeout=50)\n'


def fake(name, code_when_no_context, code_with_context=None):
    """A model that answers with fixed code, or different code if the prompt contains cutoff's context."""
    code_with_context = code_with_context or code_when_no_context
    script = ("import os; p = os.environ['CUTOFF_PROMPT']; "
              "print('```python\\n' + (%r if 'Notes from the' in p else %r) + '```')" % (code_with_context, code_when_no_context))
    return "%s=cmd:%s -c %s" % (name, shlex.quote(sys.executable), shlex.quote(script))


def make_repo(config=CONFIG):
    repo = tempfile.mkdtemp(prefix="cutoff-fixture-")
    os.makedirs(os.path.join(repo, "src", "fixturelib"))
    with open(os.path.join(repo, "src", "fixturelib", "__init__.py"), "w") as f:
        f.write(LIB)
    with open(os.path.join(repo, "CHANGELOG.md"), "w") as f:
        f.write(CHANGELOG)
    if config:
        with open(os.path.join(repo, "cutoff.toml"), "w") as f:
            f.write(config)
    return repo


def cli(repo, *argv):
    out = io.StringIO()
    code = cutoff.main(["--repo", repo, *argv], out=out)
    return code, out.getvalue()


def results(repo, model, k=2):
    code, out = cli(repo, "run", "--model", model, "-k", str(k), "--json")
    return json.loads(out)["results"]


class TestSpec(unittest.TestCase):
    def test_e1_renamed_argument_is_removed_api(self):
        r = results(make_repo(), fake("old", STALE_KW))[0]
        self.assertEqual(r["status"], "removed-api")
        self.assertIn("timeout_s", r["detail"])
        self.assertEqual(r["stale_line"], 'result = fixturelib.connect("db", timeout_s=5)')

    def test_e2_deprecation_warning_is_caught(self):
        r = results(make_repo(), fake("dep", STALE_DEPRECATED))[0]
        self.assertEqual(r["status"], "deprecated-api")
        self.assertIn("legacy_mode", r["detail"])

    def test_e3_correct_code_passes(self):
        self.assertEqual({r["status"] for r in results(make_repo(), fake("good", GOOD))}, {"pass"})
        self.assertEqual(results(make_repo(), fake("wrong", WRONG))[0]["status"], "wrong")

    def test_e4_network_is_blocked(self):
        net = 'import socket\nsocket.create_connection(("example.com", 80), timeout=2)\nresult = {"timeout": 5}\n'
        r = results(make_repo(), fake("net", net))[0]
        self.assertEqual(r["status"], "crash")
        self.assertIn("network", r["detail"])

    def test_e5_infinite_loop_is_killed(self):
        r = cutoff.run_program(make_repo(), cutoff.load_config(make_repo())[0], "while True: pass\n", "import solution\n", 2)
        self.assertEqual(r[0], "crash")
        self.assertIn("timeout", r[1])

    def test_e6_init_drafts_probes_from_changelog(self):
        repo = make_repo(config=None)
        code, out = cli(repo, "init", "--name", "fixturelib")
        self.assertIn("Drafted 3 probe(s)", out)
        text = open(os.path.join(repo, "cutoff.toml")).read()
        self.assertEqual(text.count("# drafted, review me"), 3)
        lib, probes = cutoff.load_config(repo)  # the draft is valid TOML
        self.assertIn("old_helper", lib["removed"])
        self.assertIn("legacy_mode", lib["deprecated"])
        with self.assertRaises(SystemExit):
            cli(repo, "init")  # never overwrites

    def test_e7_fix_loop_proves_the_context_helps(self):
        repo = make_repo()
        code, out = cli(repo, "fix", "--model", fake("forgetful", STALE_KW, GOOD), "-k", "2")
        self.assertIn("connect(timeout_s=…)", out)
        self.assertRegex(out, r"connect-timeout\s+forgetful\s+stale 2/2 → 0/2")
        ctx = open(os.path.join(repo, "cutoff-context.md")).read()
        self.assertIn("renamed to `connect(timeout=…)`", ctx)
        self.assertNotIn("old_helper", ctx)  # only what models actually got wrong
        self.assertEqual(open(os.path.join(repo, "CHANGELOG.md")).read(), CHANGELOG)  # docs untouched

    def test_e8_stale_rate_with_wilson_interval(self):
        # 4 of 10 samples stale: alternate code by sample index
        script = ("import os; k = int(os.environ['CUTOFF_SAMPLE']); "
                  "print('```python\\n' + (%r if k < 4 else %r) + '```')" % (STALE_KW, GOOD))
        model = "mixed=cmd:%s -c %s" % (shlex.quote(sys.executable), shlex.quote(script))
        code, out = cli(make_repo(), "run", "--model", model, "-k", "10")
        self.assertIn("40%  (4/10, 95% CI 17–69%)", out)
        lo, hi = cutoff.wilson(4, 10)
        self.assertEqual((round(100 * lo), round(100 * hi)), (17, 69))

    def test_e9_ci_regression_gate(self):
        repo = make_repo()
        cli(repo, "run", "--model", fake("m", GOOD), "-k", "2", "--update-baseline")
        self.assertEqual(cli(repo, "run", "--model", fake("m", GOOD), "-k", "2", "--ci")[0], 0)
        code, out = cli(repo, "run", "--model", fake("m", STALE_KW), "-k", "2", "--ci")
        self.assertEqual(code, 1)
        self.assertIn("connect-timeout", out.split("Stale rate went up vs baseline on:")[1])

    def test_e10_no_leftovers(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "cutoff-*")))
        repo = make_repo()
        results(repo, fake("m", STALE_KW))
        leftovers = set(glob.glob(os.path.join(tempfile.gettempdir(), "cutoff-*"))) - before - {repo}
        self.assertEqual(leftovers, set())

    def test_e11_deterministic(self):
        repo = make_repo()
        a = cli(repo, "run", "--model", fake("m", STALE_KW), "-k", "3")[1]
        b = cli(repo, "run", "--model", fake("m", STALE_KW), "-k", "3")[1]
        self.assertEqual(a, b)


class TestInstall(unittest.TestCase):
    def test_install_runs_in_a_fresh_venv(self):
        # No network needed: the "install" copies the library into the venv's site-packages.
        cfg = CONFIG.replace('python_path = ["src"]',
                             'install = "python -c \\"import shutil,sysconfig; shutil.copytree(\'src/fixturelib\', '
                             'sysconfig.get_paths()[\'purelib\'] + \'/fixturelib\')\\""')
        repo = make_repo(cfg)
        r = results(repo, fake("good", GOOD), k=1)[0]
        self.assertEqual(r["status"], "pass")
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "cutoff-venv-*")))
        results(repo, fake("good", GOOD), k=1)
        self.assertEqual(set(glob.glob(os.path.join(tempfile.gettempdir(), "cutoff-venv-*"))) - before, set())


class TestCleanRoom(unittest.TestCase):
    def test_claude_runs_without_user_settings(self):
        from unittest import mock
        seen = {}
        fake_run = lambda cmd, **k: seen.setdefault("cmd", cmd) and mock.Mock(  # noqa: E731
            stdout='{"result": ""}', stderr="", returncode=0)
        with mock.patch.object(cutoff.subprocess, "run", fake_run), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            cutoff.Model("claude:haiku").ask("p", 5, 0)
        self.assertIn("--setting-sources", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--setting-sources") + 1], "")
        self.assertIn("--tools", seen["cmd"])


class TestOutagesAreNotStaleness(unittest.TestCase):
    """Regression from the first real run: a session limit came back as text, was executed as Python,
    raised SyntaxError, and 40 of 45 samples were recorded as 'crash'."""

    LIMIT = "%s -c \"import sys; print('You have hit your session limit, resets 5:10pm'); sys.exit(1)\"" % shlex.quote(sys.executable)

    def test_rate_limit_stops_the_run(self):
        code, out = cli(make_repo(), "run", "--model", "limited=cmd:" + self.LIMIT, "-k", "2")
        self.assertEqual(code, 3)
        self.assertIn("Stopped", out)
        self.assertNotIn("Stale-API rate", out)

    def test_prose_is_not_executed_as_code(self):
        prose = "%s -c \"print('Sorry, I cannot help with that.')\"" % shlex.quote(sys.executable)
        r = results(make_repo(), "chatty=cmd:" + prose)
        self.assertEqual({x["status"] for x in r}, {"no-code"})

    def test_unscored_samples_are_excluded_from_rates_and_flagged(self):
        script = ("import os; k = int(os.environ['CUTOFF_SAMPLE']); "
                  "print('```python\\n' + %r + '```') if k < 2 else print('no code here')" % STALE_KW)
        model = "half=cmd:%s -c %s" % (shlex.quote(sys.executable), shlex.quote(script))
        code, out = cli(make_repo(), "run", "--model", model, "-k", "4")
        self.assertIn("100%  (2/2", out)                       # 2 scored samples, both stale; the 2 prose ones don't dilute it
        self.assertIn("2 more sample(s) errored or returned no code", out)

    def test_missing_model_command_is_an_outage(self):
        code, out = cli(make_repo(), "run", "--model", "ghost=cmd:definitely-not-a-command-xyz", "-k", "1")
        self.assertEqual(code, 3)

    def test_future_warning_counts_as_deprecated(self):
        repo = make_repo()
        with open(os.path.join(repo, "src", "fixturelib", "__init__.py"), "a") as f:
            f.write('\n\ndef old_style():\n    warnings.warn("old_style is deprecated", FutureWarning, stacklevel=2)\n    return 1\n')
        cfg = CONFIG.replace('deprecated = ["legacy_mode"]', 'deprecated = ["old_style"]').replace(
            'assert solution.result["timeout"] == 5', 'assert solution.result["timeout"] == 5\nassert solution.other == 1')
        with open(os.path.join(repo, "cutoff.toml"), "w") as f:
            f.write(cfg)
        code_ = 'import fixturelib\nresult = fixturelib.connect("db", timeout=5)\nother = fixturelib.old_style()\n'
        self.assertEqual(results(repo, fake("pandasish", code_))[0]["status"], "deprecated-api")


class TestSnippetUsesOnlyTheNewestRelease(unittest.TestCase):
    """Regression from the real httpx run: the snippet included superseded advice from old releases
    ("Switched to proxies=httpx.Proxy(...)"), contradicting the current API."""

    HTTPX_LIKE = textwrap.dedent("""\
        # Changelog

        ## 0.28.0 (28th November, 2024)

        * The `verify` argument as a string argument is now deprecated and will raise warnings.
        * The deprecated `proxies` argument has now been removed.

        ## 0.26.0 (20th December, 2023)

        * The `proxy` argument was added. You should use the `proxy` argument instead of the deprecated `proxies`.
        * The `proxies` argument is now deprecated. It will still continue to work, but it will be removed.

        ## 0.13.0

        - Switched to `proxies=httpx.Proxy(...)` for proxy configuration.
    """)

    def test_newest_mention_wins(self):
        lib = {"name": "httpx", "removed": ["proxies"], "deprecated": ["verify"]}
        results_ = [{"status": "removed-api", "detail": "unexpected keyword argument 'proxies'", "stale_line": ""},
                    {"status": "deprecated-api", "detail": "`verify=<str>` is deprecated", "stale_line": ""}]
        ctx = cutoff.draft_context(lib, results_, self.HTTPX_LIKE)
        self.assertIn("The deprecated `proxies` argument has now been removed.", ctx)
        self.assertIn("`verify` argument as a string argument is now deprecated", ctx)
        self.assertIn("use the `proxy` argument instead of the deprecated `proxies`", ctx)   # the replacement
        self.assertNotIn("still continue to work", ctx)
        self.assertNotIn("Switched to `proxies=httpx.Proxy", ctx)

    def test_release_sections(self):
        secs = cutoff.release_sections(self.HTTPX_LIKE)
        self.assertEqual(len(secs), 4)                      # preamble + 3 releases
        self.assertIn("0.28.0", secs[1])


class TestUnits(unittest.TestCase):
    def test_extract_code(self):
        self.assertEqual(cutoff.extract_code("Here:\n```python\nx = 1\n```\nDone."), "x = 1\n")
        self.assertEqual(cutoff.extract_code("x = 2\n"), "x = 2\n")
        self.assertIsNone(cutoff.extract_code("You've hit your session limit · resets 5:10pm"))
        self.assertIsNone(cutoff.extract_code("   \n"))

    def test_changelog_entries(self):
        kinds = [(k, s) for k, _, s in cutoff.changelog_entries(CHANGELOG)]
        self.assertEqual(kinds, [("removed", ["old_helper", "helper"]), ("changed", ["connect", "connect"]),
                                 ("deprecated", ["legacy_mode"])])

    def test_bad_config_is_explained(self):
        with self.assertRaises(SystemExit):
            cutoff.load_config(make_repo('[library]\nname = "x"\n[[probe]]\nid = "a"\n'))


if __name__ == "__main__":
    unittest.main()
