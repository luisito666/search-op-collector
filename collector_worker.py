#!/usr/bin/env python3
"""
Search-OP Collector Worker — Raspberry Pi Daemon.

Hace polling cada 2 minutos al servidor para recibir jobs pendientes,
busca productos reales en Mercado Libre (IP residencial → sin bloqueo),
y envía los resultados de vuelta.

Uso:
    python collector_worker.py

Variables de entorno requeridas:
    SEARCHOP_SERVER_URL    — URL del servidor Search-OP (ej: https://searchop.luisito.dev)
    SEARCHOP_ML_CLIENT_ID  — Mercado Libre App client_id
    SEARCHOP_ML_CLIENT_SECRET — Mercado Libre App client_secret
    SEARCHOP_POLL_SECONDS  — Intervalo de polling en segundos (default: 120)
"""

import os
import sys
import time
import json
import logging
import httpx
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────

SERVER_URL = os.environ.get("SEARCHOP_SERVER_URL", "http://localhost:8000")
ML_CLIENT_ID = os.environ.get("SEARCHOP_ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.environ.get("SEARCHOP_ML_CLIENT_SECRET", "")
POLL_SECONDS = int(os.environ.get("SEARCHOP_POLL_SECONDS", "120"))

ML_AUTH_URL = "https://api.mercadolibre.com/oauth/token"
ML_API_BASE = "https://api.mercadolibre.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("collector")


# ── Mercado Libre API ───────────────────────────────────────────────


async def get_ml_token() -> str:
    """Obtiene access token OAuth client_credentials."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(ML_AUTH_URL, data={
            "grant_type": "client_credentials",
            "client_id": ML_CLIENT_ID,
            "client_secret": ML_CLIENT_SECRET,
        })
        resp.raise_for_status()
        return resp.json()["access_token"]


async def search_ml(query: str, limit: int = 50, access_token: str | None = None) -> list[dict]:
    """Busca productos en Mercado Libre Colombia.

    Si hay access_token, usa API autenticada. Si no, intenta pública.
    Retorna lista de snapshots listos para enviar al servidor.
    """
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    url = f"{ML_API_BASE}/sites/MCO/search?q={query}&limit={limit}"

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 403:
        logger.warning(f"ML search 403 for '{query}' — IP bloqueada o scope insuficiente")
        return []
    if resp.status_code != 200:
        logger.error(f"ML search failed: {resp.status_code} for '{query}'")
        return []

    data = resp.json()
    results = []

    for item in data.get("results", []):
        seller = item.get("seller", {})
        shipping = item.get("shipping", {})

        results.append({
            "ml_product_id": str(item.get("id", ""))[:20],
            "title": str(item.get("title", ""))[:300],
            "price_cop": float(item.get("price", 0)),
            "sold_quantity": int(item.get("sold_quantity", 0)),
            "available_quantity": int(item.get("available_quantity", 0)),
            "rating": item.get("rating"),
            "seller_id": str(seller.get("id", ""))[:20],
            "seller_nickname": str(seller.get("nickname", ""))[:100],
            "category_id": str(item.get("category_id", "")),
            "condition": str(item.get("condition", "new")),
            "thumbnail": str(item.get("thumbnail", "")),
            "permalink": str(item.get("permalink", "")),
        })

    logger.info(f"ML search '{query}' → {len(results)} productos")
    return results


# ── Server API ──────────────────────────────────────────────────────


async def fetch_jobs() -> list[dict]:
    """GET /api/v1/collector/jobs — obtiene jobs pendientes."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{SERVER_URL}/api/v1/collector/jobs")
        if resp.status_code != 200:
            logger.error(f"Failed to fetch jobs: {resp.status_code}")
            return []
        data = resp.json()
        return data.get("jobs", [])


async def submit_job_results(
    job_id: int,
    status: str,
    snapshots: list[dict],
    error_message: str | None = None,
) -> bool:
    """POST /api/v1/collector/results — entrega resultados de un job."""
    payload = {
        "job_id": job_id,
        "status": status,
        "snapshots": snapshots,
        "error_message": error_message,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SERVER_URL}/api/v1/collector/results",
            json=payload,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"Job #{job_id} submitted: {data.get('snapshots_saved', 0)} snapshots saved"
            )
            return True
        else:
            logger.error(f"Failed to submit job #{job_id}: {resp.status_code} {resp.text[:200]}")
            return False


# ── Main Loop ───────────────────────────────────────────────────────


async def process_job(job: dict):
    """Ejecuta un job individual."""
    job_id = job["id"]
    action = job["action"]
    query = job["query"]
    limit = job.get("limit", 50)

    logger.info(f"Processing job #{job_id}: {action} '{query}'")

    try:
        if action == "search":
            token = await get_ml_token()
            snapshots = await search_ml(query, limit=limit, access_token=token)
            await submit_job_results(job_id, "completed", snapshots)
        else:
            await submit_job_results(
                job_id, "failed", [],
                error_message=f"Unknown action: {action}"
            )
    except Exception as e:
        logger.error(f"Job #{job_id} failed: {e}")
        await submit_job_results(job_id, "failed", [], error_message=str(e))


async def main_loop():
    """Loop principal: polling → ejecutar jobs → repetir."""
    logger.info(f"Collector started. Server: {SERVER_URL}, Poll: {POLL_SECONDS}s")

    while True:
        try:
            jobs = await fetch_jobs()

            if jobs:
                logger.info(f"Received {len(jobs)} job(s)")
                for job in jobs:
                    await process_job(job)
            else:
                logger.debug("No pending jobs")

        except Exception as e:
            logger.error(f"Poll cycle error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import asyncio

    # Validar configuración
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET:
        logger.error("SEARCHOP_ML_CLIENT_ID and SEARCHOP_ML_CLIENT_SECRET are required")
        sys.exit(1)

    logger.info(f"Search-OP Collector Worker v0.1.0")
    logger.info(f"Server: {SERVER_URL}")
    logger.info(f"Poll interval: {POLL_SECONDS}s")

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Shutting down")
