from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "etc/calamares/scripts/configure-display-manager"


class DisplayManagerBehaviorTests(unittest.TestCase):
    def make_file(self, root: Path, relative: str, executable: bool = False) -> Path:
        path = root / relative.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" if executable else "unit\n", encoding="utf-8")
        if executable:
            path.chmod(0o755)
        return path

    def install_ly(self, root: Path) -> None:
        self.make_file(root, "/usr/bin/ly-dm", executable=True)
        self.make_file(root, "/usr/lib/systemd/system/ly@.service")
        config = root / "etc/ly/config.ini"
        config.parent.mkdir(parents=True)
        config.write_text(
            "auto_login_service = ly-autologin\n"
            "auto_login_session = live\n"
            "auto_login_user = liveuser\n",
            encoding="utf-8",
        )

    def install_gdm(self, root: Path) -> None:
        self.make_file(root, "/usr/bin/gdm", executable=True)
        self.make_file(root, "/usr/lib/systemd/system/gdm.service")

    def install_sddm(self, root: Path) -> None:
        self.make_file(root, "/usr/bin/sddm", executable=True)
        self.make_file(root, "/usr/lib/systemd/system/sddm.service")

    def install_noctalia(self, root: Path, greetd: bool = True) -> None:
        self.make_file(root, "/usr/bin/noctalia-greeter", executable=True)
        self.make_file(root, "/usr/bin/noctalia-greeter-session", executable=True)
        if greetd:
            self.make_file(root, "/usr/bin/greetd", executable=True)
            self.make_file(root, "/usr/lib/systemd/system/greetd.service")
        etc = root / "etc"
        etc.mkdir(exist_ok=True)
        uid = os.getuid()
        gid = os.getgid()
        (etc / "passwd").write_text(
            f"greeter:x:{uid}:{gid}:Greeter:/var/lib/noctalia-greeter:/usr/bin/nologin\n",
            encoding="utf-8",
        )
        (etc / "group").write_text(f"greeter:x:{gid}:\n", encoding="utf-8")

    def run_script(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CATOS_DM_ROOT"] = str(root)
        return subprocess.run(
            [str(SCRIPT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    def link_target(self, root: Path, relative: str) -> str | None:
        path = root / relative.lstrip("/")
        return os.readlink(path) if path.is_symlink() else None

    def test_preferred_ly_creates_only_tty_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_ly(root)
            self.install_gdm(root)
            result = self.run_script(root, "ly", "gdm", "sddm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/multi-user.target.wants/ly@tty2.service"),
                "/usr/lib/systemd/system/ly@.service",
            )
            self.assertIsNone(self.link_target(root, "/etc/systemd/system/display-manager.service"))

    def test_missing_preference_falls_back_in_argument_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_gdm(root)
            self.install_sddm(root)
            result = self.run_script(root, "ly", "gdm", "sddm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/gdm.service",
            )

    def test_complete_noctalia_configures_greetd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_noctalia(root)
            result = self.run_script(root, "noctalia-greeter", "gdm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/greetd.service",
            )
            self.assertEqual(
                (root / "etc/greetd/config.toml").read_text(encoding="utf-8"),
                "[terminal]\nvt = 1\n\n[default_session]\n"
                'command = "/usr/bin/noctalia-greeter-session"\n'
                'user = "greeter"\n',
            )
            self.assertTrue((root / "var/lib/noctalia-greeter").is_dir())

    def test_incomplete_noctalia_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_noctalia(root, greetd=False)
            self.install_gdm(root)
            result = self.run_script(root, "noctalia-greeter", "gdm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/gdm.service",
            )
            self.assertFalse((root / "etc/greetd/config.toml").exists())

    def test_no_usable_candidate_warns_and_preserves_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            systemd = root / "etc/systemd/system"
            systemd.mkdir(parents=True)
            (systemd / "display-manager.service").symlink_to(
                "/usr/lib/systemd/system/sddm.service"
            )

            result = self.run_script(root, "ly", "gdm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no usable display manager", result.stderr.lower())
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/sddm.service",
            )

    def test_none_and_new_selection_remove_conflicting_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_gdm(root)
            self.install_sddm(root)
            self.install_ly(root)
            systemd = root / "etc/systemd/system"
            (systemd / "multi-user.target.wants").mkdir(parents=True)
            (systemd / "graphical.target.wants").mkdir(parents=True)
            (systemd / "display-manager.service").symlink_to(
                "/usr/lib/systemd/system/sddm.service"
            )
            (systemd / "multi-user.target.wants/ly@tty2.service").symlink_to(
                "/usr/lib/systemd/system/ly@.service"
            )
            (systemd / "graphical.target.wants/sddm.service").symlink_to(
                "/usr/lib/systemd/system/sddm.service"
            )

            selected = self.run_script(root, "gdm", "ly")
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/gdm.service",
            )
            self.assertFalse((systemd / "multi-user.target.wants/ly@tty2.service").exists())
            self.assertFalse((systemd / "graphical.target.wants/sddm.service").exists())

            cleared = self.run_script(root, "none")
            self.assertEqual(cleared.returncode, 0, cleared.stderr)
            self.assertFalse((systemd / "display-manager.service").exists())

    def test_ly_autologin_uses_requested_user_and_session_or_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.install_ly(root)
            enabled = self.run_script(
                root,
                "--autologin-user",
                "alice",
                "--autologin-session",
                "niri",
                "ly",
            )
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            config = (root / "etc/ly/config.ini").read_text(encoding="utf-8")
            self.assertIn("auto_login_user = alice", config)
            self.assertIn("auto_login_session = niri", config)
            self.assertNotIn("liveuser", config)

            disabled = self.run_script(root, "--autologin-user", "none", "ly")
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            config = (root / "etc/ly/config.ini").read_text(encoding="utf-8")
            self.assertIn("auto_login_user = null", config)
            self.assertIn("auto_login_session = null", config)


if __name__ == "__main__":
    unittest.main()
