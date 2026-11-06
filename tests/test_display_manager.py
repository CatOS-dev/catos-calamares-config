from __future__ import annotations

import os
import configparser
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

    def test_gxde_selection_applies_stored_autologin_and_can_disable_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_file(root, "/usr/bin/gxdm", executable=True)
            self.make_file(root, "/usr/lib/systemd/system/gxdm.service")
            config = self.make_file(root, "/etc/gxdm.conf")
            config.write_text(
                "[General]\nDisplayServer=wayland\n"
                "[Autologin]\nUser=liveuser\nSession=live.desktop\nRelogin=false\n"
                "[Users]\nRememberLastUser=true\n",
                encoding="utf-8",
            )
            stored = self.run_script(root, "--autologin-user", "alice")
            self.assertEqual(stored.returncode, 0, stored.stderr)
            env = dict(os.environ, CATOS_DM_ROOT=str(root))
            selected = subprocess.run(
                [str(SCRIPT.with_name("configure-selected-desktop")), "GXDE", "alice"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(selected.returncode, 0, selected.stderr)
            enabled = configparser.ConfigParser()
            enabled.read(config)
            self.assertEqual(enabled["Autologin"]["User"], "alice")
            self.assertEqual(enabled["Autologin"]["Session"], "gxde-flakewm.desktop")
            self.assertEqual(enabled["Autologin"]["Relogin"], "false")
            self.assertEqual(enabled["General"]["DisplayServer"], "wayland")
            self.assertEqual(enabled["Users"]["RememberLastUser"], "true")
            self.assertEqual(
                self.link_target(root, "/etc/systemd/system/display-manager.service"),
                "/usr/lib/systemd/system/gxdm.service",
            )
            disabled = self.run_script(root, "--autologin-user", "none", "gxdm")
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            cleared = configparser.ConfigParser()
            cleared.read(config)
            self.assertEqual(cleared["Autologin"]["User"], "")
            self.assertEqual(cleared["Autologin"]["Session"], "")

    def test_gxdm_autologin_creates_configuration_on_fresh_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_file(root, "/usr/bin/gxdm", executable=True)
            self.make_file(root, "/usr/lib/systemd/system/gxdm.service")
            result = self.run_script(
                root, "--autologin-user", "alice",
                "--autologin-session", "gxde-flakewm", "gxdm",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = configparser.ConfigParser()
            config.read(root / "etc/gxdm.conf")
            self.assertEqual(config["Autologin"]["User"], "alice")
            self.assertEqual(config["Autologin"]["Session"], "gxde-flakewm.desktop")


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
