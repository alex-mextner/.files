#!/bin/bash

set -e

INHIBIT_COOKIE=""

cleanup() {
    if [ -n "$INHIBIT_COOKIE" ]; then
        dbus-send --session --dest=org.gnome.ScreenSaver --type=method_call \
            /org/gnome/ScreenSaver org.gnome.ScreenSaver.UnInhibit uint32:"$INHIBIT_COOKIE"
    fi
    exit 0
}
trap cleanup EXIT INT TERM

playerctl --follow --player="%any" metadata --format '{{ status }}' 2>/dev/null | while read -r status; do
    case "$status" in
        Playing)
            if [ -z "$INHIBIT_COOKIE" ]; then
                COOKIE=$(dbus-send --session --dest=org.gnome.ScreenSaver --type=method_call \
                    --print-reply /org/gnome/ScreenSaver org.gnome.ScreenSaver.Inhibit \
                    string:"media-inhibit" string:"video playback" 2>/dev/null | awk '{print $2}')
                INHIBIT_COOKIE="$COOKIE"
            fi
            ;;
        Paused|Stopped)
            if [ -n "$INHIBIT_COOKIE" ]; then
                dbus-send --session --dest=org.gnome.ScreenSaver --type=method_call \
                    /org/gnome/ScreenSaver org.gnome.ScreenSaver.UnInhibit uint32:"$INHIBIT_COOKIE"
                INHIBIT_COOKIE=""
            fi
            ;;
    esac
done
