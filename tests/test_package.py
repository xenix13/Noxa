"""The former package name `seance` keeps working for servers installed before 4.5."""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version_in(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


class FormerNameTest(unittest.TestCase):
    def test_same_version(self):
        # servers before 4.5 read seance/__init__.py on GitHub to find new versions
        self.assertEqual(version_in("seance/__init__.py"), version_in("noxa/__init__.py"))

    def test_old_service_command_starts_noxa(self):
        r = subprocess.run([sys.executable, "-m", "seance", "--help"], cwd=ROOT, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--data", r.stdout)

    def test_env_reads_both_names(self):
        from noxa import env
        os.environ.pop("NOXA_BENCH_X", None)
        os.environ["SEANCE_BENCH_X"] = "old"
        try:
            self.assertEqual(env("BENCH_X"), "old")
            os.environ["NOXA_BENCH_X"] = "new"
            self.assertEqual(env("BENCH_X"), "new")
        finally:
            os.environ.pop("NOXA_BENCH_X", None)
            os.environ.pop("SEANCE_BENCH_X", None)


if __name__ == "__main__":
    unittest.main()
