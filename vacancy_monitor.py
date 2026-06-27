"""
Vacancy Monitor — парсер вакансий с сайтов компаний.

Режимы запуска:
  python vacancy_monitor.py           — разовый, всегда шлёт сводку в TG
  python vacancy_monitor.py --server  — серверный, TG только при новых/обновлённых

Файлы конфигурации:
  .env         — токен и chat_id Telegram (не в git)
  config.json  — сайты, ключевые слова, таймауты (не в git)
  state.json   — состояние парсера, создаётся автоматически (не в git)
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


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """
    Простой парсер .env без внешних зависимостей.
    Не перезаписывает переменные которые уже есть в окружении —
    это позволяет переопределять настройки через export перед запуском.
    """
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
            "Файл config.json не найден.\n"
            "Скопируй config.example.json → config.json и заполни своими данными."
        )
        sys.exit(1)

    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("config.json содержит ошибку JSON: %s", e)
        sys.exit(1)


def get_tg_settings() -> dict:
    """
    Секреты читаются только из окружения, никогда из config.json.
    Так токен не попадёт в git даже случайно.
    """
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    proxy   = os.getenv("TELEGRAM_PROXY",   "")

    errors = []
    if not token:
        errors.append("TELEGRAM_TOKEN не задан")
    if not chat_id:
        errors.append("TELEGRAM_CHAT_ID не задан")
    if errors:
        log.error(
            "Отсутствуют обязательные переменные: %s\n"
            "Создай .env по образцу .env.example",
            ", ".join(errors),
        )
        sys.exit(1)

    return {"token": token, "chat_id": chat_id, "proxy": proxy}


def load_state() -> dict:
    """state.json хранит хэши уже виденных вакансий между запусками."""
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


# ---------------------------------------------------------------------------
# Фильтрация вакансий
# ---------------------------------------------------------------------------

def _is_relevant(text: str, hard_kws: list, exclude_kws: list) -> tuple[bool, str]:
    """
    Двухэтапный фильтр:
    1. Сначала проверяем исключения — быстро отсекаем нерелевантные вакансии
       (1С-автоматизация, бухгалтерия и т.п. которые тоже содержат "инженер")
    2. Затем ищем жёсткие ключевые слова — конкретные технологии и роли
    Порядок важен: исключения имеют приоритет над совпадениями.
    """
    lower = text.lower()
    for ex in exclude_kws:
        if ex in lower:
            return False, ""
    for kw in hard_kws:
        if kw in lower:
            return True, kw
    return False, ""


def _text_hash(text: str) -> str:
    """
    Хэшируем нормализованный текст, а не HTML.
    HTML меняется от рекламных блоков и счётчиков даже если вакансия не изменилась,
    поэтому берём только видимый текст и убираем лишние пробелы.
    """
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _extract_context(text: str, keyword: str, max_len: int = 200) -> str:
    """
    Вырезает предложение где встретилось ключевое слово.
    Нужно чтобы в Telegram-уведомлении сразу было видно контекст —
    например "Требуется опыт работы с Docker от 1 года" вместо просто названия вакансии.
    """
    lower = text.lower()
    idx = lower.find(keyword.lower())
    if idx == -1:
        return text[:max_len].strip()

    # Ищем границу предложения слева — ближайшая точка или перенос строки до keyword
    left  = max(text.rfind(".", 0, idx), text.rfind("\n", 0, idx))
    start = left + 1 if left != -1 else max(0, idx - 120)

    # Граница справа — конец предложения после keyword
    right_dot = text.find(".", idx)
    right_nl  = text.find("\n", idx)
    candidates = [x for x in [right_dot, right_nl] if x != -1]
    end = min(candidates) + 1 if candidates else min(idx + 200, len(text))

    fragment = re.sub(r"\s+", " ", text[start:end].strip())
    if len(fragment) > max_len:
        fragment = fragment[:max_len].rstrip() + "…"
    return fragment


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

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
                    log.error("Telegram error %s: %s", resp.status_code, resp.text[:200])
                    return False
                log.info("Telegram: отправлено.")
                return True
        except TypeError:
            # httpx < 0.27 использует proxies= вместо proxy=
            # оставляем совместимость чтобы не требовать обновления
            old: dict = {"timeout": 15}
            if use_proxy:
                old["proxies"] = use_proxy
            async with httpx.AsyncClient(**old) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    log.error("Telegram error %s: %s", resp.status_code, resp.text[:200])
                    return False
                log.info("Telegram: отправлено.")
                return True

    try:
        return await _try(proxy)
    except Exception as exc:
        if proxy:
            # В TUN-режиме (Hiddify/WireGuard перехватывают весь трафик)
            # прокси не нужен — система сама заворачивает трафик в туннель.
            # Если прокси указан но недоступен — пробуем без него.
            log.warning("Прокси недоступен (%s), пробую без прокси...", exc)
            try:
                return await _try(None)
            except Exception as exc2:
                log.error("Telegram недоступен: %s", exc2)
                return False
        log.error("Telegram недоступен: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Playwright — загрузка страниц
# ---------------------------------------------------------------------------

async def _safe_goto(page: Page, url: str, timeout: int) -> bool:
    """
    Загружает страницу и ждёт networkidle для завершения JS-рендеринга.
    networkidle опциональный — некоторые SPA никогда его не достигают,
    поэтому таймаут на нём не считаем ошибкой.
    """
    try:
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeout:
            pass  # domcontentloaded достаточно для большинства страниц
        return True
    except PlaywrightTimeout:
        log.warning("Таймаут: %s", url)
        return False
    except Exception as exc:
        log.error("Ошибка загрузки %s: %s", url, exc)
        return False


async def _get_page_text(page: Page) -> str:
    """innerText вместо page.content() — получаем только видимый текст без тегов."""
    try:
        return await page.inner_text("body")
    except Exception:
        return await page.content()


async def _collect_links(page: Page, base_url: str) -> list[dict]:
    """
    Собирает все ссылки страницы и приводит href к абсолютному виду.
    Относительные пути (/vacancy/123) дополняем схемой и доменом базового URL.
    Якоря (#section) и javascript: пропускаем.
    """
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
                continue
            if abs_href in seen:
                continue
            seen.add(abs_href)
            results.append({"href": abs_href, "title": title or href})
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# Логика проверки одного URL
# ---------------------------------------------------------------------------

async def check_url(
    page: Page,
    base_url: str,
    state: dict,
    new_state: dict,
    cfg: dict,
) -> list[dict]:
    """
    Стратегия парсинга:
    - Если на странице есть ссылки с ключевыми словами в title/href —
      заходим на каждую и проверяем полный текст детальной страницы.
      Это точнее: ссылка могла называться "DevOps" но вести на нерелевантную страницу.
    - Если ссылок-кандидатов нет — проверяем текст самой страницы целиком
      (случай когда все вакансии на одной странице без отдельных URL).
    """
    hard_kws    = cfg["keywords"]["hard"]
    exclude_kws = cfg["keywords"]["exclude"]
    timeouts    = cfg["timeouts"]

    log.info("Проверяю: %s", base_url)
    found: list[dict] = []

    ok = await _safe_goto(page, base_url, timeouts["page_ms"])
    if not ok:
        return found

    all_links = await _collect_links(page, base_url)

    # Первичный отбор по title/href — не финальный, детальная проверка ниже
    candidate_links = [
        lnk for lnk in all_links
        if any(kw in f"{lnk['title']} {lnk['href']}".lower() for kw in hard_kws)
    ]

    if candidate_links:
        log.info("  Кандидатов: %d", len(candidate_links))
        for lnk in candidate_links:
            href  = lnk["href"]
            title = lnk["title"]

            await asyncio.sleep(0.5)  # вежливая пауза между запросами
            detail_ok = await _safe_goto(page, href, timeouts["link_ms"])
            if not detail_ok:
                continue

            detail_text = await _get_page_text(page)

            # Финальная проверка по полному тексту страницы вакансии
            relevant, kw = _is_relevant(detail_text, hard_kws, exclude_kws)
            if not relevant:
                log.info("  Пропускаю: %s", title[:60])
                try:
                    await page.go_back()
                except Exception:
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

            found.append({
                "title":   title,
                "url":     href,
                "keyword": kw,
                "status":  status,
                "context": context,
            })

            try:
                await page.go_back()
            except Exception:
                # go_back() падает если страница открылась в новой вкладке
                # или был редирект — просто возвращаемся на базовый URL
                await _safe_goto(page, base_url, timeouts["page_ms"])
            await asyncio.sleep(0.3)

    else:
        # Нет отдельных страниц вакансий — проверяем текущую страницу целиком
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

            found.append({
                "title":   base_url,
                "url":     base_url,
                "keyword": kw,
                "status":  status,
                "context": context,
            })
        else:
            log.info("  Ничего релевантного")

    return found


# ---------------------------------------------------------------------------
# Формирование Telegram-сообщения
# ---------------------------------------------------------------------------

def build_message(all_found: list[dict], server_mode: bool) -> Optional[str]:
    new_items     = [v for v in all_found if v["status"] == "new"]
    updated_items = [v for v in all_found if v["status"] == "updated"]
    seen_count    = sum(1 for v in all_found if v["status"] == "seen")
    has_changes   = bool(new_items or updated_items)

    # В серверном режиме молчим если нет изменений — не спамим пустыми сводками
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
            # Выделяем ключевое слово жирным чтобы сразу было видно в контексте
            highlighted = re.sub(
                f"({re.escape(kw)})",
                r"<b>\1</b>",
                context,
                flags=re.IGNORECASE,
                count=1,
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


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

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
    log.info("Прокси Telegram:  %s", tg["proxy"] or "нет (TUN/прямой доступ)")

    async with async_playwright() as pw:
        # headless=True — браузер без GUI, работает на серверах без дисплея
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

    # Объединяем старый state с новым.
    # Ключи которые не встретились в этом прогоне остаются —
    # вакансия могла просто не загрузиться из-за таймаута.
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