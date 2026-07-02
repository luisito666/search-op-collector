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


async def search_ml(query: str, limit: int = 50, access_token: str | None = None) -> list[dict]:
    """Busca productos en Mercado Libre Colombia usando Playwright."""
    encoded_query = query.replace(" ", "-").replace("+", "-")
    url = f"https://listado.mercadolibre.com.co/{encoded_query}"

    results = []
    screenshot_path = f"/tmp/ml_debug_{query.replace(' ', '_')}.png"

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

            logger.debug("Visiting ML home page...")
            await page.goto("https://www.mercadolibre.com.co/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)

            logger.debug(f"Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            page_title = await page.title()
            page_url = page.url
            body_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : 'NO BODY'")

            logger.info(f"Page title: {page_title}")
            logger.info(f"Final URL: {page_url}")
            logger.info(f"Body preview: {body_text[:200]}")

            if "account-verification" in page_url or "verification" in page_url.lower():
                logger.warning(f"ML redirected to verification page for '{query}'")
                await page.screenshot(path=screenshot_path)
                logger.info(f"Screenshot saved: {screenshot_path}")
                await browser.close()
                return []

            no_results = await page.evaluate("""() => {
                const body = document.body ? document.body.innerText : '';
                return body.includes('No encontramos') || body.includes('no hay resultados') || body.includes('sin resultados');
            }""")
            if no_results:
                logger.info(f"ML returned 'no results' page for '{query}'")
                await browser.close()
                return []

            # STRATEGY 1: JS extraction with multiple selector fallbacks
            products_raw = await page.evaluate("""() => {
                const results = [];

                const selectors = [
                    'li.ui-search-layout__item',
                    'div.ui-search-result__wrapper',
                    'div.ui-search-result',
                    'ol.ui-search-layout > li',
                    'div.andes-card',
                    '[class*="ui-search"] a[href*="/MCO-"]',
                ];

                let items = [];
                for (const sel of selectors) {
                    items = document.querySelectorAll(sel);
                    if (items.length > 0) break;
                }

                if (items.length === 0) {
                    const links = document.querySelectorAll('a[href*="/MCO-"]');
                    for (const link of links) {
                        const card = link.closest('li, div[class], article');
                        if (card && !results.find(r => r.url === link.href)) {
                            results.push({
                                url: link.href,
                                title: link.title || link.getAttribute('aria-label') || link.innerText.trim().substring(0, 100),
                                cardHTML: card.outerHTML.substring(0, 500)
                            });
                        }
                    }
                    return results;
                }

                items.forEach(item => {
                    const link = item.querySelector('a[href*="/MCO-"]');
                    if (!link) return;

                    const href = link.href || '';
                    const idMatch = href.match(/MCO-?(\\d+)/);
                    const mlId = idMatch ? idMatch[1].substring(0, 20) : '';
                    if (!mlId) return;

                    const titleEl = item.querySelector('h2, h3, .ui-search-item__title, [class*="title"]');
                    const title = titleEl ? titleEl.innerText.trim() : (link.title || '');

                    const priceEl = item.querySelector('.price-tag-fraction, .andes-money-amount__fraction, [class*="price"] [class*="fraction"], span[class*="amount"]');
                    const priceText = priceEl ? priceEl.innerText.replace(/[^0-9]/g, '') : '0';
                    const price = parseInt(priceText) || 0;

                    const img = item.querySelector('img[src*="mlstatic"], img[src*="mercadolibre"], img');
                    const thumbnail = img ? (img.src || img.getAttribute('data-src') || '') : '';

                    let soldQty = 0;
                    const allText = item.innerText;
                    const soldMatch = allText.match(/(\\d+)\\s*vendid/i);
                    if (soldMatch) soldQty = parseInt(soldMatch[1]) || 0;

                    let seller = '';
                    const sellerMatch = allText.match(/por\\s+(\\S+)/i);
                    if (sellerMatch) seller = sellerMatch[1];

                    results.push({
                        ml_product_id: mlId,
                        title: (title || '').substring(0, 300),
                        price_cop: price,
                        sold_quantity: soldQty,
                        available_quantity: 0,
                        rating: null,
                        seller_id: seller.substring(0, 20),
                        seller_nickname: seller.substring(0, 100),
                        category_id: '',
                        condition: 'new',
                        thumbnail: thumbnail,
                        permalink: href,
                    });
                });

                return results;
            }""")

            logger.info(f"JS extraction found {len(products_raw)} items")

            # STRATEGY 2: BeautifulSoup HTML parse if JS found nothing
            if not products_raw:
                logger.warning("JS extraction found 0 items — falling back to HTML parse")
                html = await page.content()

                html_path = f"/tmp/ml_html_{query.replace(' ', '_')}.html"
                with open(html_path, "w") as f:
                    f.write(html[:50000])
                logger.info(f"HTML saved to {html_path} (first 50KB)")

                soup = BeautifulSoup(html, "html.parser")
                for link in soup.select('a[href*="/MCO-"]')[:limit]:
                    href = link.get("href", "")
                    id_match = re.search(r'/MCO-?(\d+)', href)
                    if not id_match:
                        continue

                    ml_id = id_match.group(1)[:20]
                    title = link.get("title") or link.get_text(strip=True)
                    if not title or len(title) < 3:
                        continue

                    price_el = None
                    parent = link.parent
                    for _ in range(5):
                        if parent is None:
                            break
                        price_el = parent.select_one('[class*="price"], [class*="Price"], .andes-money-amount')
                        if price_el:
                            break
                        parent = parent.parent

                    price = 0
                    if price_el:
                        price_text = price_el.get_text(strip=True)
                        price_match = re.search(r'[\d.]+', price_text.replace(".", "").replace(",", "."))
                        if price_match:
                            try:
                                price = int(float(price_match.group()))
                            except Exception:
                                pass

                    products_raw.append({
                        "ml_product_id": ml_id,
                        "title": title[:300],
                        "price_cop": price,
                        "sold_quantity": 0,
                        "available_quantity": 0,
                        "rating": None,
                        "seller_id": "",
                        "seller_nickname": "",
                        "category_id": "",
                        "condition": "new",
                        "thumbnail": "",
                        "permalink": href if href.startswith("http") else f"https://www.mercadolibre.com.co{href}",
                    })

                logger.info(f"HTML fallback found {len(products_raw)} items")

            if not products_raw:
                await page.screenshot(path=screenshot_path)
                logger.warning(f"0 products found for '{query}'. Screenshot: {screenshot_path}")

            seen = set()
            for prod in products_raw:
                pid = prod.get("ml_product_id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append(prod)

            logger.info(f"ML scrape '{query}' → {len(results)} productos (Playwright)")

        except Exception as e:
            logger.error(f"Playwright error for '{query}': {e}", exc_info=True)
            try:
                await page.screenshot(path=screenshot_path)
                logger.info(f"Error screenshot: {screenshot_path}")
            except Exception:
                pass
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
