# Search-OP Collector Worker

Daemon para Raspberry Pi que recolecta productos de Mercado Libre Colombia y los envía al servidor Search-OP.

## ¿Por qué una Raspberry Pi?

El servidor Search-OP está en un datacenter con IP bloqueada por Mercado Libre (403). La Raspberry Pi en casa tiene IP residencial colombiana → sin bloqueos.

## Arquitectura

```
┌──────────────────────────────┐      ┌─────────────────────────────────┐
│  🏠 Raspberry Pi             │      │  ☁️ Servidor Search-OP           │
│  collector_worker.py         │      │                                 │
│                              │      │  GET /api/v1/collector/jobs     │
│  ── poll cada 2 min ───────────────▶│  ← recibe jobs pendientes       │
│                              │      │                                 │
│  ── search ML API ──────────▶│      │  POST /api/v1/collector/results │
│  (IP residencial → OK)       │      │  ← entrega snapshots            │
│                              │──────▶│                                 │
│                              │      │  → guarda en PostgreSQL         │
│                              │      │  → trend_detector + orphans     │
└──────────────────────────────┘      └─────────────────────────────────┘
```

## Instalación en la Raspberry Pi

```bash
# Clonar
git clone https://github.com/luisito666/search-op-collector.git
cd search-op-collector

# Instalar dependencias
pip install -r requirements.txt
```

### Instalación de Playwright

```bash
pip install playwright
playwright install chromium
playwright install-deps  # Instala dependencias del sistema (necesario en Raspberry Pi)
```

```bash

# Crear archivo .env
cat > .env << 'EOF'
export SEARCHOP_SERVER_URL=https://searchop.luisito.dev
export SEARCHOP_ML_CLIENT_ID=tu_client_id
export SEARCHOP_ML_CLIENT_SECRET=tu_client_secret
export SEARCHOP_POLL_SECONDS=120
EOF

# Ejecutar
source .env && python collector_worker.py
```

## Systemd (24/7)

```ini
# /etc/systemd/system/searchop-collector.service
[Unit]
Description=Search-OP Collector Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/search-op-collector
EnvironmentFile=/home/pi/search-op-collector/.env
ExecStart=/usr/bin/python3 collector_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now searchop-collector
sudo systemctl status searchop-collector
```

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SEARCHOP_SERVER_URL` | `http://localhost:8000` | URL del servidor Search-OP |
| `SEARCHOP_ML_CLIENT_ID` | *(requerido)* | Client ID de la app ML |
| `SEARCHOP_ML_CLIENT_SECRET` | *(requerido)* | Client Secret de la app ML |
| `SEARCHOP_POLL_SECONDS` | `120` | Intervalo de polling en segundos |

## Licencia

MIT
