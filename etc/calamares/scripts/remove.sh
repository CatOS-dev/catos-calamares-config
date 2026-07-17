#!/usr/bin/env bash

set -u

_files_to_remove=(
  /usr/local/bin/choose-mirror
  /usr/local/bin/prepare-live-desktop.sh
  /usr/local/bin/removeun-online
  /usr/local/share/livecd-sound
)

_remove_pacman_package() {
  local package="$1"
  if pacman -Qq "$package" > /dev/null 2>&1; then
    pacman -Rns "$package" --noconfirm || true
  fi
}

_clean_vm_packages() {
  if pacman -Qi virtualbox-guest-utils > /dev/null 2>&1; then
    systemctl disable vboxservice.service || true
    _remove_pacman_package virtualbox-guest-utils
  fi

  if pacman -Qi virtualbox-guest-utils-nox > /dev/null 2>&1; then
    systemctl disable vboxservice.service || true
    _remove_pacman_package virtualbox-guest-utils-nox
  fi

  rm -f /etc/xdg/autostart/vmware-user.desktop
  if pacman -Qi open-vm-tools > /dev/null 2>&1; then
    systemctl disable vmtoolsd.service || true
    _remove_pacman_package open-vm-tools
  fi
  rm -f /etc/systemd/system/multi-user.target.wants/vmtoolsd.service

  if pacman -Qi qemu-guest-agent > /dev/null 2>&1; then
    systemctl disable qemu-guest-agent.service || true
    _remove_pacman_package qemu-guest-agent
  fi
}

_clean_packages() {
  local packages_to_remove=(
    gparted
    catos-calamares
    catos-calamares-config
    edk2-shell
    gpart
    arch-install-scripts
    squashfs-tools
    syslinux
    clonezilla
    memtest86+
    memtest86+-efi
    mkinitcpio-archiso
    tcpdump
  )

  if ! chwd --is_nvidia_card | grep -q 'NVIDIA card found!'; then
    echo "No NVIDIA card detected. Removing NVIDIA-only packages"
    packages_to_remove+=(nvidia-open nvidia-utils)
    _files_to_remove+=(/etc/mkinitcpio.conf.d/10-nvidia.conf)
  fi

  local package
  for package in "${packages_to_remove[@]}"; do
    _remove_pacman_package "$package"
  done
}

_clean_files() {
  local path
  for path in "${_files_to_remove[@]}"; do
    rm -rf "$path" || true
  done
}

if [[ $(systemd-detect-virt 2>/dev/null || true) == none ]]; then
  _clean_vm_packages
fi

_clean_packages
_clean_files
