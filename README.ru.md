# CrowdSec BIRD Stream Bouncer

Language: [English](README.md) | **Русский**

Пользовательский remediation component для CrowdSec: преобразует решения CrowdSec в blackhole-маршруты BIRD и распространяет их по eBGP.

## Оглавление

1. [Обзор](#обзор)
2. [Поток данных](#поток-данных)
3. [Структура репозитория](#структура-репозитория)
4. [Требования](#требования)
5. [Установка на хост](#установка-на-хост)
6. [Конфигурация](#конфигурация)
7. [Сервис systemd](#сервис-systemd)
8. [Интеграция с BIRD](#интеграция-с-bird)
9. [Интеграция с MikroTik (пример)](#интеграция-с-mikrotik-пример)
10. [Проверка](#проверка)
11. [Метрики](#метрики)
12. [Docker (опционально)](#docker-опционально)
13. [Замечания по безопасности](#замечания-по-безопасности)
14. [Ограничения](#ограничения)
15. [Диагностика](#диагностика)

## Обзор

Проект реализует потоковую (stream-based) схему распространения blackhole-префиксов:

- CrowdSec LAPI предоставляет инкрементальные обновления через `GET /v1/decisions/stream`.
- Bouncer получает, фильтрует и нормализует решения.
- Для BIRD генерируются динамические фрагменты маршрутов (`v4` и `v6`).
- BIRD анонсирует маршруты на один или несколько роутеров по eBGP.
- На роутере входящая routing policy помечает маршруты как `blackhole`.

Цели дизайна:

- Вынести логику обработки на Linux-хост.
- Оставить роутеру профильную задачу маршрутизации.
- Исключить постоянные локальные file-fetch циклы в flash-память роутера.

## Поток данных

```text
CrowdSec LAPI
   |
   |  GET /v1/decisions/stream
   v
crowdsec-bird-bouncer.py
   |
   |  генерирует route-фрагменты
   v
/etc/bird/blackhole_dynamic_v4.conf
/etc/bird/blackhole_dynamic_v6.conf
   |
   |  birdc configure (или configure soft)
   v
BIRD2
   |
   |  eBGP-анонсы (+ опциональная community)
   v
Роутер (пример: MikroTik)
   |
   |  входящий routing filter => set blackhole
   v
Дроп трафика
```

## Структура репозитория

- `crowdsec-bird-bouncer.py`: основная логика bouncer.
- `crowdsec-bird-bouncer.env.example`: полный справочник параметров окружения.
- `crowdsec-bird-bouncer.service`: шаблон systemd unit.
- `Dockerfile`: базовая заготовка контейнера.
- `requirements.txt`: Python-зависимости.

## Требования

- Linux-хост с `python3`, `bird2`, CrowdSec/LAPI.
- Поднятая BGP-смежность между BIRD-хостом и роутером.
- Валидный API-ключ bouncer (`cscli bouncers add ...`).

## Установка на хост

1. Установить зависимости:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-requests bird2 crowdsec
```

2. Установить скрипт:

```bash
sudo install -m 0755 crowdsec-bird-bouncer.py /usr/local/bin/crowdsec-bird-bouncer.py
```

3. Создать ключ bouncer:

```bash
sudo cscli bouncers add bird-stream-bouncer
```

4. Установить env-конфиг:

```bash
sudo install -d -m 0755 /etc/crowdsec
sudo cp crowdsec-bird-bouncer.env.example /etc/crowdsec/bird-bouncer.env
sudo chmod 600 /etc/crowdsec/bird-bouncer.env
```

5. Записать реальный `CROWDSEC_API_KEY` в `/etc/crowdsec/bird-bouncer.env`.

## Конфигурация

Основной файл конфигурации:

- `/etc/crowdsec/bird-bouncer.env`

Ключевые параметры:

- `CROWDSEC_API_KEY`
- `CROWDSEC_BIRD_LAPI_URL`
- `CROWDSEC_BIRD_CONFIG_FILE_V4`
- `CROWDSEC_BIRD_CONFIG_FILE_V6` (пустое значение отключает IPv6-выгрузку)
- `CROWDSEC_BIRD_RELOAD_CMD`

Параметры безопасности и масштабирования:

- Ограничители префиксов: `CROWDSEC_BIRD_MIN_PREFIXLEN_V4`, `CROWDSEC_BIRD_MIN_PREFIXLEN_V6`, `CROWDSEC_BIRD_BLOCK_PRIVATE_PREFIXES`
- Ограничитель объема: `CROWDSEC_BIRD_MAX_TOTAL_PREFIXES`
- Параметры опроса: `CROWDSEC_BIRD_POLL_INTERVAL`, `CROWDSEC_BIRD_REQUEST_TIMEOUT`

### Доступ к LAPI для удаленного bouncer

Если bouncer работает не на том же хосте, что и CrowdSec, необходимо изменить параметры bind/access для LAPI в `/etc/crowdsec/config.yaml`.

Типичный дефолт - доступ только с loopback:

```yaml
api:
  server:
    listen_uri: 127.0.0.1:8080
    trusted_ips:
      - 127.0.0.1
      - ::1
```

Для удаленного доступа:

1. Установить `api.server.listen_uri` на адрес, доступный с хоста bouncer (например, management IP, не обязательно `0.0.0.0`).
2. Добавить IP-адрес(а) bouncer в `api.server.trusted_ips`.
3. Перезапустить CrowdSec:

```bash
sudo systemctl restart crowdsec
```

4. Проверить доступность LAPI-порта через сеть/фаервол.
5. Обновить env bouncer:

```bash
CROWDSEC_BIRD_LAPI_URL=http://<crowdsec-host>:<lapi-port>
```

Примечание:

- Порт должен совпадать с `listen_uri` (во многих дефолтных установках это `8088`).
- Минимизируйте поверхность доступа (trusted IP + firewall ACL).

## Сервис systemd

Установка и запуск:

```bash
sudo cp crowdsec-bird-bouncer.service /etc/systemd/system/crowdsec-bird-bouncer.service
sudo systemctl daemon-reload
sudo systemctl enable --now crowdsec-bird-bouncer.service
```

Проверка:

```bash
sudo systemctl status crowdsec-bird-bouncer.service
sudo journalctl -u crowdsec-bird-bouncer.service -n 100 -f
```

## Интеграция с BIRD

Минимальные требования:

- Динамические файлы должны include'иться из `bird.conf`.
- Экспорт должен разрешать только нужные CrowdSec dynamic routes.
- Маркировка community должна выполняться в одном месте (либо в BIRD filter, либо в bouncer).

### Полный референс `bird.conf`

Перед production-использованием заменить все значения-плейсхолдеры.

```bird
router id 192.168.140.11;     # Router ID BIRD-хоста
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
    local as 65101;          # ASN на стороне BIRD
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
    neighbor 192.168.140.1 as 65100;  # Peer и ASN роутера
    # Пример для нескольких peer/range:
    # neighbor range 192.168.140.0/24, 10.10.10.0/24, 172.16.0.0/16 as 65100;
}
```

Соответствие параметров:

- `router id`: Router ID BIRD-хоста.
- `local as`: ASN Linux/BIRD стороны.
- `neighbor ... as`: адрес и ASN роутера.
- Для eBGP ASN должны различаться.

Проверка и применение:

```bash
sudo chown -R bird:bird /etc/bird/
sudo birdc configure check
sudo birdc configure
sudo birdc show protocols all
```

## Интеграция с MikroTik (пример)

MikroTik используется как референс-пример. Подход применим к любому BGP-роутеру с policy/filter механизмом.

### Пример для RouterOS v7

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

Соответствие параметров:

- `router-id` / `local.address`: локальный адрес MikroTik.
- `remote.address`: адрес BIRD-хоста.
- `.as`: удаленный ASN (ASN BIRD).
- `input.filter`: policy, помечающая маршруты как blackhole.

Проверка сессии:

```mikrotik
/routing/bgp/session/print
:put [/routing/bgp/session/get [find name~"crowdsec"] prefix-count]
```

## Проверка

Добавление тестовых решений:

```bash
sudo cscli decisions add --ip 1.2.3.4 --type ban --duration 1h
sudo cscli decisions add --range 4.4.4.0/24 --type ban --duration 1h
```

Удаление тестовых решений:

```bash
sudo cscli decisions delete --ip 1.2.3.4
sudo cscli decisions delete --range 4.4.4.0/24
```

Наблюдать:

- логи bouncer (`journalctl`)
- состояние протоколов BIRD (`birdc show protocols all`)
- таблицу маршрутов и счетчики префиксов на роутере

## Метрики

Bouncer может отправлять usage metrics в `POST /v1/usage-metrics`.

Включение в env:

- `CROWDSEC_BIRD_ENABLE_USAGE_METRICS=true`
- `CROWDSEC_BIRD_USAGE_METRICS_INTERVAL=300`

Отправляются сервисные метрики компонента:

- активные префиксы (`v4`, `v6`, `total`)
- количество добавленных/удаленных решений
- успешные и ошибочные опросы LAPI
- успешные и ошибочные перезагрузки BIRD

Счетчики packet/byte drop на dataplane роутера в проект не входят.

## Docker (опционально)

### Режим A (рекомендуемый): BIRD на хосте + bouncer в контейнере

Использовать, если BIRD уже работает на хосте.

1. Подготовить env-конфиг на хосте:

```bash
sudo install -d -m 0755 /etc/crowdsec
sudo cp docker/recommended/bird-bouncer.env.example /etc/crowdsec/bird-bouncer.env
sudo chmod 600 /etc/crowdsec/bird-bouncer.env
```

2. Запуск:

```bash
docker compose -f compose.recommended.yml up -d --build
```

3. Логи:

```bash
docker compose -f compose.recommended.yml logs -f
```

В этом режиме пробрасываются host-пути BIRD:

- `/etc/bird`
- `/run/bird`

### Режим B (полный routing-стек): BIRD + bouncer в контейнерах

Подходит для lab/testing или когда нужно изолировать BIRD от хоста.

1. Создать env-файл:

```bash
cp docker/full/bird-bouncer.env.example docker/full/bird-bouncer.env
```

2. Отредактировать:

- `CROWDSEC_API_KEY`
- `CROWDSEC_BIRD_LAPI_URL`
- BGP-параметры в `docker/bird/bird.conf`

3. Запуск:

```bash
docker compose -f compose.full.yml up -d --build
```

4. Логи:

```bash
docker compose -f compose.full.yml logs -f
```

### Сборка только образа bouncer (при необходимости)

```bash
docker build -t crowdsec-bird-bouncer:local .
```

Пример ручного запуска:

```bash
docker run --rm \
  --name crowdsec-bird-bouncer \
  --network host \
  --env-file /etc/crowdsec/bird-bouncer.env \
  -v /etc/bird:/etc/bird \
  -v /run/bird:/run/bird \
  crowdsec-bird-bouncer:local
```

Замечания:

- Основной сценарий проекта: host-first (`systemd` + host BIRD).
- В контейнерном режиме bouncer должен иметь доступ к BIRD control socket (`birdc`).

## Замечания по безопасности

- Хранить API-ключ только в `/etc/crowdsec/bird-bouncer.env`.
- Выставлять строгие права (`chmod 600`) для env-файла.
- Не хардкодить секреты в source/unit.

## Ограничения

- Нет прямой телеметрии packet/byte drop из dataplane роутера.
- Скорость реакции зависит от интервала polling.
- Требуется стабильная BGP-смежность хост-роутер.
- Поведение routing policy зависит от реализации у конкретного вендора.
- Docker-режим не является zero-config из-за связи с BIRD control plane.

## Диагностика

Проверка bouncer и `last API pull`:

```bash
sudo cscli bouncers list
sudo cscli bouncers inspect bird-stream-bouncer
```

Проверка сервиса:

```bash
sudo systemctl status crowdsec-bird-bouncer.service
sudo journalctl -u crowdsec-bird-bouncer.service -f
```

Проверка BIRD:

```bash
sudo birdc show protocols all
sudo birdc configure check
```

Если в CrowdSec Console отображается `No metrics available`:

- проверить `CROWDSEC_BIRD_ENABLE_USAGE_METRICS=true`
- проверить, что LAPI принимает `POST /v1/usage-metrics`
- проверить логи bouncer на HTTP-ошибки отправки метрик
