"""
Vacancy Monitor — мониторинг вакансий на сайтах IT-компаний.

Запуск:
  python vacancy_monitor.py           # разовый, всегда шлёт сводку в TG
  python vacancy_monitor.py --server  # только при новых/обновлённых вакансиях

Конфиги:
  .env         — TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY (не в git)
  config.json  — сайты, ключевые слова, таймауты (не в git)
  state.json   — хэши виденных вакансий, создаётся автоматически (не в git)
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

CONFIG_FILE = Path("config.json")
STATE_FILE  = Path("state.json")
ENV_FILE    = Path(".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("vacancy_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    # Не перезаписываем переменные из окружения - можно переопределить через export
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict:
    _load_dotenv()
    if not CONFIG_FILE.exists():
        log.error(
            "config.json не найден. "
            "Скопируй config.example.json → config.json и заполни своими данными."
        )
        sys.exit(1)
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Ошибка в config.json: %s", e)
        sys.exit(1)


def get_tg_settings() -> dict:
    # Секреты читаются только из env - токен не попадёт в git даже случайно
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    proxy   = os.getenv("TELEGRAM_PROXY",   "")
    errors  = []
    if not token:
        errors.append("TELEGRAM_TOKEN")
    if not chat_id:
        errors.append("TELEGRAM_CHAT_ID")
    if errors:
        log.error("Не заданы переменные: %s — создай .env по образцу .env.example", ", ".join(errors))
        sys.exit(1)
    return {"token": token, "chat_id": chat_id, "proxy": proxy}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json повреждён, сброс.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _is_relevant(text: str, hard_kws: list, exclude_kws: list) -> tuple[bool, str]:
    # Исключения проверяем первыми "автоматизация" есть и в devops и в 1С-вакансиях
    lower = text.lower()
    for ex in exclude_kws:
        if ex in lower:
            return False, ""
    for kw in hard_kws:
        if kw in lower:
            return True, kw
    return False, ""


def _text_hash(text: str) -> str:
    # Хэшируем текст а не HTML, рекламные блоки и счётчики меняют HTML постоянно
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _extract_context(text: str, keyword: str, max_len: int = 200) -> str:
    # Вырезаем предложение с ключевым словом для превью в Telegram
    lower = text.lower()
    idx = lower.find(keyword.lower())
    if idx == -1:
        return text[:max_len].strip()

    left  = max(text.rfind(".", 0, idx), text.rfind("\n", 0, idx))
    start = left + 1 if left != -1 else max(0, idx - 120)

    right_dot = text.find(".", idx)
    right_nl  = text.find("\n", idx)
    candidates = [x for x in [right_dot, right_nl] if x != -1]
    end = min(candidates) + 1 if candidates else min(idx + 200, len(text))

    fragment = re.sub(r"\s+", " ", text[start:end].strip())
    if len(fragment) > max_len:
        fragment = fragment[:max_len].rstrip() + "…"
    return fragment


async def send_telegram(message: str, tg: dict) -> bool:
    token   = tg["token"]
    chat_id = tg["chat_id"]
    proxy   = tg.get("proxy", "").strip()
    if proxy.lower() in ("none", ""):
        proxy = None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":                  chat_id,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }

    async def _try(use_proxy: Optional[str]) -> bool:
        kwargs: dict = {"timeout": 15}
        if use_proxy:
            kwargs["proxy"] = use_proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    log.error("Telegram %s: %s", resp.status_code, resp.text[:200])
                    return False
                log.info("Telegram: отправлено.")
                return True
        except TypeError:
            # httpx < 0.27 использует proxies= вместо proxy=
            old: dict = {"timeout": 15}
            if use_proxy:
                old["proxies"] = use_proxy
            async with httpx.AsyncClient(**old) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    log.error("Telegram %s: %s", resp.status_code, resp.text[:200])
                    return False
                log.info("Telegram: отправлено.")
                return True

    try:
        return await _try(proxy)
    except Exception as exc:
        if proxy:
            # В TUN-режиме (Hiddify/WireGuard) трафик идёт через туннель напрямую,
            # отдельный HTTP-прокси не нужен
            log.warning("Прокси недоступен (%s), пробую без прокси...", exc)
            try:
                return await _try(None)
            except Exception as exc2:
                log.error("Telegram недоступен: %s", exc2)
                return False
        log.error("Telegram недоступен: %s", exc)
        return False


async def _safe_goto(page: Page, url: str, timeout: int) -> bool:
    try:
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        try:
            # networkidle некоторые SPA никогда не достигают это не ошибка
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeout:
            pass
        return True
    except PlaywrightTimeout:
        log.warning("Таймаут: %s", url)
        return False
    except Exception as exc:
        log.error("Ошибка загрузки %s: %s", url, exc)
        return False


async def _get_page_text(page: Page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return await page.content()


async def _collect_links(page: Page, base_url: str) -> list[dict]:
    # Приводим href к абсолютному виду т.к сайты часто используют относительные пути
    links = await page.query_selector_all("a[href]")
    results = []
    seen: set[str] = set()
    parsed_base = urlparse(base_url)

    for link in links:
        try:
            href  = await link.get_attribute("href") or ""
            title = (await link.inner_text()).strip()
            if href.startswith("http"):
                abs_href = href
            elif href.startswith("/"):
                abs_href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
            else:
                continue  # якоря, javascript: и прочий мусор
            if abs_href in seen:
                continue
            seen.add(abs_href)
            results.append({"href": abs_href, "title": title or href})
        except Exception:
            continue
    return results


async def check_url(
    page: Page,
    base_url: str,
    state: dict,
    new_state: dict,
    cfg: dict,
) -> list[dict]:
    hard_kws    = cfg["keywords"]["hard"]
    exclude_kws = cfg["keywords"]["exclude"]
    timeouts    = cfg["timeouts"]

    log.info("Проверяю: %s", base_url)
    found: list[dict] = []

    ok = await _safe_goto(page, base_url, timeouts["page_ms"])
    if not ok:
        return found

    all_links = await _collect_links(page, base_url)

    # Первичный отбор по title/href грубый, финальная проверка по тексту страницы
    candidate_links = [
        lnk for lnk in all_links
        if any(kw in f"{lnk['title']} {lnk['href']}".lower() for kw in hard_kws)
    ]

    if candidate_links:
        log.info("  Кандидатов: %d", len(candidate_links))
        for lnk in candidate_links:
            href  = lnk["href"]
            title = lnk["title"]

            await asyncio.sleep(0.5)
            detail_ok = await _safe_goto(page, href, timeouts["link_ms"])
            if not detail_ok:
                continue

            detail_text = await _get_page_text(page)
            relevant, kw = _is_relevant(detail_text, hard_kws, exclude_kws)

            if not relevant:
                log.info("  Пропускаю: %s", title[:60])
                try:
                    await page.go_back()
                except Exception:
                    # go_back() падает если был редирект или открылась новая вкладка
                    await _safe_goto(page, base_url, timeouts["page_ms"])
                await asyncio.sleep(0.3)
                continue

            h       = _text_hash(detail_text)
            context = _extract_context(detail_text, kw)
            prev    = state.get(href)
            new_state[href] = h

            if prev is None:
                status = "new"
                log.info("  НОВАЯ [%s]: %s", kw, title[:60])
            elif prev != h:
                status = "updated"
                log.info("  ОБНОВИЛАСЬ [%s]: %s", kw, title[:60])
            else:
                status = "seen"
                log.info("  Без изменений: %s", title[:60])

            found.append({"title": title, "url": href, "keyword": kw,
                          "status": status, "context": context})

            try:
                await page.go_back()
            except Exception:
                await _safe_goto(page, base_url, timeouts["page_ms"])
            await asyncio.sleep(0.3)

    else:
        # Нет отдельных страниц вакансий - мониторим саму страницу целиком
        page_text = await _get_page_text(page)
        relevant, kw = _is_relevant(page_text, hard_kws, exclude_kws)
        if relevant:
            h       = _text_hash(page_text)
            context = _extract_context(page_text, kw)
            prev    = state.get(base_url)
            new_state[base_url] = h

            if prev is None:
                status = "new"
                log.info("  НОВЫЙ контент [%s]", kw)
            elif prev != h:
                status = "updated"
                log.info("  Контент изменился [%s]", kw)
            else:
                status = "seen"
                log.info("  Без изменений (страница целиком)")

            found.append({"title": base_url, "url": base_url, "keyword": kw,
                          "status": status, "context": context})
        else:
            log.info("  Ничего релевантного")

    return found


def build_message(all_found: list[dict], server_mode: bool) -> Optional[str]:
    new_items     = [v for v in all_found if v["status"] == "new"]
    updated_items = [v for v in all_found if v["status"] == "updated"]
    seen_count    = sum(1 for v in all_found if v["status"] == "seen")
    has_changes   = bool(new_items or updated_items)

    if server_mode and not has_changes:
        return None

    mode_label = "🖥 серверный" if server_mode else "💻 разовый запуск"
    lines = [f"<b>Vacancy Monitor</b> ({mode_label})\n"]

    def fmt_vacancy(v: dict) -> str:
        title   = v["title"][:80]
        url     = v["url"]
        kw      = v["keyword"]
        context = v.get("context", "").strip()
        out = f'• <a href="{url}">{title}</a> <i>[{kw}]</i>'
        if context:
            highlighted = re.sub(
                f"({re.escape(kw)})", r"<b>\1</b>",
                context, flags=re.IGNORECASE, count=1,
            )
            out += f"\n  💬 <i>{highlighted}</i>"
        return out

    if new_items:
        lines.append("🆕 <b>Новые вакансии:</b>")
        lines.extend(fmt_vacancy(v) for v in new_items)
    if updated_items:
        lines.append("\n🔄 <b>Обновились:</b>")
        lines.extend(fmt_vacancy(v) for v in updated_items)
    if not has_changes:
        lines.append("✅ Новых вакансий нет")

    lines.append(
        f"\n📊 <b>Итог:</b> {len(new_items)} новых · "
        f"{len(updated_items)} обновлено · {seen_count} без изменений"
    )
    return "\n".join(lines)


async def main(server_mode: bool) -> None:
    log.info("=" * 60)
    log.info("Режим: %s", "SERVER" if server_mode else "ONCE")
    log.info("=" * 60)

    cfg   = load_config()
    tg    = get_tg_settings()
    state = load_state()
    new_state: dict = {}
    all_found: list = []

    log.info("URL для проверки: %d", len(cfg["target_urls"]))
    log.info("Прокси Telegram:  %s", tg["proxy"] or "нет")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        page = await context.new_page()

        for url in cfg["target_urls"]:
            try:
                found = await check_url(page, url, state, new_state, cfg)
                all_found.extend(found)
            except Exception as exc:
                log.error("Ошибка для %s: %s", url, exc)
            await asyncio.sleep(cfg["timeouts"]["inter_page_sec"])

        await browser.close()

    # Старые записи не удаляем - вакансия могла не загрузиться из-за таймаута
    merged_state = {**state, **new_state}
    save_state(merged_state)

    new_count     = sum(1 for v in all_found if v["status"] == "new")
    updated_count = sum(1 for v in all_found if v["status"] == "updated")
    log.info("=" * 60)
    log.info("Готово. Новых: %d, обновлено: %d", new_count, updated_count)

    message = build_message(all_found, server_mode)
    if message:
        await send_telegram(message, tg)
    else:
        log.info("Нет изменений — Telegram не отправляется (серверный режим).")

    log.info("State: %d записей.", len(merged_state))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vacancy Monitor")
    parser.add_argument(
        "--server",
        action="store_true",
        help="Серверный режим: Telegram только при новых/обновлённых вакансиях",
    )
    args = parser.parse_args()
    asyncio.run(main(server_mode=args.server))