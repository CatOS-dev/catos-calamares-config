#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time


GENERATED_DIR = Path("/run/calamares/cachyos")
PACMAN_CONFIG = GENERATED_DIR / "pacman.conf"
TARGET_PACMAN_CONFIG = GENERATED_DIR / "target-pacman.conf"
MIRRORLIST_DIR = GENERATED_DIR / "pacman.d"
SOURCE_PACMAN_CONFIG = Path("/etc/pacman.conf")
CACHYOS_FINGERPRINT = "882DCFE48E2051D48E2562ABF3B607488DB35A47"
KEYSERVER = "hkps://keyserver.ubuntu.com"
MANAGED_BEGIN = "# BEGIN CATOS CACHYOS REPOSITORIES"
MANAGED_END = "# END CATOS CACHYOS REPOSITORIES"
SUPPORTED_ARCHITECTURES = frozenset({"v3", "v4", "znver4"})


def select_architecture(gcc_march: str, loader_help: str) -> str:
    march = (gcc_march or "").strip().lower()
    help_text = loader_help or ""

    if march in {"znver4", "znver5"} and "x86-64-v4 (supported" in help_text:
        return "znver4"
    if "x86-64-v4 (supported" in help_text:
        return "v4"
    if "x86-64-v3 (supported" in help_text:
        return "v3"
    return "x86_64"


def supports_optimized_repositories(architecture: str) -> bool:
    return architecture in SUPPORTED_ARCHITECTURES


def require_supported_architecture(architecture: str) -> None:
    if not supports_optimized_repositories(architecture):
        raise RuntimeError(
            "CachyOS optimized repositories require an x86-64-v3 capable CPU"
        )


def repository_names(
    architecture: str,
    include_base_repository: bool = False,
) -> list[str]:
    optimized = {
        "v3": ["cachyos-v3", "cachyos-core-v3", "cachyos-extra-v3"],
        "v4": ["cachyos-v4", "cachyos-core-v4", "cachyos-extra-v4"],
        "znver4": [
            "cachyos-znver4",
            "cachyos-core-znver4",
            "cachyos-extra-znver4",
        ],
    }
    repositories = list(optimized.get(architecture, []))
    if include_base_repository:
        repositories.append("cachyos")
    return repositories


def mirrorlist_for(repo_arch: str) -> str:
    return (
        f"Server = https://mirror.nju.edu.cn/cachyos/repo/{repo_arch}/$repo\n"
        f"Server = https://mirrors.ustc.edu.cn/cachyos/repo/{repo_arch}/$repo\n"
    )


def render_mirrorlists() -> dict[str, str]:
    return {
        "cachyos-mirrorlist": mirrorlist_for("x86_64"),
        "cachyos-v3-mirrorlist": mirrorlist_for("x86_64_v3"),
        "cachyos-v4-mirrorlist": mirrorlist_for("x86_64_v4"),
    }


def mirrorlist_name(architecture: str) -> str:
    if architecture == "v3":
        return "cachyos-v3-mirrorlist"
    if architecture in {"v4", "znver4"}:
        return "cachyos-v4-mirrorlist"
    return "cachyos-mirrorlist"


def pacman_architectures(architecture: str) -> str:
    if architecture == "v3":
        return "x86_64 x86_64_v3"
    if architecture in {"v4", "znver4"}:
        return "x86_64 x86_64_v4"
    return "x86_64"


def render_repository_block(
    architecture: str,
    mirrorlist_dir: str | Path = Path("/etc/pacman.d"),
    include_base_repository: bool = False,
) -> str:
    mirrorlist_root = Path(mirrorlist_dir)
    optimized_mirrorlist = mirrorlist_name(architecture)
    sections = []
    for repository in repository_names(architecture, include_base_repository):
        selected_mirrorlist = (
            "cachyos-mirrorlist" if repository == "cachyos" else optimized_mirrorlist
        )
        sections.append(
            f"[{repository}]\n"
            f"Include = {mirrorlist_root / selected_mirrorlist}"
        )
    return f"{MANAGED_BEGIN}\n" + "\n\n".join(sections) + f"\n{MANAGED_END}"


def render_pacman_config(
    base_config: str,
    architecture: str,
    mirrorlist_dir: str | Path = Path("/etc/pacman.d"),
    include_base_repository: bool = False,
) -> str:
    managed_pattern = re.compile(
        rf"(?ms)^\s*{re.escape(MANAGED_BEGIN)}.*?{re.escape(MANAGED_END)}\s*\n?"
    )
    cleaned = managed_pattern.sub("", base_config)
    architecture_line = f"Architecture = {pacman_architectures(architecture)}"
    cleaned, replacements = re.subn(
        r"(?m)^\s*Architecture\s*=.*$",
        architecture_line,
        cleaned,
        count=1,
    )
    if replacements == 0:
        cleaned, replacements = re.subn(
            r"(?m)^\[options\]\s*$",
            f"[options]\n{architecture_line}",
            cleaned,
            count=1,
        )
    if replacements == 0:
        raise ValueError("pacman.conf has no [options] section")
    anchors = [
        match.start()
        for match in re.finditer(r"(?m)^\[(?:core|extra|multilib)\]\s*$", cleaned)
    ]
    if not anchors:
        raise ValueError("pacman.conf has no active Arch Linux repository section")

    position = min(anchors)
    before = cleaned[:position].rstrip()
    after = cleaned[position:].lstrip()
    return (
        f"{before}\n\n"
        f"{render_repository_block(architecture, mirrorlist_dir, include_base_repository)}\n\n"
        f"{after}"
    )


def _capture(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return ""
    return result.stdout or ""


def _gcc_march() -> str:
    output = _capture(["gcc", "-march=native", "-Q", "--help=target"])
    match = re.search(r"(?m)^\s*-march=\s*(\S+)\s*$", output)
    return match.group(1) if match else ""


def _loader_help() -> str:
    candidates = (
        Path("/lib/ld-linux-x86-64.so.2"),
        Path("/lib64/ld-linux-x86-64.so.2"),
        Path("/usr/lib/ld-linux-x86-64.so.2"),
    )
    for loader in candidates:
        if loader.exists():
            return _capture([str(loader), "--help"])
    return ""


def detect_architecture() -> str:
    return select_architecture(_gcc_march(), _loader_help())


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def write_repository_configuration(architecture: str) -> None:
    base_config = SOURCE_PACMAN_CONFIG.read_text(encoding="utf-8")
    _write_text(
        PACMAN_CONFIG,
        render_pacman_config(base_config, architecture, MIRRORLIST_DIR),
    )
    _write_text(
        TARGET_PACMAN_CONFIG,
        render_pacman_config(
            base_config,
            architecture,
            Path("/etc/pacman.d"),
            include_base_repository=True,
        ),
    )
    for filename, content in render_mirrorlists().items():
        _write_text(MIRRORLIST_DIR / filename, content)
    _write_text(GENERATED_DIR / "architecture", architecture + "\n")


def trust_cachyos_key() -> None:
    receive_command = [
        "pacman-key",
        "--keyserver",
        KEYSERVER,
        "--recv-keys",
        CACHYOS_FINGERPRINT,
    ]
    sign_command = ["pacman-key", "--lsign-key", CACHYOS_FINGERPRINT]
    last_error = None
    for attempt in range(1, 4):
        try:
            subprocess.run(receive_command, check=True)
            subprocess.run(sign_command, check=True)
            return
        except subprocess.CalledProcessError as error:
            last_error = error
            if attempt < 3:
                time.sleep(2)
    if last_error is not None:
        raise last_error


def main() -> int:
    architecture = detect_architecture()
    print(f"Detected CachyOS repository architecture: {architecture}")
    try:
        require_supported_architecture(architecture)
    except RuntimeError as error:
        print(error)
        return 1
    print(
        "CachyOS pacstrap repositories: "
        + ", ".join(repository_names(architecture))
    )
    print(
        "CachyOS target repositories: "
        + ", ".join(repository_names(architecture, include_base_repository=True))
    )
    write_repository_configuration(architecture)
    trust_cachyos_key()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
