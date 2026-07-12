#!/usr/bin/env bash
set -euo pipefail

CONF_DIR=/etc/chrony/conf.d
CONF_FILE="$CONF_DIR/reticulumpi-gps.conf"

die() { echo "Error: $*" >&2; exit 64; }
numeric_re='^-?[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?$'

[ "$(id -u)" -eq 0 ] || die "chrony_helper.sh must run as root"

case "${1:-}" in
    configure)
        [ "$#" -eq 7 ] || die "configure requires shm precision offset delay pps-device pps-precision"
        shm="$2"
        precision="$3"
        offset="$4"
        delay="$5"
        pps_device="$6"
        pps_precision="$7"
        if ! [[ "$shm" =~ ^[0-9]+$ ]] || [ "$shm" -gt 15 ]; then
            die "invalid SHM segment"
        fi
        [[ "$precision" =~ $numeric_re ]] || die "invalid precision"
        [[ "$offset" =~ $numeric_re ]] || die "invalid offset"
        [[ "$delay" =~ $numeric_re ]] || die "invalid delay"
        [[ "$pps_precision" =~ $numeric_re ]] || die "invalid PPS precision"
        if [ "$pps_device" != "-" ]; then
            [[ "$pps_device" =~ ^/dev/pps[0-9]+$ ]] || die "invalid PPS device"
        fi
        install -d -m 0755 "$CONF_DIR"
        tmp="$(mktemp "$CONF_DIR/.reticulumpi-gps.XXXXXX")"
        trap 'rm -f "$tmp"' EXIT
        {
            echo '# ReticulumPi GPS refclock — managed by ntp_server plugin'
            printf 'refclock SHM %s refid GPS precision %s offset %s delay %s\n' \
                "$shm" "$precision" "$offset" "$delay"
            if [ "$pps_device" != "-" ]; then
                printf 'refclock PPS %s refid PPS precision %s lock GPS\n' \
                    "$pps_device" "$pps_precision"
            fi
        } > "$tmp"
        chmod 0644 "$tmp"
        mv -f "$tmp" "$CONF_FILE"
        trap - EXIT
        systemctl restart chrony.service
        echo configured
        ;;
    remove)
        [ "$#" -eq 1 ] || die "remove accepts no arguments"
        rm -f "$CONF_FILE"
        systemctl restart chrony.service
        echo removed
        ;;
    online)
        [ "$#" -eq 1 ] || die "online accepts no arguments"
        chronyc online
        ;;
    *)
        die "usage: chrony_helper.sh {configure|remove|online}"
        ;;
esac
