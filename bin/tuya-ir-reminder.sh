#!/bin/bash
set -euo pipefail

/Users/ultra/.files/bin/tg --tag report --title "Напоминание: прошивка ИК-модуля" "Напоминание: сегодня прошивка ИК-модуля GWW-RC03 на ESPHome (tuya-cloudcutter). Нужно твоё участие: sudo-пароль + переключение питания устройства по инструкции. Детали — issue #9 в esphome-ir (https://github.com/alex-mextner/esphome-ir/issues/9)."

launchctl bootout "gui/$(id -u)/com.alex.tuya-ir-reminder" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.alex.tuya-ir-reminder.plist"
