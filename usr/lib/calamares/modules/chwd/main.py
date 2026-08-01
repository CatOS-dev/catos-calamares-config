#!/usr/bin/env python3

import os
import subprocess
import time

import libcalamares
from libcalamares.utils import gettext_path, gettext_languages

import gettext

_translation = gettext.translation("calamares-python",
                                   localedir=gettext_path(),
                                   languages=gettext_languages(),
                                   fallback=True)
_ = _translation.gettext
_n = _translation.ngettext

status_update_time = 0


class HostError(Exception):
    """Exception raised when the call to returns a non-zero exit code

    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message


def pretty_name():
    return _("Installing needed drivers for CatOS...")


def line_cb(line):
    """
    Writes every line to the debug log and displays it in calamares
    :param line: The line of output text from the command
    """
    global status_update_time
    libcalamares.utils.debug("chwd: " + line.strip())
    if (time.time() - status_update_time) > 0.5:
        libcalamares.job.setprogress(0)
        status_update_time = time.time()


def run_in_host(command, line_func):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                            bufsize=1)
    for line in proc.stdout:
        if line.strip():
            line_func(line)
    proc.wait()
    if proc.returncode != 0:
        raise HostError("Failed to run chwd")


def run():
    """
    Installs hardware drivers

    """
    root_mount_point = libcalamares.globalstorage.value("rootMountPoint")

    if not root_mount_point:
        return (_("No mount point for root partition in globalstorage"),
                _('globalstorage does not contain a "rootMountPoint" key, doing nothing'))

    if not os.path.exists(root_mount_point):
        return (_("Bad mount point for root partition in globalstorage"),
                _('globalstorage["rootMountPoint"] is "{root}", which does not exist, doing nothing').format(
                    root=root_mount_point
                ))

    # run the command in chroot
    shell_command = ["arch-chroot", root_mount_point, "chwd", "--autoconfigure"]

    try:
        run_in_host(shell_command, line_cb)
    except (subprocess.CalledProcessError, HostError, OSError) as error:
        libcalamares.utils.warning(
            "chwd failed; continuing without automatic driver configuration: {!s}".format(error)
        )
        libcalamares.job.setprogress(1.0)
        return None

    libcalamares.job.setprogress(1.0)

    return None
