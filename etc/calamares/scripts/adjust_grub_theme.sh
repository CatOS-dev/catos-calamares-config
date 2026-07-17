#!/usr/bin/env bash

resolution=$(xrandr --current 2>/dev/null | awk '/\*/ { print $1; exit }')
if [[ ! $resolution =~ ^([0-9]+)x([0-9]+)$ ]]; then
    echo "无法检测显示分辨率，保留默认的 1080p GRUB 主题"
    exit 0
fi

width=${BASH_REMATCH[1]}
height=${BASH_REMATCH[2]}

if (( width >= 3840 && height >= 2160 )); then
    echo "当前分辨率：${width}x${height}，使用 4K GRUB 主题"
    sed -i "s/resolution='1080p'/resolution='4k'/g" /etc/calamares/scripts/adjust_grub_theme_after.sh
elif (( width >= 2560 && height >= 1440 )); then
    echo "当前分辨率：${width}x${height}，使用 2K GRUB 主题"
    sed -i "s/resolution='1080p'/resolution='2k'/g" /etc/calamares/scripts/adjust_grub_theme_after.sh
else
    echo "当前分辨率：${width}x${height}，使用默认的 1080p GRUB 主题"
fi
