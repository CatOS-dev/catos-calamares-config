from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CachyOSRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = load_module(
            "cachyos_repository",
            "etc/calamares/scripts/cachyos-repository.py",
        )
        cls.pacstrap_repository = load_module(
            "pacstrap_repository",
            "usr/lib/calamares/modules/pacstrap/pacstrap_repository.py",
        )

    def test_architecture_selection_prefers_znver4_then_v4_then_v3(self):
        loader_v4 = """
          x86-64-v2 (supported, searched)
          x86-64-v3 (supported, searched)
          x86-64-v4 (supported, searched)
        """
        loader_v3 = """
          x86-64-v2 (supported, searched)
          x86-64-v3 (supported, searched)
        """

        self.assertEqual(
            self.repository.select_architecture("znver5", loader_v4),
            "znver4",
        )
        self.assertEqual(
            self.repository.select_architecture("skylake-avx512", loader_v4),
            "v4",
        )
        self.assertEqual(
            self.repository.select_architecture("haswell", loader_v3),
            "v3",
        )
        self.assertEqual(
            self.repository.select_architecture("x86-64", ""),
            "x86_64",
        )
        self.assertTrue(self.repository.supports_optimized_repositories("v3"))
        self.assertTrue(self.repository.supports_optimized_repositories("v4"))
        self.assertTrue(self.repository.supports_optimized_repositories("znver4"))
        self.assertFalse(self.repository.supports_optimized_repositories("x86_64"))
        with self.assertRaisesRegex(RuntimeError, "x86-64-v3"):
            self.repository.require_supported_architecture("x86_64")

        original_detect = self.repository.detect_architecture
        original_write = self.repository.write_repository_configuration
        side_effects = []
        try:
            self.repository.detect_architecture = lambda: "x86_64"
            self.repository.write_repository_configuration = side_effects.append
            self.assertEqual(self.repository.main(), 1)
            self.assertEqual(side_effects, [])
        finally:
            self.repository.detect_architecture = original_detect
            self.repository.write_repository_configuration = original_write

    def test_repository_config_precedes_arch_and_uses_local_mirrorlists(self):
        base = """[options]\nArchitecture = auto\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n\n[extra]\nInclude = /etc/pacman.d/mirrorlist\n"""
        rendered = self.repository.render_pacman_config(
            base,
            "v3",
            Path("/run/calamares/cachyos/pacman.d"),
        )

        self.assertLess(rendered.index("[cachyos-v3]"), rendered.index("[core]"))
        self.assertIn("[cachyos-core-v3]", rendered)
        self.assertIn("[cachyos-extra-v3]", rendered)
        self.assertNotIn("[cachyos]", rendered)
        self.assertIn(
            "Include = /run/calamares/cachyos/pacman.d/cachyos-v3-mirrorlist",
            rendered,
        )
        self.assertIn("Architecture = x86_64 x86_64_v3", rendered)
        self.assertNotIn("Architecture = auto", rendered)
        self.assertNotIn("mirror.cachyos.org", rendered)

        target_rendered = self.repository.render_pacman_config(
            base,
            "v3",
            Path("/etc/pacman.d"),
            include_base_repository=True,
        )
        self.assertIn(
            "Include = /etc/pacman.d/cachyos-v3-mirrorlist",
            target_rendered,
        )
        self.assertIn("[cachyos]", target_rendered)
        self.assertNotIn("/run/calamares", target_rendered)

        rendered_v4 = self.repository.render_pacman_config(base, "v4")
        rendered_znver4 = self.repository.render_pacman_config(base, "znver4")
        rendered_base = self.repository.render_pacman_config(base, "x86_64")
        self.assertIn("Architecture = x86_64 x86_64_v4", rendered_v4)
        self.assertIn("Architecture = x86_64 x86_64_v4", rendered_znver4)
        self.assertIn("Architecture = x86_64", rendered_base)
        self.assertNotIn("x86_64_v3", rendered_base)
        self.assertNotIn("x86_64_v4", rendered_base)

        mirrorlists = self.repository.render_mirrorlists()
        self.assertEqual(
            set(mirrorlists),
            {
                "cachyos-mirrorlist",
                "cachyos-v3-mirrorlist",
                "cachyos-v4-mirrorlist",
            },
        )
        combined = "\n".join(mirrorlists.values())
        self.assertIn("mirror.nju.edu.cn/cachyos/repo/x86_64/$repo", combined)
        self.assertIn("mirrors.ustc.edu.cn/cachyos/repo/x86_64/$repo", combined)
        self.assertIn("mirror.nju.edu.cn/cachyos/repo/x86_64_v3/$repo", combined)
        self.assertIn("mirrors.ustc.edu.cn/cachyos/repo/x86_64_v4/$repo", combined)
        self.assertNotIn("cdn77", combined)
        self.assertNotIn("mirror.cachyos.org", combined)

    def test_pacstrap_switches_kernel_and_config_together(self):
        packages = ["base", "linux", "linux-headers", "grub"]
        selected = self.pacstrap_repository.transform_packages(packages, "cachyos")
        self.assertEqual(
            selected,
            ["base", "linux-cachyos", "linux-cachyos-headers", "grub"],
        )
        self.assertEqual(
            self.pacstrap_repository.pacman_config_for("cachyos"),
            "/run/calamares/cachyos/pacman.conf",
        )
        self.assertEqual(
            self.pacstrap_repository.transform_packages(packages, "catos"),
            packages,
        )
        self.assertEqual(
            self.pacstrap_repository.pacman_config_for("catos"),
            "/etc/pacman.conf",
        )

    def test_generated_config_and_mirrorlists_are_copied_to_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            generated = target / "generated"
            generated.mkdir()
            (generated / "target-pacman.conf").write_text(
                "[cachyos]\n",
                encoding="utf-8",
            )
            mirror_dir = generated / "pacman.d"
            mirror_dir.mkdir()
            for name in self.repository.render_mirrorlists():
                (mirror_dir / name).write_text(name + "\n", encoding="utf-8")

            copied = self.pacstrap_repository.install_repository_config(
                target,
                "cachyos",
                generated,
            )

            self.assertTrue(copied)
            self.assertEqual(
                (target / "etc/pacman.conf").read_text(encoding="utf-8"),
                "[cachyos]\n",
            )
            for name in self.repository.render_mirrorlists():
                self.assertTrue((target / "etc/pacman.d" / name).is_file())

    def test_bootstrap_skips_without_cachyos_and_switches_pacman_when_enabled(self):
        script = ROOT / "etc/calamares/scripts/bootstrap-cachyos"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            log = root / "calls.log"
            mock = bin_dir / "mock-command"
            mock.write_text(
                "#!/bin/sh\n"
                "printf '%s %s\\n' \"${0##*/}\" \"$*\" >> \"$CALL_LOG\"\n"
                "if [ \"${0##*/}\" = pacman-conf ]; then\n"
                "    printf '%s\\n' \"$MOCK_GPG_DIR\"\n"
                "fi\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            for command in ("pacman", "pacman-key", "pacman-conf", "gpgconf"):
                (bin_dir / command).symlink_to(mock)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                    "CALL_LOG": str(log),
                    "MOCK_GPG_DIR": str(root / "gnupg"),
                }
            )

            plain_config = root / "plain-pacman.conf"
            plain_config.write_text("[core]\n", encoding="utf-8")
            environment["CACHYOS_BOOTSTRAP_PACMAN_CONFIG"] = str(plain_config)
            subprocess.run([str(script)], env=environment, check=True)
            self.assertFalse(log.exists())

            cachyos_config = root / "cachyos-pacman.conf"
            cachyos_config.write_text(
                "[options]\nArchitecture = x86_64 x86_64_v3\n"
                "[cachyos-v3]\n[cachyos]\n[core]\n",
                encoding="utf-8",
            )
            environment["CACHYOS_BOOTSTRAP_PACMAN_CONFIG"] = str(cachyos_config)
            subprocess.run([str(script)], env=environment, check=True)

            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertIn("pacman-key --init", calls)
            self.assertIn("pacman-key --populate archlinux", calls)
            self.assertFalse(any("--keyserver" in call for call in calls))
            self.assertIn(
                "pacman -Sy --noconfirm --needed cachyos/cachyos-keyring",
                calls,
            )
            self.assertIn("pacman-key --populate cachyos", calls)
            self.assertIn("pacman -S --noconfirm cachyos/pacman", calls)
            self.assertNotIn(
                "pacman -S --noconfirm --needed cachyos/pacman",
                calls,
            )
            self.assertIn(
                "Architecture = auto",
                cachyos_config.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
