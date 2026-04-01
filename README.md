# CrowdSec BIRD Stream Bouncer

Language: **English** | [Русский](README.ru.md)

Custom CrowdSec remediation component that converts CrowdSec decisions into BIRD blackhole routes and distributes them over eBGP.

## Table of Contents

1. [Overview](#overview)
2. [Data Flow](#data-flow)
3. [Repository Layout](#repository-layout)
4. [Requirements](#requirements)
5. [Host Installation](#host-installation)
6. [Configuration](#configuration)
7. [Systemd Service](#systemd-service)
8. [BIRD Integration](#bird-integration)
9. [MikroTik Integration (Reference)](#mikrotik-integration-reference)
10. [Validation](#validation)
11. [Metrics](#metrics)
12. [Docker (Optional)](#docker-optional)
13. [Security Notes](#security-notes)
14. [Known Limitations](#known-limitations)
15. [Troubleshooting](#troubleshooting)

## Overview

This project implements a stream-based blackhole distribution pipeline:

- CrowdSec LAPI provides incremental updates via `GET /v1/decisions/stream`.
- The bouncer fetches, filters, and normalizes decisions.
- Dynamic route fragments are generated for BIRD (`v4` and `v6`).
- BIRD announces routes to one or many routers via eBGP.
- Router-side policy marks accepted routes as `blackhole`.

Design goals:

- Keep processing logic on Linux host.
- Keep routers focused on routing and policy enforcement.
- Avoid periodic local file-fetch workflows on router flash storage.

## Data Flow

```text
CrowdSec LAPI
   |
   |  GET /v1/decisions/stream
   v
crowdsec-bird-bouncer.py
   |
   |  writes route fragments
   v
/etc/bird/blackhole_dynamic_v4.conf
/etc/bird/blackhole_dynamic_v6.conf
   |
   |  birdc configure (or configure soft)
   v
BIRD2
   |
   |  eBGP announcements (+ optional community)
   v
Router (MikroTik example)
   |
   |  inbound routing filter => set blackhole
   v
Traffic sink / drop
```

## Repository Layout

- `crowdsec-bird-bouncer.py`: main bouncer logic.
- `crowdsec-bird-bouncer.env.example`: full environment configuration reference.
- `crowdsec-bird-bouncer.service`: systemd service template.
- `Dockerfile`: container build skeleton.
- `requirements.txt`: Python dependencies.

## Requirements

- Linux host with `python3`, `bird2`, and CrowdSec LAPI.
- BGP adjacency between BIRD host and target router(s).
- Valid CrowdSec bouncer API key (`cscli bouncers add ...`).

## Host Installation

1. Install dependencies:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-requests bird2 crowdsec
```

2. Install script:

```bash
sudo install -m 0755 crowdsec-bird-bouncer.py /usr/local/bin/crowdsec-bird-bouncer.py
```

3. Create bouncer key:

```bash
sudo cscli bouncers add bird-stream-bouncer
```

4. Install env config:

```bash
sudo install -d -m 0755 /etc/crowdsec
sudo cp crowdsec-bird-bouncer.env.example /etc/crowdsec/bird-bouncer.env
sudo chmod 600 /etc/crowdsec/bird-bouncer.env
```

5. Put real `CROWDSEC_API_KEY` into `/etc/crowdsec/bird-bouncer.env`.

## Configuration

Primary configuration file:

- `/etc/crowdsec/bird-bouncer.env`

Key parameters:

- `CROWDSEC_API_KEY`
- `CROWDSEC_BIRD_LAPI_URL`
- `CROWDSEC_BIRD_CONFIG_FILE_V4`
- `CROWDSEC_BIRD_CONFIG_FILE_V6` (set empty to disable IPv6 export)
- `CROWDSEC_BIRD_RELOAD_CMD`

Safety and scaling controls:

- Prefix guards: `CROWDSEC_BIRD_MIN_PREFIXLEN_V4`, `CROWDSEC_BIRD_MIN_PREFIXLEN_V6`, `CROWDSEC_BIRD_BLOCK_PRIVATE_PREFIXES`
- Capacity guard: `CROWDSEC_BIRD_MAX_TOTAL_PREFIXES`
- Polling behavior: `CROWDSEC_BIRD_POLL_INTERVAL`, `CROWDSEC_BIRD_REQUEST_TIMEOUT`

### Remote Bouncer Access to LAPI

If the bouncer is not running on the same host as CrowdSec, adjust CrowdSec LAPI bind/access settings in `/etc/crowdsec/config.yaml`.

Default configuration is typically loopback-only:

```yaml
api:
  server:
    listen_uri: 127.0.0.1:8080
    trusted_ips:
      - 127.0.0.1
      - ::1
```

For remote access:

1. Set `api.server.listen_uri` to an address reachable by the bouncer host (for example, a management interface IP, not necessarily `0.0.0.0`).
2. Add bouncer source IP(s) to `api.server.trusted_ips`.
3. Restart CrowdSec:

```bash
sudo systemctl restart crowdsec
```

4. Ensure network/firewall allows access to the LAPI port from bouncer host.
5. Update bouncer env:

```bash
CROWDSEC_BIRD_LAPI_URL=http://<crowdsec-host>:<lapi-port>
```

Note:

- Use the same port as `listen_uri` (for example `8088` in many default installs).
- Restrict exposure as much as possible (IP ACL + firewall).

## Systemd Service

Install and start:

```bash
sudo cp crowdsec-bird-bouncer.service /etc/systemd/system/crowdsec-bird-bouncer.service
sudo systemctl daemon-reload
sudo systemctl enable --now crowdsec-bird-bouncer.service
```

Check status:

```bash
sudo systemctl status crowdsec-bird-bouncer.service
sudo journalctl -u crowdsec-bird-bouncer.service -n 100 -f
```

## BIRD Integration

Minimum requirements:

- Generated files are included from `bird.conf`.
- Export policy allows only dynamic CrowdSec routes.
- Community tagging is done in one place only (BIRD filter or bouncer output).

### Full `bird.conf` Reference

Replace placeholders before production use.

```bird
router id 192.168.140.11;     # BIRD host Router ID
log syslog all;

protocol device {
    scan time 10;
}

protocol kernel {
    persist;
    scan time 20;
    ipv4 {
        import none;
        export all;
    };
}

protocol kernel {
    persist;
    scan time 20;
    ipv6 {
        import none;
        export all;
    };
}

protocol direct {
    interface "*";
}

protocol static blackhole_dynamic {
    ipv4;
    include "/etc/bird/blackhole_dynamic_v4.conf";
}

protocol static blackhole_dynamic_v6 {
    ipv6;
    include "/etc/bird/blackhole_dynamic_v6.conf";
}

filter export_blackhole {
    if proto = "blackhole_dynamic" || proto = "blackhole_dynamic_v6" then {
        bgp_community.add((65535,666));
        accept;
    }
    reject;
}

template bgp crowdsec_template {
    local as 65101;          # BIRD-side ASN
    hold time 240;
    keepalive time 80;

    # passive on;

    ipv4 {
        import none;
        export filter export_blackhole;
        next hop self;
    };

    ipv6 {
        import none;
        export filter export_blackhole;
        next hop self;
    };
}

protocol bgp mikrotik from crowdsec_template {
    neighbor 192.168.140.1 as 65100;  # Router peer and ASN
    # Example for multiple peers/ranges:
    # neighbor range 192.168.140.0/24, 10.10.10.0/24, 172.16.0.0/16 as 65100;
}
```

Parameter mapping:

- `router id`: BIRD host router ID.
- `local as`: ASN on BIRD side.
- `neighbor ... as`: peer address and ASN on router side.
- eBGP requires different ASNs.

Validate and apply:

```bash
sudo chown -R bird:bird /etc/bird/
sudo birdc configure check
sudo birdc configure
sudo birdc show protocols all
```

## MikroTik Integration (Reference)

MikroTik is a reference example; this bouncer is router-vendor agnostic if BGP policy is available.

### RouterOS v7 Example

```mikrotik
/routing bgp instance
add as=65100 name=bgp-instance-crowdsec router-id=192.168.140.1

/routing bgp connection
add afi=ip,ipv6 as=65100 connect=yes disabled=no \
hold-time=4m input.filter=blackhole-chain instance=bgp-instance-crowdsec \
keepalive-time=1m local.address=192.168.140.1 .role=ebgp \
name=crowdsec remote.address=192.168.140.11/32 .as=65101 routing-table=main

/routing filter rule
add chain=blackhole-chain disabled=no \
rule="if (bgp-communities includes blackhole) { set blackhole yes; set gw 0.0.0.0; accept; }"
```

Parameter mapping:

- `router-id` / `local.address`: MikroTik local address.
- `remote.address`: BIRD host address.
- `.as`: remote ASN (BIRD ASN).
- `input.filter`: policy that marks accepted routes as blackhole.

Session checks:

```mikrotik
/routing/bgp/session/print
:put [/routing/bgp/session/get [find name~"crowdsec"] prefix-count]
```

## Validation

Create test decisions:

```bash
sudo cscli decisions add --ip 1.2.3.4 --type ban --duration 1h
sudo cscli decisions add --range 4.4.4.0/24 --type ban --duration 1h
```

Delete test decisions:

```bash
sudo cscli decisions delete --ip 1.2.3.4
sudo cscli decisions delete --range 4.4.4.0/24
```

Observe:

- bouncer logs (`journalctl`)
- BIRD protocol status (`birdc show protocols all`)
- router route table and BGP prefix counters

## Metrics

The bouncer can post usage metrics to `POST /v1/usage-metrics`.

Enable in env:

- `CROWDSEC_BIRD_ENABLE_USAGE_METRICS=true`
- `CROWDSEC_BIRD_USAGE_METRICS_INTERVAL=300`

Exported metrics are component-level:

- active prefixes (`v4`, `v6`, `total`)
- decisions added/removed
- successful and failed polls
- successful and failed BIRD reloads

Router dataplane packet-drop counters are intentionally out of scope.

## Docker (Optional)

### Mode A (Recommended): Host BIRD + Containerized Bouncer

Use when BIRD already runs on host.

1. Prepare host env config:

```bash
sudo install -d -m 0755 /etc/crowdsec
sudo cp docker/recommended/bird-bouncer.env.example /etc/crowdsec/bird-bouncer.env
sudo chmod 600 /etc/crowdsec/bird-bouncer.env
```

2. Start:

```bash
docker compose -f compose.recommended.yml up -d --build
```

3. Logs:

```bash
docker compose -f compose.recommended.yml logs -f
```

This mode mounts host BIRD config/runtime paths:

- `/etc/bird`
- `/run/bird`

### Mode B (Full Routing Stack): BIRD + Bouncer in Containers

Use for lab/testing or when you want BIRD isolated from host.

1. Create env file:

```bash
cp docker/full/bird-bouncer.env.example docker/full/bird-bouncer.env
```

2. Edit:

- `CROWDSEC_API_KEY`
- `CROWDSEC_BIRD_LAPI_URL`
- BGP-related values in `docker/bird/bird.conf`

3. Start:

```bash
docker compose -f compose.full.yml up -d --build
```

4. Logs:

```bash
docker compose -f compose.full.yml logs -f
```

### Single-Image Build (if needed)

Build bouncer image only:

```bash
docker build -t crowdsec-bird-bouncer:local .
```

Run manually:

```bash
docker run --rm \
  --name crowdsec-bird-bouncer \
  --network host \
  --env-file /etc/crowdsec/bird-bouncer.env \
  -v /etc/bird:/etc/bird \
  -v /run/bird:/run/bird \
  crowdsec-bird-bouncer:local
```

Notes:

- Host-first design (`systemd` + host BIRD) is the primary deployment model.
- In container mode, bouncer still requires `birdc` access to BIRD control socket.

## Security Notes

- Keep API key only in `/etc/crowdsec/bird-bouncer.env`.
- Set strict permissions (`chmod 600`) on env file.
- Do not hardcode secrets in source code or unit files.

## Known Limitations

- No direct packet/byte drop telemetry from router dataplane.
- Reaction speed depends on stream polling interval.
- Requires stable host-to-router BGP session.
- Routing policy behavior depends on router vendor implementation.
- Docker mode is not zero-config because of BIRD control-plane coupling.

## Troubleshooting

Check bouncer status and pull activity:

```bash
sudo cscli bouncers list
sudo cscli bouncers inspect bird-stream-bouncer
```

Check service logs:

```bash
sudo systemctl status crowdsec-bird-bouncer.service
sudo journalctl -u crowdsec-bird-bouncer.service -f
```

Check BIRD:

```bash
sudo birdc show protocols all
sudo birdc configure check
```

If CrowdSec Console shows `No metrics available`:

- verify `CROWDSEC_BIRD_ENABLE_USAGE_METRICS=true`
- verify LAPI accepts `POST /v1/usage-metrics`
- check bouncer logs for metrics HTTP errors
