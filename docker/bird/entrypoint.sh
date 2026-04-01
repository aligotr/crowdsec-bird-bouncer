#!/bin/sh
set -eu

mkdir -p /run/bird /etc/bird/dynamic
touch /etc/bird/dynamic/blackhole_dynamic_v4.conf
touch /etc/bird/dynamic/blackhole_dynamic_v6.conf

exec /usr/sbin/bird -f -c /etc/bird/bird.conf -s /run/bird/bird.ctl

