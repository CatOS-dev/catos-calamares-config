# -*- coding: utf-8 -*-

import libcalamares


def _target_env_capture_lines(command):
    """
    Run command in target env and capture stdout lines.
    """
    lines = []

    def cb(line: str):
        lines.append(line)

    libcalamares.utils.target_env_process_output(command, cb)
    return [l.rstrip("\n") for l in lines if l]


def build_repo_index():
    """
    Build (packages_set, groups_set) from pacman sync db in target env.
    """
    pkgs = set(_target_env_capture_lines(["pacman", "-Slq"]))
    groups = set(_target_env_capture_lines(["pacman", "-Sgq"]))
    libcalamares.utils.debug(f"pacman repo index: {len(pkgs)} packages, {len(groups)} groups")
    return pkgs, groups


def _pkg_name_of(item):
    if isinstance(item, str):
        return item
    return item.get("package")


def filter_operation_list(action_key, items, repo_pkgs, repo_groups):
    """
    Filter one list: keep only existing packages or groups.
    Drops missing entries; logs warnings.
    """
    kept = []
    for item in items:
        name = _pkg_name_of(item)
        if not name:
            libcalamares.utils.warning(f"{action_key}: empty package entry ignored: {item!r}")
            continue

        if name in repo_pkgs or name in repo_groups:
            kept.append(item)
        else:
            libcalamares.utils.warning(f"{action_key}: dropping missing package/group: {name}")
            libcalamares.utils.debug(f"{action_key}: dropped entry: {item!r}")

    return kept


def preprocess_operations(operations, subst_locale_fn, repo_pkgs, repo_groups):
    """
    Apply subst_locale + existence filter.
    Returns: (new_operations, filtered_total_count)
    """
    new_ops = []
    total = 0

    filter_keys = {"install", "try_install", "remove", "try_remove"}

    for op in operations:
        new_op = {}
        for key, raw_list in op.items():
            if key == "source":
                new_op[key] = raw_list
                continue

            localized = subst_locale_fn(raw_list)

            if key in filter_keys:
                filtered = filter_operation_list(key, localized, repo_pkgs, repo_groups)
                new_op[key] = filtered
                total += len(filtered)
            elif key == "localInstall":
                new_op[key] = localized
                total += len(localized)
            else:
                new_op[key] = localized

        # drop completely-empty ops (optional)
        if any(isinstance(v, list) and len(v) for v in new_op.values()):
            new_ops.append(new_op)

    return new_ops, total
