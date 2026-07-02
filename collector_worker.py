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
import sys
import time
import logging
import httpx
from playwright.async_api import async_playwright

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


async def search_ml(query: str, limit: int = 50) -> list[dict]:
    """Busca productos en Mercado Libre Colombia usando Playwright (navegador real).

    Mercado Libre requiere JavaScript y un navegador real. Playwright lanza
    Chromium headless que ML no puede distinguir de un usuario genuino.
    """
    encoded_query = query.replace(" ", "-").replace("+", "-")
    url = f"https://listado.mercadolibre.com.co/{encoded_query}"

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )

        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="es-CO",
                viewport={"width": 1366, "height": 768},
            )
            page = await context.new_page()

            # Visit home page first to get session cookies
            await page.goto("https://www.mercadolibre.com.co/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            try:
                await page.wait_for_selector("li.ui-search-layout__item, ol.ui-search-layout li", timeout=10000)
            except Exception:
                logger.warning(f"No product cards found for '{query}' after 10s")
                await browser.close()
                return []

            for _ in range(min(limit // 24 + 1, 5)):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)

            products = await page.evaluate("""() => {
                const items = document.querySelectorAll('li.ui-search-layout__item, div.ui-search-result__wrapper, div.andes-card');
                const results = [];

                items.forEach(item => {
                    const link = item.querySelector('a.ui-search-link, a[href*="/MCO-"]');
                    if (!link) return;
                    const href = link.href || link.getAttribute('href') || '';
                    const idMatch = href.match(/MCO-(\\d+)/);
                    if (!idMatch) return;

                    const titleEl = item.querySelector('h2, h3, .ui-search-item__title');
                    const title = titleEl ? titleEl.innerText.trim() : (link.title || link.getAttribute('title') || '');
                    if (!title) return;

                    let price = 0;
                    const priceEl = item.querySelector('.price-tag-fraction, .andes-money-amount__fraction, span.andes-money-amount__fraction');
                    if (priceEl) {
                        price = parseFloat(priceEl.innerText.replace(/\\./g, '').replace(/,/g, '.')) || 0;
                    }

                    const img = item.querySelector('img.ui-search-result-image__element, img[src*="mlstatic"]');
                    const thumbnail = img ? (img.src || img.getAttribute('data-src') || '') : '';

                    let soldQuantity = 0;
                    const soldEl = Array.from(item.querySelectorAll('*')).find(el => /vendido/i.test(el.innerText));
                    if (soldEl) {
                        const soldMatch = soldEl.innerText.match(/(\\d+)\\s+vendido/i);
                        if (soldMatch) soldQuantity = parseInt(soldMatch[1]) || 0;
                    }

                    let sellerNickname = '';
                    const sellerEl = Array.from(item.querySelectorAll('*')).find(el => /^por\\s/i.test(el.innerText));
                    if (sellerEl) {
                        const sellerMatch = sellerEl.innerText.match(/por\\s+(\\S+)/i);
                        if (sellerMatch) sellerNickname = sellerMatch[1];
                    }

                    let rating = null;
                    const ratingEl = item.querySelector('.ui-search-reviews__rating, [class*="star"]');
                    if (ratingEl) {
                        const ratingMatch = ratingEl.innerText.match(/(\\d+\\.?\\d*)/);
                        if (ratingMatch) rating = parseFloat(ratingMatch[1]) || null;
                    }

                    results.push({
                        ml_product_id: idMatch[1].substring(0, 20),
                        title: title.substring(0, 300),
                        price_cop: price,
                        sold_quantity: soldQuantity,
                        available_quantity: 0,
                        rating: rating,
                        seller_id: sellerNickname.substring(0, 20),
                        seller_nickname: sellerNickname.substring(0, 100),
                        category_id: '',
                        condition: 'new',
                        thumbnail: thumbnail,
                        permalink: href.startsWith('http') ? href : 'https://www.mercadolibre.com.co' + href,
                    });
                });

                return results;
            }""")

            seen = set()
            for prod in products:
                pid = prod.get("ml_product_id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append(prod)

            logger.info(f"ML scrape '{query}' → {len(results)} productos (Playwright)")

        except Exception as e:
            logger.error(f"Playwright error for '{query}': {e}")
        finally:
            await browser.close()

    return results[:limit]


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
                error_message=f"Unknown action: {action}",
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
