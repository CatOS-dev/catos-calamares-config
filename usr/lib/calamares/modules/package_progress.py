#!/usr/bin/env python3
"""Download planning and cache-based progress telemetry for pacman operations."""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Iterable, NamedTuple, Sequence
from urllib.parse import unquote, urlparse


PACMAN_PRINT_FORMAT = "%n\t%v\t%s\t%l"
_TRANSACTION_PROGRESS = re.compile(r"^\(\s*(\d+)\s*/\s*(\d+)\s*\)\s*(.*)$")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class PlannedDownload(NamedTuple):
    name: str
    size: int
    filename: str


class DownloadPlan(NamedTuple):
    downloads: tuple[PlannedDownload, ...]
    total_bytes: int


class TransferSnapshot(NamedTuple):
    downloaded_bytes: int
    total_bytes: int
    speed_bytes_per_second: float
    ratio: float
    active_packages: tuple[str, ...]


def _package_filename(location: str) -> str:
    parsed = urlparse(location)
    path = parsed.path if parsed.scheme else location
    return unquote(Path(path).name)


def parse_download_plan(lines: Iterable[str]) -> DownloadPlan:
    """Parse locale-independent ``pacman --print-format`` rows."""
    downloads: dict[str, PlannedDownload] = {}
    for raw_line in lines:
        line = str(raw_line).strip()
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        name, _version, size_text, location = fields
        try:
            size = int(size_text)
        except ValueError:
            continue
        filename = _package_filename(location)
        if not name or size <= 0 or not filename:
            continue
        candidate = PlannedDownload(name=name, size=size, filename=filename)
        existing = downloads.get(filename)
        if existing is None or candidate.size > existing.size:
            downloads[filename] = candidate
    ordered = tuple(downloads.values())
    return DownloadPlan(ordered, sum(item.size for item in ordered))


def build_pacman_plan_command(command: Sequence[str]) -> list[str] | None:
    """Build a read-only pacman print command when the operation can download.

    Repository refresh operations are intentionally skipped because planning
    them would mutate sync databases before the real command runs.
    """
    if not command:
        return None
    short_options = "".join(
        argument[1:]
        for argument in command[1:]
        if argument.startswith("-") and not argument.startswith("--")
    )
    long_options = set(argument for argument in command[1:] if argument.startswith("--"))
    if "R" in short_options or "--remove" in long_options:
        return None
    if "y" in short_options or "--refresh" in long_options:
        return None
    if "S" not in short_options and "--sync" not in long_options:
        return None

    filtered = [argument for argument in command[1:] if argument != "--noprogressbar"]
    return [command[0], "--print", "--print-format", PACMAN_PRINT_FORMAT, *filtered]


def map_progress(start: float, end: float, ratio: float) -> float:
    bounded = min(1.0, max(0.0, float(ratio)))
    return float(start) + (float(end) - float(start)) * bounded


def is_download_start(frame: str) -> bool:
    """Return whether pacman output has entered the actual retrieval phase."""
    text = _ANSI_ESCAPE.sub("", str(frame)).strip().lower()
    return "retrieving packages" in text or text.endswith("downloading...")


def parse_transaction_progress(frame: str) -> float | None:
    text = _ANSI_ESCAPE.sub("", str(frame)).lstrip()
    match = _TRANSACTION_PROGRESS.match(text)
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    if total <= 0:
        return None
    return min(1.0, max(0.0, current / total))


class PacmanTransactionTracker:
    """Track real pacman transaction stages without treating each counter as global."""

    _STAGES = (
        (("running pre-transaction hooks",), 0.00, 0.03),
        (("checking keys in keyring",), 0.03, 0.06),
        (("checking package integrity",), 0.06, 0.11),
        (("loading package files",), 0.11, 0.15),
        (("checking for file conflicts",), 0.15, 0.18),
        (("checking available disk space",), 0.18, 0.20),
        (
            (
                "processing package changes",
                "installing ",
                "upgrading ",
                "reinstalling ",
                "downgrading ",
                "removing ",
            ),
            0.20,
            0.90,
        ),
        (("running post-transaction hooks",), 0.90, 1.00),
    )

    def __init__(self) -> None:
        self._progress = 0.0
        self._active_stage: tuple[float, float] | None = None

    @property
    def progress(self) -> float:
        return self._progress

    @property
    def started(self) -> bool:
        """Return whether pacman has entered a transaction stage."""
        return self._active_stage is not None

    def reset(self) -> None:
        self._progress = 0.0
        self._active_stage = None

    def observe(self, frame: str) -> float | None:
        text = _ANSI_ESCAPE.sub("", str(frame)).lstrip()
        lowered = text.lower()
        for patterns, start, end in self._STAGES:
            if any(pattern in lowered for pattern in patterns):
                self._active_stage = (start, end)
                break

        match = _TRANSACTION_PROGRESS.match(text)
        if not match:
            return None
        current = int(match.group(1))
        total = int(match.group(2))
        if total <= 0:
            return None
        description = match.group(3).strip().lower()
        ratio = min(1.0, max(0.0, current / total))
        stage = None
        for patterns, start, end in self._STAGES:
            if any(pattern in description for pattern in patterns):
                stage = (start, end)
                self._active_stage = stage
                break
        if stage is None:
            stage = self._active_stage
        if stage is None:
            return None
        candidate = map_progress(stage[0], stage[1], ratio)
        self._progress = max(self._progress, candidate)
        return self._progress

def format_bytes(value: float) -> str:
    amount = max(0.0, float(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024.0 or candidate == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{int(amount)} {unit}"
    if amount >= 100:
        return f"{amount:.0f} {unit}"
    if amount >= 10:
        return f"{amount:.1f} {unit}"
    return f"{amount:.2f} {unit}"


def format_transfer_status(snapshot: TransferSnapshot, label: str) -> str:
    text = f"{label}: {format_bytes(snapshot.downloaded_bytes)} / {format_bytes(snapshot.total_bytes)}"
    if snapshot.speed_bytes_per_second > 0:
        text += f" · {format_bytes(snapshot.speed_bytes_per_second)}/s"
    if snapshot.active_packages:
        visible = ", ".join(snapshot.active_packages[:2])
        if len(snapshot.active_packages) > 2:
            visible += f" +{len(snapshot.active_packages) - 2}"
        text += f" · {visible}"
    return text


class RepositoryRefreshSnapshot(NamedTuple):
    transferred_bytes: int
    completed_repositories: int
    total_repositories: int
    speed_bytes_per_second: float
    ratio: float
    active_repositories: tuple[str, ...]


class RepositoryDatabaseSampler:
    """Observe pacman sync database files during a repository refresh."""

    def __init__(
        self,
        sync_directory: os.PathLike[str] | str,
        repositories: Sequence[str],
        *,
        smoothing: float = 0.35,
    ) -> None:
        self._sync_directory = Path(sync_directory)
        self._repositories = tuple(dict.fromkeys(str(item) for item in repositories if str(item)))
        self._smoothing = min(1.0, max(0.0, float(smoothing)))
        self._baseline = {repository: self._final_signature(repository) for repository in self._repositories}
        self._estimated = {
            repository: max(1, signature[1] if signature is not None else 1)
            for repository, signature in self._baseline.items()
        }
        self._observed = {repository: 0 for repository in self._repositories}
        self._ratio = 0.0
        self._previous_time: float | None = None
        self._previous_bytes = 0
        self._speed = 0.0

    def _final_signature(self, repository: str) -> tuple[int, int] | None:
        path = self._sync_directory / f"{repository}.db"
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _partial_bytes(self, repository: str) -> int:
        total = 0
        for suffix in (".db.part", ".db.sig.part"):
            try:
                total += (self._sync_directory / f"{repository}{suffix}").stat().st_size
            except OSError:
                pass
        return total

    def sample(self, *, now: float | None = None) -> RepositoryRefreshSnapshot:
        timestamp = time.monotonic() if now is None else float(now)
        completed = 0
        active: list[str] = []
        weighted_progress = 0
        for repository in self._repositories:
            partial_bytes = self._partial_bytes(repository)
            current_signature = self._final_signature(repository)
            changed = current_signature is not None and current_signature != self._baseline[repository]
            if current_signature is not None:
                self._estimated[repository] = max(self._estimated[repository], current_signature[1])
            if partial_bytes > 0:
                self._estimated[repository] = max(self._estimated[repository], partial_bytes)
            if changed:
                completed += 1
                observed = current_signature[1]
                weighted_progress += self._estimated[repository]
            else:
                observed = partial_bytes
                weighted_progress += min(partial_bytes, self._estimated[repository])
                if partial_bytes > 0:
                    active.append(repository)
            self._observed[repository] = max(self._observed[repository], observed)

        transferred = sum(self._observed.values())
        if self._previous_time is not None and timestamp > self._previous_time:
            delta = transferred - self._previous_bytes
            raw_speed = max(0.0, delta / (timestamp - self._previous_time))
            if self._speed <= 0.0:
                self._speed = raw_speed
            else:
                self._speed = self._smoothing * raw_speed + (1.0 - self._smoothing) * self._speed
        self._previous_time = timestamp
        self._previous_bytes = transferred

        total = len(self._repositories)
        estimated_total = sum(self._estimated.values())
        candidate_ratio = weighted_progress / estimated_total if estimated_total > 0 else 1.0
        if completed == total:
            candidate_ratio = 1.0
        self._ratio = max(self._ratio, min(1.0, max(0.0, candidate_ratio)))
        return RepositoryRefreshSnapshot(
            transferred_bytes=transferred,
            completed_repositories=completed,
            total_repositories=total,
            speed_bytes_per_second=self._speed,
            ratio=self._ratio,
            active_repositories=tuple(active),
        )


def format_repository_refresh_status(snapshot: RepositoryRefreshSnapshot, label: str) -> str:
    text = f"{label}: {snapshot.completed_repositories} / {snapshot.total_repositories}"
    if snapshot.transferred_bytes > 0:
        text += f" · {format_bytes(snapshot.transferred_bytes)}"
    if snapshot.speed_bytes_per_second > 0:
        text += f" · {format_bytes(snapshot.speed_bytes_per_second)}/s"
    if snapshot.active_repositories:
        text += " · " + ", ".join(snapshot.active_repositories[:2])
    return text


class TerminalFrameDecoder:
    """Incrementally split byte output on either CR or LF without losing chunks."""

    def __init__(self, encoding: str = "utf-8") -> None:
        self._buffer = bytearray()
        self._encoding = encoding

    def feed(self, data: bytes) -> list[str]:
        self._buffer.extend(data)
        return self._drain(flush=False)

    def finish(self) -> list[str]:
        return self._drain(flush=True)

    def _drain(self, *, flush: bool) -> list[str]:
        frames: list[str] = []
        while True:
            positions = [index for index in (self._buffer.find(b"\r"), self._buffer.find(b"\n")) if index >= 0]
            if not positions:
                break
            separator = min(positions)
            frame = bytes(self._buffer[:separator])
            remove = separator + 1
            if self._buffer[separator : separator + 2] == b"\r\n":
                remove += 1
            del self._buffer[:remove]
            if frame:
                frames.append(frame.decode(self._encoding, errors="replace"))
        if flush and self._buffer:
            frames.append(bytes(self._buffer).decode(self._encoding, errors="replace"))
            self._buffer.clear()
        return frames


class TransferSampler:
    """Calculate aggregate package bytes and EWMA speed from pacman cache files."""

    def __init__(
        self,
        plan: DownloadPlan,
        cache_directories: Sequence[os.PathLike[str] | str],
        *,
        smoothing: float = 0.35,
    ) -> None:
        self._plan = plan
        self._cache_directories = tuple(Path(path) for path in cache_directories)
        self._smoothing = min(1.0, max(0.0, float(smoothing)))
        self._previous_time: float | None = None
        self._previous_bytes = 0
        self._speed = 0.0

    def _file_bytes(self, download: PlannedDownload) -> tuple[int, bool]:
        observed = 0
        active = False
        for cache in self._cache_directories:
            final_path = cache / download.filename
            try:
                if final_path.is_file():
                    return download.size, False
            except OSError:
                pass
            partial_path = cache / f"{download.filename}.part"
            try:
                if partial_path.is_file():
                    observed = max(observed, min(download.size, partial_path.stat().st_size))
                    active = True
            except OSError:
                pass
        return observed, active

    def sample(self, *, now: float | None = None) -> TransferSnapshot:
        timestamp = time.monotonic() if now is None else float(now)
        downloaded = 0
        active: list[str] = []
        for item in self._plan.downloads:
            item_bytes, is_active = self._file_bytes(item)
            downloaded += item_bytes
            if is_active and item_bytes < item.size:
                active.append(item.name)
        downloaded = min(self._plan.total_bytes, downloaded)

        if self._previous_time is not None and timestamp > self._previous_time:
            delta = downloaded - self._previous_bytes
            raw_speed = max(0.0, delta / (timestamp - self._previous_time))
            if self._speed <= 0.0:
                self._speed = raw_speed
            else:
                self._speed = self._smoothing * raw_speed + (1.0 - self._smoothing) * self._speed
        self._previous_time = timestamp
        self._previous_bytes = downloaded

        ratio = downloaded / self._plan.total_bytes if self._plan.total_bytes > 0 else 1.0
        return TransferSnapshot(
            downloaded_bytes=downloaded,
            total_bytes=self._plan.total_bytes,
            speed_bytes_per_second=self._speed,
            ratio=min(1.0, max(0.0, ratio)),
            active_packages=tuple(active),
        )
