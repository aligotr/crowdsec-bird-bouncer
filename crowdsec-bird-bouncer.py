#!/usr/bin/env python3

import requests
import json
import time
import subprocess
import sys
import ipaddress
import fcntl
import os
import shlex
import platform

# ====================== НАСТРОЙКИ ИЗ ENV ======================
# Все параметры берутся из env с дефолтами. Рекомендуемый путь env-файла:
#   /etc/crowdsec/bird-bouncer.env
# Загружай его через EnvironmentFile= в systemd unit.
# Подробные подсказки и примеры значений см. в crowdsec-bird-bouncer.env.example

def env_str(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()

def env_int(name, default):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        print(
            f"[{time.strftime('%H:%M:%S')}] ВНИМАНИЕ: {name}='{value}' не число, использую {default}",
            file=sys.stderr,
        )
        return default

def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    print(
        f"[{time.strftime('%H:%M:%S')}] ВНИМАНИЕ: {name}='{value}' не bool, использую {default}",
        file=sys.stderr,
    )
    return default

def env_list(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    raw = value.strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]

LAPI_URL = env_str("CROWDSEC_BIRD_LAPI_URL", "http://127.0.0.1:8080")
CONFIG_FILE_V4 = env_str("CROWDSEC_BIRD_CONFIG_FILE_V4", "/etc/bird/blackhole_dynamic_v4.conf")
CONFIG_FILE_V6 = env_str("CROWDSEC_BIRD_CONFIG_FILE_V6", "/etc/bird/blackhole_dynamic_v6.conf")
bird_reload_cmd = env_str("CROWDSEC_BIRD_RELOAD_CMD", "/usr/sbin/birdc configure soft")
try:
    BIRD_RELOAD = shlex.split(bird_reload_cmd)
except ValueError:
    print(
        f"[{time.strftime('%H:%M:%S')}] ВНИМАНИЕ: CROWDSEC_BIRD_RELOAD_CMD задан некорректно, использую дефолт",
        file=sys.stderr,
    )
    BIRD_RELOAD = ["/usr/sbin/birdc", "configure", "soft"]
if not BIRD_RELOAD:
    BIRD_RELOAD = ["/usr/sbin/birdc", "configure", "soft"]
BLACKHOLE_TYPE = env_str("CROWDSEC_BIRD_BLACKHOLE_TYPE", "blackhole")
ADD_COMMUNITY_IN_CONFIG_V4 = env_bool("CROWDSEC_BIRD_ADD_COMMUNITY_IN_CONFIG_V4", False)
COMMUNITY_V4 = env_str("CROWDSEC_BIRD_COMMUNITY_V4", "65535:666")
ADD_COMMUNITY_IN_CONFIG_V6 = env_bool("CROWDSEC_BIRD_ADD_COMMUNITY_IN_CONFIG_V6", False)
COMMUNITY_V6 = env_str("CROWDSEC_BIRD_COMMUNITY_V6", "65535:666")
POLL_INTERVAL = env_int("CROWDSEC_BIRD_POLL_INTERVAL", 30)
ALLOWED_ORIGINS = env_list("CROWDSEC_BIRD_ALLOWED_ORIGINS", [])
DECISION_TYPES = env_list("CROWDSEC_BIRD_DECISION_TYPES", ["ban"])
SCOPES = env_str("CROWDSEC_BIRD_SCOPES", "ip,range")
IPV4_ONLY = env_bool("CROWDSEC_BIRD_IPV4_ONLY", True)
NORMALIZE_RANGES = env_bool("CROWDSEC_BIRD_NORMALIZE_RANGES", True)
VERBOSE_NO_CHANGES = env_bool("CROWDSEC_BIRD_VERBOSE_NO_CHANGES", False)
MAX_TOTAL_PREFIXES = env_int("CROWDSEC_BIRD_MAX_TOTAL_PREFIXES", 30000)
BLOCK_PRIVATE_PREFIXES = env_bool("CROWDSEC_BIRD_BLOCK_PRIVATE_PREFIXES", True)
MIN_PREFIXLEN_V4 = env_int("CROWDSEC_BIRD_MIN_PREFIXLEN_V4", 24)
MIN_PREFIXLEN_V6 = env_int("CROWDSEC_BIRD_MIN_PREFIXLEN_V6", 48)
BOUNCER_NAME = env_str("CROWDSEC_BIRD_BOUNCER_NAME", "bird-stream-bouncer")
BOUNCER_VERSION = env_str("CROWDSEC_BIRD_BOUNCER_VERSION", "1.0.0")
REQUEST_TIMEOUT = env_int("CROWDSEC_BIRD_REQUEST_TIMEOUT", 15)
ENABLE_USAGE_METRICS = env_bool("CROWDSEC_BIRD_ENABLE_USAGE_METRICS", True)
USAGE_METRICS_INTERVAL = env_int("CROWDSEC_BIRD_USAGE_METRICS_INTERVAL", 300)
USAGE_METRICS_TYPE = env_str("CROWDSEC_BIRD_USAGE_METRICS_TYPE", "network")
USAGE_METRICS_NAME = env_str("CROWDSEC_BIRD_USAGE_METRICS_NAME", BOUNCER_NAME)

if POLL_INTERVAL < 1:
    POLL_INTERVAL = 1
if REQUEST_TIMEOUT < 1:
    REQUEST_TIMEOUT = 1
if USAGE_METRICS_INTERVAL < 10:
    USAGE_METRICS_INTERVAL = 10

# ===================================================================

API_KEY = os.getenv('CROWDSEC_API_KEY')
if not API_KEY:
    print("ОШИБКА: Переменная окружения CROWDSEC_API_KEY не задана!", file=sys.stderr)
    print("   Укажи её в systemd-юните через Environment= или EnvironmentFile=", file=sys.stderr)
    sys.exit(1)

for cfg in [CONFIG_FILE_V4] + ([CONFIG_FILE_V6] if CONFIG_FILE_V6 else []):
    dir_path = os.path.dirname(cfg)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

headers = {
    "X-Api-Key": API_KEY,
    "User-Agent": f"{BOUNCER_NAME}/{BOUNCER_VERSION}",
    "Accept": "application/json",
}
session = requests.Session()
session.headers.update(headers)
current_prefixes = set()
startup_timestamp = int(time.time())
last_metrics_sent_at = 0
stats = {
    "polls_ok": 0,
    "polls_http_error": 0,
    "polls_conn_error": 0,
    "decisions_new": 0,
    "decisions_deleted": 0,
    "bird_reloads_ok": 0,
    "bird_reloads_error": 0,
}
usage_metrics_runtime_enabled = ENABLE_USAGE_METRICS

def reload_bird():
    try:
        subprocess.run(["/usr/sbin/birdc", "configure", "check"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(BIRD_RELOAD, check=True)
        stats["bird_reloads_ok"] += 1
        print(f"[{time.strftime('%H:%M:%S')}] BIRD перезагружен успешно")
    except subprocess.CalledProcessError as e:
        stats["bird_reloads_error"] += 1
        print(f"[{time.strftime('%H:%M:%S')}] ОШИБКА BIRD: конфиг не применён (синтаксис или reload): {e}", file=sys.stderr)

def is_valid_prefix(prefix_str):
    try:
        net = ipaddress.ip_network(prefix_str, strict=False)
        if BLOCK_PRIVATE_PREFIXES and net.is_private:
            return False
        if net.version == 4 and net.prefixlen < MIN_PREFIXLEN_V4:
            return False
        if net.version == 6 and net.prefixlen < MIN_PREFIXLEN_V6:
            return False
        return True
    except ValueError:
        return False

def build_prefix_list(ip_version):
    result = []
    for prefix_str in sorted(current_prefixes):
        try:
            net = ipaddress.ip_network(prefix_str, strict=False)
        except ValueError:
            continue
        if net.version != ip_version:
            continue
        if not is_valid_prefix(prefix_str):
            continue
        result.append(str(net) if NORMALIZE_RANGES else prefix_str)
    return result

def write_bird_config(config_path, prefixes, ip_version, add_community, community_value):
    with open(config_path, "w") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass

        f.write(f"# Автогенерируемые IPv{ip_version} blackhole-роуты от CrowdSec\n")
        f.write(f"# Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Всего префиксов: {len(prefixes)}\n\n")

        for prefix in prefixes:
            line = f"route {prefix} {BLACKHOLE_TYPE}"
            if add_community:
                line += f" community [{community_value}]"
            f.write(line + ";\n")

        f.flush()
        os.fsync(f.fileno())

    return len(prefixes)

def update_config():
    ipv4_prefixes = build_prefix_list(4)
    ipv6_prefixes = build_prefix_list(6) if CONFIG_FILE_V6 else []
    total_prefixes = len(ipv4_prefixes) + len(ipv6_prefixes)

    if MAX_TOTAL_PREFIXES and total_prefixes > MAX_TOTAL_PREFIXES:
        print(f"[{time.strftime('%H:%M:%S')}] ВНИМАНИЕ: Превышен лимит ({total_prefixes} > {MAX_TOTAL_PREFIXES}). Конфиг НЕ обновляется.", file=sys.stderr)
        return

    ipv4_count = write_bird_config(
        CONFIG_FILE_V4,
        ipv4_prefixes,
        4,
        ADD_COMMUNITY_IN_CONFIG_V4,
        COMMUNITY_V4,
    )

    ipv6_count = 0
    if CONFIG_FILE_V6:
        ipv6_count = write_bird_config(
            CONFIG_FILE_V6,
            ipv6_prefixes,
            6,
            ADD_COMMUNITY_IN_CONFIG_V6,
            COMMUNITY_V6,
        )

    reload_bird()
    print(f"[{time.strftime('%H:%M:%S')}] Обновлены конфиги: IPv4={ipv4_count}, IPv6={ipv6_count}, всего={total_prefixes}")

def prefix_totals():
    ipv4_count = 0
    ipv6_count = 0
    for prefix_str in current_prefixes:
        try:
            net = ipaddress.ip_network(prefix_str, strict=False)
        except ValueError:
            continue
        if not is_valid_prefix(prefix_str):
            continue
        if net.version == 4:
            ipv4_count += 1
        elif net.version == 6:
            ipv6_count += 1
    return ipv4_count, ipv6_count

def build_usage_metrics_payload(window_size):
    now = int(time.time())
    active_v4, active_v6 = prefix_totals()
    payload = {
        "remediation_components": [
            {
                "type": USAGE_METRICS_TYPE,
                "name": USAGE_METRICS_NAME,
                "version": BOUNCER_VERSION,
                "last_pull": now,
                "utc_startup_timestamp": startup_timestamp,
                "os": {
                    "name": platform.system() or "unknown",
                    "family": platform.system() or "unknown",
                    "version": platform.release() or "unknown",
                },
                "metrics": [
                    {
                        "meta": {
                            "window_size_seconds": window_size,
                            "utc_now_timestamp": now,
                        },
                        "items": [
                            {"name": "active_prefixes_total", "value": active_v4 + active_v6, "unit": "count"},
                            {"name": "active_prefixes_v4", "value": active_v4, "unit": "count"},
                            {"name": "active_prefixes_v6", "value": active_v6, "unit": "count"},
                            {"name": "decisions_new_total", "value": stats["decisions_new"], "unit": "count"},
                            {"name": "decisions_deleted_total", "value": stats["decisions_deleted"], "unit": "count"},
                            {"name": "polls_ok_total", "value": stats["polls_ok"], "unit": "count"},
                            {"name": "polls_http_error_total", "value": stats["polls_http_error"], "unit": "count"},
                            {"name": "polls_conn_error_total", "value": stats["polls_conn_error"], "unit": "count"},
                            {"name": "bird_reloads_ok_total", "value": stats["bird_reloads_ok"], "unit": "count"},
                            {"name": "bird_reloads_error_total", "value": stats["bird_reloads_error"], "unit": "count"},
                        ],
                    }
                ],
                "feature_flags": [],
            }
        ]
    }
    return payload

def send_usage_metrics():
    global last_metrics_sent_at, usage_metrics_runtime_enabled
    if not usage_metrics_runtime_enabled:
        return

    now = int(time.time())
    if last_metrics_sent_at and (now - last_metrics_sent_at) < USAGE_METRICS_INTERVAL:
        return

    window_size = USAGE_METRICS_INTERVAL if not last_metrics_sent_at else max(1, now - last_metrics_sent_at)
    url = f"{LAPI_URL.rstrip('/')}/v1/usage-metrics"
    payload = build_usage_metrics_payload(window_size)
    try:
        response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        last_metrics_sent_at = now
        if VERBOSE_NO_CHANGES:
            print(f"[{time.strftime('%H:%M:%S')}] Usage metrics sent / Метрики отправлены")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        print(f"[{time.strftime('%H:%M:%S')}] Usage metrics HTTP error / HTTP ошибка метрик: {e}", file=sys.stderr)
        if status in (404, 405, 501):
            usage_metrics_runtime_enabled = False
            print(
                f"[{time.strftime('%H:%M:%S')}] Usage metrics disabled / Метрики отключены: endpoint unavailable ({status})",
                file=sys.stderr,
            )
    except requests.exceptions.RequestException as e:
        print(f"[{time.strftime('%H:%M:%S')}] Usage metrics error / Ошибка отправки метрик: {e}", file=sys.stderr)

print(f"Starting CrowdSec -> BIRD blackhole bouncer / Запуск CrowdSec -> BIRD bouncer, UA={headers['User-Agent']}")
if ENABLE_USAGE_METRICS:
    print(f"Usage metrics: enabled / включены, interval={USAGE_METRICS_INTERVAL}s, component={USAGE_METRICS_NAME}")
else:
    print("Usage metrics: disabled / отключены")

full_url = f"{LAPI_URL.rstrip('/')}/v1/decisions/stream"
startup = True

while True:
    params = {"scopes": SCOPES}
    if ALLOWED_ORIGINS:
        params["origins"] = ",".join(ALLOWED_ORIGINS)
    if startup:
        params["startup"] = "true"

    try:
        if VERBOSE_NO_CHANGES or not startup:
            print(f"[{time.strftime('%H:%M:%S')}] Опрос LAPI (startup={startup})...")

        response = session.get(full_url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        stats["polls_ok"] += 1

        deleted_count = new_count = 0

        if data.get("deleted"):
            for d in data["deleted"]:
                if d.get("type") in DECISION_TYPES and (not ALLOWED_ORIGINS or d.get("origin") in ALLOWED_ORIGINS):
                    val = d.get("value")
                    if val and val in current_prefixes:
                        try:
                            version = ipaddress.ip_network(val, strict=False).version
                        except ValueError:
                            version = None
                        if not IPV4_ONLY or version == 4:
                            current_prefixes.remove(val)
                            deleted_count += 1
                            stats["decisions_deleted"] += 1

        if data.get("new"):
            for d in data["new"]:
                if d.get("type") in DECISION_TYPES and (not ALLOWED_ORIGINS or d.get("origin") in ALLOWED_ORIGINS):
                    val = d.get("value")
                    if val and val not in current_prefixes and is_valid_prefix(val):
                        if not IPV4_ONLY or ipaddress.ip_network(val, strict=False).version == 4:
                            current_prefixes.add(val)
                            new_count += 1
                            stats["decisions_new"] += 1

        if new_count or deleted_count or startup:
            print(f"[{time.strftime('%H:%M:%S')}] Обработано: +{new_count} новых, -{deleted_count} удалено")
            update_config()
        else:
            if VERBOSE_NO_CHANGES:
                print(f"[{time.strftime('%H:%M:%S')}] Изменений нет")

        if startup:
            print(f"[{time.strftime('%H:%M:%S')}] Initial sync done / Изначально загружено {len(current_prefixes)} префиксов")
            startup = False

        send_usage_metrics()

    except requests.exceptions.HTTPError as e:
        stats["polls_http_error"] += 1
        status = e.response.status_code if e.response is not None else "unknown"
        body = e.response.text.strip().replace("\n", " ") if e.response is not None else str(e)
        if len(body) > 300:
            body = body[:300] + "..."
        print(f"[{time.strftime('%H:%M:%S')}] HTTP ошибка LAPI ({status}): {body}. Жду 10 сек...", file=sys.stderr)
        if status in (401, 403):
            print(f"[{time.strftime('%H:%M:%S')}] Подсказка: проверь правильность/актуальность CROWDSEC_API_KEY для bouncer '{BOUNCER_NAME}'.", file=sys.stderr)
        time.sleep(10)
    except requests.exceptions.RequestException as e:
        stats["polls_conn_error"] += 1
        print(f"[{time.strftime('%H:%M:%S')}] Ошибка соединения с LAPI: {e}. Жду 10 сек...", file=sys.stderr)
        time.sleep(10)
    except json.JSONDecodeError as e:
        print(f"[{time.strftime('%H:%M:%S')}] Некорректный JSON: {e}")
        time.sleep(10)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Неожиданная ошибка: {e}", file=sys.stderr)
        time.sleep(10)

    time.sleep(POLL_INTERVAL)
