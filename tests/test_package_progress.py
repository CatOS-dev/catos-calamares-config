from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "usr/lib/calamares/modules/package_progress.py"


def load_progress_module():
    spec = importlib.util.spec_from_file_location("catos_test_package_progress", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageProgressTests(unittest.TestCase):
    def test_parses_stable_pacman_download_plan(self) -> None:
        module = load_progress_module()
        plan = module.parse_download_plan(
            [
                "linux\t6.15-1\t1048576\thttps://repo.example/linux-6.15-1-x86_64.pkg.tar.zst",
                "pacman\t7.1-1\t0\tfile:///var/cache/pacman/pkg/pacman-7.1-1-x86_64.pkg.tar.zst",
                "malformed",
            ]
        )

        self.assertEqual(plan.total_bytes, 1048576)
        self.assertEqual(len(plan.downloads), 1)
        self.assertEqual(plan.downloads[0].name, "linux")
        self.assertEqual(plan.downloads[0].filename, "linux-6.15-1-x86_64.pkg.tar.zst")

    def test_samples_partial_files_and_real_transfer_speed(self) -> None:
        module = load_progress_module()
        plan = module.parse_download_plan(
            ["linux\t6.15-1\t1024\thttps://repo.example/linux.pkg.tar.zst"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            partial = cache / "linux.pkg.tar.zst.part"
            partial.write_bytes(b"x" * 256)
            sampler = module.TransferSampler(plan, [cache])

            first = sampler.sample(now=10.0)
            self.assertEqual(first.downloaded_bytes, 256)
            self.assertEqual(first.speed_bytes_per_second, 0.0)
            self.assertAlmostEqual(first.ratio, 0.25)
            self.assertEqual(first.active_packages, ("linux",))

            partial.write_bytes(b"x" * 768)
            second = sampler.sample(now=12.0)
            self.assertEqual(second.downloaded_bytes, 768)
            self.assertEqual(second.speed_bytes_per_second, 256.0)
            self.assertAlmostEqual(second.ratio, 0.75)

            partial.unlink()
            (cache / "linux.pkg.tar.zst").write_bytes(b"x" * 1024)
            final = sampler.sample(now=13.0)
            self.assertEqual(final.downloaded_bytes, 1024)
            self.assertAlmostEqual(final.ratio, 1.0)
            self.assertEqual(final.active_packages, ())

    def test_samples_repository_database_refresh_bytes_and_completion(self) -> None:
        module = load_progress_module()
        with tempfile.TemporaryDirectory() as temporary:
            sync = Path(temporary)
            database = sync / "core.db"
            database.write_bytes(b"o" * 1024)
            sampler = module.RepositoryDatabaseSampler(sync, ["core"])

            partial = sync / "core.db.part"
            partial.write_bytes(b"x" * 256)
            first = sampler.sample(now=10.0)
            self.assertEqual(first.transferred_bytes, 256)
            self.assertEqual(first.completed_repositories, 0)
            self.assertAlmostEqual(first.ratio, 0.25)
            self.assertEqual(first.active_repositories, ("core",))

            partial.write_bytes(b"x" * 768)
            second = sampler.sample(now=12.0)
            self.assertEqual(second.transferred_bytes, 768)
            self.assertEqual(second.speed_bytes_per_second, 256.0)
            self.assertAlmostEqual(second.ratio, 0.75)

            partial.unlink()
            database.write_bytes(b"new database" * 100)
            final = sampler.sample(now=13.0)
            self.assertEqual(final.completed_repositories, 1)
            self.assertEqual(final.ratio, 1.0)
            self.assertEqual(final.active_repositories, ())

    def test_terminal_decoder_splits_carriage_returns_and_newlines(self) -> None:
        module = load_progress_module()
        decoder = module.TerminalFrameDecoder()

        frames = decoder.feed(b"one\rtw")
        frames += decoder.feed(b"o\r\nthree\nfour")
        frames += decoder.finish()

        self.assertEqual(frames, ["one", "two", "three", "four"])

    def test_builds_non_mutating_plan_only_for_download_operations(self) -> None:
        module = load_progress_module()
        install = module.build_pacman_plan_command(
            ["pacman", "-S", "--noconfirm", "linux"]
        )
        self.assertEqual(
            install[:4],
            ["pacman", "--print", "--print-format", module.PACMAN_PRINT_FORMAT],
        )
        self.assertIn("linux", install)
        self.assertIsNone(
            module.build_pacman_plan_command(["pacman", "-Syu", "--noconfirm"])
        )
        self.assertIsNone(
            module.build_pacman_plan_command(["pacman", "-R", "--noconfirm", "linux"])
        )
        self.assertIsNone(
            module.build_pacman_plan_command(["pacman", "-U", "--noconfirm", "/tmp/linux.pkg.tar.zst"])
        )

    def test_parses_numeric_transaction_prefix_without_english_text(self) -> None:
        module = load_progress_module()
        self.assertAlmostEqual(
            module.parse_transaction_progress("( 42/100) 正在安装软件包"), 0.42
        )
        self.assertIsNone(module.parse_transaction_progress("downloading linux"))

    def test_transaction_tracker_does_not_finish_on_short_check_stage(self) -> None:
        module = load_progress_module()
        tracker = module.PacmanTransactionTracker()

        key_complete = tracker.observe("( 2/2) checking keys in keyring")
        install_start = tracker.observe("( 1/120) installing linux")
        install_middle = tracker.observe("( 60/120) installing systemd")
        self.assertIsNone(tracker.observe(":: Running post-transaction hooks..."))
        hook_complete = tracker.observe("( 4/4) Updating icon theme caches...")

        self.assertIsNotNone(key_complete)
        self.assertLess(key_complete, 0.20)
        self.assertGreaterEqual(install_start, key_complete)
        self.assertGreater(install_middle, install_start)
        self.assertEqual(hook_complete, 1.0)

    def test_maps_download_ratio_into_real_job_phase(self) -> None:
        module = load_progress_module()
        self.assertAlmostEqual(module.map_progress(0.10, 0.80, 0.50), 0.45)
        self.assertAlmostEqual(module.map_progress(0.10, 0.80, -1.0), 0.10)
        self.assertAlmostEqual(module.map_progress(0.10, 0.80, 2.0), 0.80)


if __name__ == "__main__":
    unittest.main()
