import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "etc/calamares/scripts/configure-selected-desktop"


class NoctaliaDesktopBehaviorTests(unittest.TestCase):
    def test_missing_optional_helpers_are_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / SCRIPT.name
            shutil.copy2(SCRIPT, script)
            result = subprocess.run(
                [str(script), "Niri-noctalia", "test-user"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("missing display-manager helper", result.stderr)
        self.assertIn("missing Catdot helper", result.stderr)

    def test_missing_autologin_user_preserves_display_manager_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            script = directory / SCRIPT.name
            helper = directory / "configure-display-manager"
            arguments = directory / "arguments"
            shutil.copy2(SCRIPT, script)
            helper.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$DM_ARGUMENTS"\n',
                encoding="utf-8",
            )
            helper.chmod(0o755)
            result = subprocess.run(
                [str(script), "Niri-dms", "test-user"],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "DM_ARGUMENTS": str(arguments)},
            )
            captured_arguments = arguments.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured_arguments[:2], ["--autologin-session", "niri"])
        self.assertNotIn("--autologin-user", captured_arguments)


if __name__ == "__main__":
    unittest.main()
