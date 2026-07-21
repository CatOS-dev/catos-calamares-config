#!/usr/bin/env bash

set -u

_files_to_remove=(
  /usr/local/bin/choose-mirror
  /usr/local/bin/prepare-live-desktop.sh
  /usr/local/bin/removeun-online
  /usr/local/share/livecd-sound
)

_remove_pacman_packages() {
  local -a installed_packages=()
  local package

  for package in "$@"; do
    if pacman -Qq "$package" > /dev/null 2>&1; then
      installed_packages+=("$package")
    fi
  done

  if (( ${#installed_packages[@]} == 0 )); then
    return 0
  fi

  if ! pacman -Rns --noconfirm "${installed_packages[@]}"; then
    printf 'warning: failed to remove packages: %s\n' "${installed_packages[*]}" >&2
  fi

  return 0
}

_remove_pacman_packages_individually() {
  local package

  for package in "$@"; do
    _remove_pacman_packages "$package"
  done
}

_clean_vm_packages() {
  if pacman -Qq virtualbox-guest-utils > /dev/null 2>&1; then
    systemctl disable vboxservice.service || true
    _remove_pacman_packages virtualbox-guest-utils
  fi

  if pacman -Qq virtualbox-guest-utils-nox > /dev/null 2>&1; then
    systemctl disable vboxservice.service || true
    _remove_pacman_packages virtualbox-guest-utils-nox
  fi

  rm -f /etc/xdg/autostart/vmware-user.desktop
  if pacman -Qq open-vm-tools > /dev/null 2>&1; then
    systemctl disable vmtoolsd.service || true
    _remove_pacman_packages open-vm-tools
  fi
  rm -f /etc/systemd/system/multi-user.target.wants/vmtoolsd.service

  if pacman -Qq qemu-guest-agent > /dev/null 2>&1; then
    systemctl disable qemu-guest-agent.service || true
    _remove_pacman_packages qemu-guest-agent
  fi
}

_clean_live_packages() {
  local -a live_packages=(
    gparted
    edk2-shell
    gpart
    arch-install-scripts
    syslinux
    clonezilla
    memtest86+
    memtest86+-efi
    mkinitcpio-archiso
    tcpdump
  )
  local nvidia_probe

  if ! command -v chwd > /dev/null 2>&1; then
    echo "warning: chwd is unavailable; keeping NVIDIA packages" >&2
  elif nvidia_probe=$(chwd --is_nvidia_card 2>/dev/null); then
    if ! grep -q 'NVIDIA card found!' <<< "$nvidia_probe"; then
      echo "No NVIDIA card detected. Removing NVIDIA-only packages"
      live_packages+=(nvidia-open nvidia-utils)
      _files_to_remove+=(/etc/mkinitcpio.conf.d/10-nvidia.conf)
    fi
  else
    echo "warning: NVIDIA detection failed; keeping NVIDIA packages" >&2
  fi

  _remove_pacman_packages_individually "${live_packages[@]}"
}

_clean_files() {
  local path

  for path in "${_files_to_remove[@]}"; do
    rm -rf "$path" || true
  done
}

_clean_installer_packages() {
  # Remove the configuration package and Calamares in one transaction so
  # dependency ordering cannot leave the installer package behind. Pacman
  # recursively removes dependencies that are no longer required and were
  # installed as dependencies.
  _remove_pacman_packages catos-calamares-config catos-calamares

  # squashfs-tools is still an explicit ISO profile package, so pacman -Rns on
  # Calamares cannot remove it automatically. No retained profile package
  # requires it, therefore remove it only after the installer transaction.
  _remove_pacman_packages squashfs-tools
}

if [[ $(systemd-detect-virt 2>/dev/null || true) == none ]]; then
  _clean_vm_packages
fi

_clean_live_packages
_clean_files
_clean_installer_packages
