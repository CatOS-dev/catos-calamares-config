#!/usr/bin/env python3
"""Stable recovery.failureContext construction for CatOS installer modules."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

SCHEMA_VERSION = 1


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def classify_failure(
    *,
    source: str,
    stage: str,
    details: str = "",
    output: str = "",
) -> str:
    """Return a stable Recovery category, with root causes before components."""
    text = "\n".join((stage, details, output)).lower()

    if _contains_any(text, ("read-only file system", "filesystem is read-only", "mounted read-only")):
        return "read-only-filesystem"
    if _contains_any(text, ("no space left on device", "disk quota exceeded", "not enough free disk space")):
        return "storage-full"
    if _contains_any(
        text,
        (
            "certificate is not yet valid",
            "certificate has expired",
            "signature from the future",
            "key is not yet valid",
            "system clock",
        ),
    ):
        return "clock-invalid"
    if "keyring" in stage or _contains_any(
        text,
        (
            "unknown trust",
            "unknown public key",
            "could not be looked up remotely",
            "required key missing",
            "keyring is not writable",
            "keyring file is missing",
            "invalid or corrupted package (pgp signature)",
            "invalid or corrupted database (pgp signature)",
            "signature is unknown trust",
            "signature verification failed",
            "gpgme error: no data",
            "pacman-key",
        ),
    ):
        return "keyring-failure"
    if _contains_any(text, ("unable to lock database", "failed to init transaction (unable to lock", "db.lck")):
        return "package-database-locked"
    if _contains_any(text, ("could not resolve host", "temporary failure in name resolution", "name or service not known")):
        return "dns-failure"
    if _contains_any(text, ("network is unreachable", "no route to host", "network unreachable")):
        return "network-unavailable"
    if _contains_any(
        text,
        (
            "proxy authentication required",
            "407 proxy",
            "requested url returned error: 403",
            "http 403",
            "captive portal",
            "tls certificate verify failed",
            "ssl certificate problem: unable to get local issuer",
        ),
    ):
        return "network-authentication"
    if stage == "repository-configuration" or _contains_any(
        text,
        (
            "repository configuration missing",
            "invalid repository selection",
            "unsupported repository selection",
            "required pacman configuration does not exist",
            "mirrorlist is missing",
        ),
    ):
        return "repository-configuration"
    if _contains_any(text, ("requested url returned error: 404", "http 404", " 404 not found")) or (
        "failed retrieving file" in text and "not found" in text
    ):
        return "mirror-out-of-sync"
    if _contains_any(
        text,
        (
            "requested url returned error: 429",
            "requested url returned error: 500",
            "requested url returned error: 502",
            "requested url returned error: 503",
            "requested url returned error: 504",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "service unavailable",
            "too many requests",
            "connection refused",
            "failed to connect",
            "could not connect to server",
        ),
    ):
        return "mirror-unavailable"
    if _contains_any(
        text,
        (
            "operation too slow",
            "connection timed out",
            "connection timeout",
            "download timeout",
            "less than 1 bytes/sec",
            "connection reset by peer",
            "transfer closed with outstanding",
        ),
    ):
        return "network-timeout"
    if _contains_any(
        text,
        (
            "checksum mismatch",
            "invalid or corrupted package",
            "unexpected end of file",
            "unexpected eof",
            "zstd decompression error",
            "failed to commit transaction (invalid or corrupted",
        ),
    ):
        return "corrupt-download"
    if _contains_any(
        text,
        (
            "could not satisfy dependencies",
            "conflicting dependencies",
            "failed to prepare transaction",
            "target not found",
            "required installation packages are unavailable",
            "missing required packages",
        ),
    ):
        return "dependency-conflict"

    if source == "bootloadu":
        return "bootloader-failure"
    if source == "umount":
        return "cleanup-failure"
    return "unknown"


def command_text(command: Sequence[Any] | str | None) -> str:
    if isinstance(command, str):
        return command
    if command:
        return " ".join(str(part) for part in command)
    return ""


def build_failure_context(
    *,
    source: str,
    stage: str,
    summary: str,
    details: str,
    command: Sequence[Any] | str | None = None,
    exit_code: int | None = None,
    output: str = "",
    provider: str = "",
    category: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    resolved_category = category or classify_failure(
        source=source,
        stage=stage,
        details=details,
        output=output,
    )
    context: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "stage": stage,
        "category": resolved_category,
        "reasonCode": reason_code or f"{source}.{stage}.{resolved_category}",
        "summary": str(summary),
        "details": str(details),
    }
    rendered_command = command_text(command)
    if rendered_command:
        context["command"] = rendered_command
    if exit_code is not None:
        context["exitCode"] = int(exit_code)
    if output:
        context["output"] = str(output)
    if provider:
        context["provider"] = provider
    return context
