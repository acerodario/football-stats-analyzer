"""Cliente de la API interna de SofaScore.

La web carga todos sus datos desde /api/v1/... (visto en el .har). Abrimos la
página con Playwright y hacemos los fetch desde dentro del navegador, así las
peticiones llevan las mismas cookies/cabeceras que un usuario normal.
"""
import asyncio
import json
import random

from playwright.async_api import async_playwright

BASE = "https://www.sofascore.com"
API = BASE + "/api/v1"

_FETCH_JS = """async (u) => {
  try {
    const r = await fetch(u, {headers: {accept: '*/*'}});
    return [r.status, await r.text()];
  } catch (e) {
    return [0, String(e)];
  }
}"""


class SofaError(Exception):
    pass


class BlockedError(SofaError):
    """SofaScore devolvió 403 'challenge': hay que esperar antes de seguir."""


class SofaClient:
    def __init__(self, concurrency=3, rate=3.0, headless=True):
        self.concurrency = concurrency
        self.rate = rate  # peticiones por segundo como máximo
        self.headless = headless

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self):
        self._sem = asyncio.Semaphore(self.concurrency)
        self._reload_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._ctx = await self._browser.new_context(locale="es-ES")
        self._page = await self._ctx.new_page()
        await self._page.route("**/*", self._block_heavy)
        await self._page.goto(BASE + "/es-la", wait_until="domcontentloaded", timeout=60000)

    @staticmethod
    async def _block_heavy(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    async def _reload(self):
        async with self._reload_lock:
            try:
                await self._page.goto(BASE + "/es-la", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

    async def _throttle(self):
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            wait = self._next_slot - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = max(loop.time(), self._next_slot) + 1 / self.rate

    async def get(self, path, retries=4):
        """Devuelve el JSON del endpoint, o None si es 404 (no hay datos)."""
        url = API + path
        status, text = 0, ""
        for attempt in range(retries):
            await self._throttle()
            async with self._sem:
                status, text = await self._page.evaluate(_FETCH_JS, url)
            if status == 200:
                return json.loads(text)
            if status == 404:
                return None
            if status == 403 and "challenge" in text:
                if attempt == retries - 1:
                    raise BlockedError("SofaScore bloqueó temporalmente las peticiones (403 challenge)")
                # bajar el ritmo antes de reintentar
                await asyncio.sleep(20 * (attempt + 1))
                await self._reload()
                continue
            # 429 / 5xx / error de red: esperar y reintentar
            await asyncio.sleep(2 ** attempt + random.random())
        raise SofaError(f"HTTP {status} en {path}: {text[:200]}")

    async def get_many(self, paths):
        """Pide varios endpoints en paralelo. Los que fallan quedan como excepción,
        salvo un bloqueo, que se propaga para detener el scrape."""
        res = await asyncio.gather(*(self.get(p) for p in paths), return_exceptions=True)
        for r in res:
            if isinstance(r, BlockedError):
                raise r
        return res

    async def close(self):
        for obj in ("_browser", "_pw"):
            o = getattr(self, obj, None)
            if o is None:
                continue
            try:
                await (o.close() if obj == "_browser" else o.stop())
            except Exception:
                pass
