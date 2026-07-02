#!/usr/bin/env python3
"""
Search-OP Collector Worker — Raspberry Pi Daemon.

Hace polling cada 2 minutos al servidor para recibir jobs pendientes,
busca productos reales en Mercado Libre vía scraping HTML (IP residencial),
y envía los resultados de vuelta.

Uso:
    python collector_worker.py

Variables de entorno requeridas:
    SEARCHOP_SERVER_URL    — URL del servidor Search-OP (ej: https://searchop.luisito.dev)
    SEARCHOP_POLL_SECONDS  — Intervalo de polling en segundos (default: 120)

Variables opcionales (OAuth, no requeridas para scraping):
    SEARCHOP_ML_CLIENT_ID  — Mercado Libre App client_id
    SEARCHOP_ML_CLIENT_SECRET — Mercado Libre App client_secret
"""

import os
import re
import sys
import time
import logging
import httpx
from bs4 import BeautifulSoup

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
    """Busca productos en Mercado Libre Colombia vía scraping HTML.

    Desde IP residencial colombiana, la web pública de ML no bloquea.
    Parseamos los resultados del listado para extraer datos reales de productos.
    """
    encoded_query = query.replace(" ", "-").replace("+", "-")
    url = f"https://listado.mercadolibre.com.co/{encoded_query}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code != 200:
        logger.warning(f"ML page returned {resp.status_code} for '{query}'")
        return []

    if "suspicious-traffic" in resp.text or "account-verification" in resp.text:
        logger.warning(f"ML returned CAPTCHA/suspicious traffic page for '{query}'")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.select("li.ui-search-layout__item, div.ui-search-result__wrapper, ol.ui-search-layout li")
    if not items:
        items = soup.select("[class*='ui-search-result']")

    results = []
    for item in items[:limit]:
        try:
            title_elem = item.select_one("h2, h3, .ui-search-item__title")
            title = title_elem.get_text(strip=True) if title_elem else ""
            if not title:
                title_elem = item.select_one("a[title]")
                title = title_elem.get("title", "") if title_elem else ""
            if not title:
                continue

            price_cop = 0.0
            price_elem = item.select_one(".price-tag-fraction, .andes-money-amount__fraction, span.andes-money-amount__fraction")
            if price_elem:
                price_text = price_elem.get_text(strip=True).replace(".", "").replace(",", "")
                try:
                    price_cop = float(price_text)
                except ValueError:
                    pass

            permalink = ""
            link_elem = item.select_one("a.ui-search-link, a[href*='/MCO-']")
            if link_elem:
                permalink = link_elem.get("href", "")
                if permalink and not permalink.startswith("http"):
                    permalink = "https://www.mercadolibre.com.co" + permalink

            ml_product_id = ""
            if permalink:
                match = re.search(r'/MCO-(\d+)', permalink)
                if match:
                    ml_product_id = match.group(1)
            if not ml_product_id:
                item_id_elem = item.select_one("[id*='MLC'], [id*='MCO']")
                if item_id_elem:
                    id_match = re.search(r'MCO(\d+)', item_id_elem.get("id", ""))
                    if id_match:
                        ml_product_id = id_match.group(1)
            if not ml_product_id:
                continue

            thumbnail = ""
            img_elem = item.select_one("img.ui-search-result-image__element, img[src*='mlstatic'], img[data-src]")
            if img_elem:
                thumbnail = img_elem.get("src") or img_elem.get("data-src") or ""

            sold_quantity = 0
            sold_elem = item.find(string=re.compile(r'\d+\s+vendido', re.IGNORECASE))
            if sold_elem:
                sold_match = re.search(r'(\d+)\s+vendido', sold_elem, re.IGNORECASE)
                if sold_match:
                    sold_quantity = int(sold_match.group(1))

            seller_nickname = ""
            seller_elem = item.find(string=re.compile(r'por\s+\w+', re.IGNORECASE))
            if seller_elem:
                seller_match = re.search(r'por\s+(\S+)', seller_elem, re.IGNORECASE)
                if seller_match:
                    seller_nickname = seller_match.group(1)

            rating = None
            rating_elem = item.select_one(".ui-search-reviews__rating, [class*='star']")
            if rating_elem:
                rating_text = rating_elem.get_text(strip=True)
                rating_match = re.search(r'(\d+\.?\d*)', rating_text)
                if rating_match:
                    try:
                        rating = float(rating_match.group(1))
                    except ValueError:
                        pass

            results.append({
                "ml_product_id": ml_product_id[:20],
                "title": title[:300],
                "price_cop": price_cop,
                "sold_quantity": sold_quantity,
                "available_quantity": 0,
                "rating": rating,
                "seller_id": seller_nickname[:20],
                "seller_nickname": seller_nickname[:100],
                "category_id": "",
                "condition": "new",
                "thumbnail": thumbnail,
                "permalink": permalink,
            })
        except Exception as e:
            logger.debug(f"Error parsing item in '{query}': {e}")
            continue

    seen = set()
    unique_results = []
    for r in results:
        if r["ml_product_id"] not in seen:
            seen.add(r["ml_product_id"])
            unique_results.append(r)

    logger.info(f"ML scrape '{query}' → {len(unique_results)} productos (from HTML)")
    return unique_results


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
            snapshots = await search_ml(query, limit=limit)
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
        logger.warning("ML credentials not set — OAuth features disabled, but HTML scraping works without them")

    logger.info(f"Search-OP Collector Worker v0.1.0")
    logger.info(f"Server: {SERVER_URL}")
    logger.info(f"Poll interval: {POLL_SECONDS}s")

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Shutting down")
