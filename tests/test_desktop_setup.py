from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "etc/calamares/scripts/configure-selected-desktop"


class DesktopSetupBehaviorTests(unittest.TestCase):
    def prepare_scripts(self, root: Path) -> Path:
        script_dir = root / "scripts"
        script_dir.mkdir()
        wrapper = script_dir / SCRIPT.name
        shutil.copy2(SCRIPT, wrapper)

        for name, prefix in (
            ("configure-display-manager", "dm"),
            ("activate-catdot-profile", "profile"),
        ):
            helper = script_dir / name
            helper.write_text(
                "#!/bin/sh\n"
                f"printf '{prefix}' >> \"$CALL_LOG\"\n"
                "printf ' <%s>' \"$@\" >> \"$CALL_LOG\"\n"
                "printf '\\n' >> \"$CALL_LOG\"\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
        return wrapper

    def remove_helper(self, wrapper: Path, name: str) -> None:
        (wrapper.parent / name).unlink()

    def replace_helper(self, wrapper: Path, name: str, exit_code: int) -> None:
        helper = wrapper.parent / name
        helper.write_text(
            "#!/bin/sh\n"
            f"printf '{name} failed\\n' >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)

    def run_wrapper(self, wrapper: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
        log = wrapper.parent.parent / "calls.log"
        env = os.environ.copy()
        env["CALL_LOG"] = str(log)
        result = subprocess.run(
            [str(wrapper), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        return result, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_noctalia_is_configured_and_profile_is_activated_in_one_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            result, calls = self.run_wrapper(wrapper, "Niri-noctalia", "alice", "alice")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls,
                "dm <--autologin-user> <alice> <--autologin-session> <niri>"
                " <noctalia-greeter> <ly> <gdm> <sddm> <lightdm> <plasmalogin>\n"
                "profile <alice> <catos-niri-noctaliav5>\n",
            )

    def test_desktop_without_catdot_profile_only_configures_display_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            result, calls = self.run_wrapper(wrapper, "GNOME-Desktop", "alice", "")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls,
                "dm <--autologin-user> <none> <gdm> <lightdm> <sddm> <plasmalogin>\n",
            )

    def test_unknown_desktop_warns_without_partial_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            result, calls = self.run_wrapper(wrapper, "Typo-Desktop", "alice", "alice")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unsupported desktop selection", result.stderr)
            self.assertEqual(calls, "")

    def test_desktop_without_profile_does_not_require_profile_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            self.remove_helper(wrapper, "activate-catdot-profile")
            result, calls = self.run_wrapper(wrapper, "GNOME-Desktop", "alice", "")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls,
                "dm <--autologin-user> <none> <gdm> <lightdm> <sddm> <plasmalogin>\n",
            )

    def test_display_manager_failure_is_a_warning_and_profile_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            self.replace_helper(wrapper, "configure-display-manager", 17)
            result, calls = self.run_wrapper(wrapper, "Niri-noctalia", "alice", "")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("display-manager setup failed", result.stderr)
            self.assertEqual(calls, "profile <alice> <catos-niri-noctaliav5>\n")

    def test_profile_failure_is_a_warning_not_an_install_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper = self.prepare_scripts(Path(tmpdir))
            self.replace_helper(wrapper, "activate-catdot-profile", 23)
            result, calls = self.run_wrapper(wrapper, "Niri-noctalia", "alice", "")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Catdot Profile activation failed", result.stderr)
            self.assertEqual(
                calls,
                "dm <--autologin-user> <none> <--autologin-session> <niri>"
                " <noctalia-greeter> <ly> <gdm> <sddm> <lightdm> <plasmalogin>\n",
            )


if __name__ == "__main__":
    unittest.main()
