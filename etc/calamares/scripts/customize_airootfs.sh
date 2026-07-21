#!/usr/bin/env bash

## Script to perform several important tasks before `mkarchcraftiso` create filesystem image.

set -e -u

## /etc/issue
rm /usr/share/pixmaps/archlinux-logo.png
ln -s /usr/share/pixmaps/catos-logo.png /usr/share/pixmaps/archlinux-logo.png

rm /usr/share/pixmaps/archlinux-logo.svg
ln -s /usr/share/pixmaps/catos-logo.svg /usr/share/pixmaps/archlinux-logo.svg

rm /usr/share/pixmaps/archlinux-logo-text.svg
ln -s /usr/share/pixmaps/logo-text.svg /usr/share/pixmaps/archlinux-logo-text.svg

rm /usr/share/pixmaps/archlinux-logo-text-dark.svg
ln -s /usr/share/pixmaps/logo-text-dark.svg /usr/share/pixmaps/archlinux-logo-text-dark.svg

## -------------------------------------------------------------- ##

## -------------------------------------------------------------- ##

## /etc/motd
cat > "/etc/motd" <<- _EOF_
_EOF_

## -------------------------------------------------------------- ##

## -------------------------------------------------------------- ##
## 更换国内源
echo 'Server = https://mirrors.ustc.edu.cn/archlinux/$repo/os/$arch' > /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.tuna.tsinghua.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.cernet.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.bfsu.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.aliyun.com/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.bfsu.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.xjtu.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist
echo 'Server = https://mirrors.shanghaitech.edu.cn/archlinux/$repo/os/$arch' >> /etc/pacman.d/mirrorlist

##fcitx5
echo 'ELECTRON_OZONE_PLATFORM_HINT=auto' >> /etc/environment
echo 'XMODIFIERS=@im=fcitx' >> /etc/environment