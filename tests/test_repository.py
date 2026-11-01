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


if __name__ == "__main__":
    unittest.main()
