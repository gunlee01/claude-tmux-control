import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RefactorContractScriptTest(unittest.TestCase):
    def test_phase_zero_dry_run_lists_facade_contract_gate(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "refactor_contract_check.py"), "--phase", "0", "--dry-run"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RefactorCompatibilityContractTest", result.stdout)
        self.assertIn("py_compile", result.stdout)
        self.assertIn("scripts/check_docs.py", result.stdout)
        self.assertIn("claude_tmux_control.py --version", result.stdout)

    def test_all_dry_run_lists_full_unittest_gate(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "refactor_contract_check.py"), "--phase", "all", "--dry-run"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-m unittest discover -s tests", result.stdout)


if __name__ == "__main__":
    unittest.main()
