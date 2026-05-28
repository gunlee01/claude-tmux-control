import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_import_boundaries  # noqa: E402


class ImportBoundaryScriptTest(unittest.TestCase):
    def test_current_tree_passes_import_boundary_check(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_import_boundaries.py")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("import boundary check passed", result.stdout)

    def test_state_module_must_not_import_bridge_sessions(self):
        violations = check_import_boundaries.violations_for_sources(
            {"ctc_state": "from ctc_bridge_sessions import recover_stale_active_turn\n"}
        )

        self.assertEqual(violations, ["ctc_state: forbidden import: ctc_bridge_sessions"])

    def test_extraction_modules_must_not_import_facade(self):
        violations = check_import_boundaries.violations_for_sources(
            {"ctc_streaming": "import claude_tmux_control\n"}
        )

        self.assertEqual(violations, ["ctc_streaming: forbidden import: claude_tmux_control"])


if __name__ == "__main__":
    unittest.main()
