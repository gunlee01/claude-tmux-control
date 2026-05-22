import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CheckDocsScriptTest(unittest.TestCase):
    def test_docs_check_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_docs.py")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docs check passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
