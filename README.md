# Vacancy Monitor

Парсер вакансий с сайтов компаний. Использует Playwright для рендеринга
JavaScript-страниц (SPA/React/Vue) и отправляет уведомления в Telegram.

Включает веб-интерфейс для управления настройками и запуска парсера.

## Как это работает

Скрипт запускается, проходит по всем сайтам из `config.json`, сравнивает
найденные вакансии с сохранёнными в `state.json` и завершается.
Для периодического запуска используется планировщик (cron / systemd timer / Task Scheduler).
Веб-интерфейс дополняет CLI — парсер работает независимо от него.

## Быстрый старт

### 1. Установка зависимостей

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

На Linux-сервере без GUI — дополнительно:

```bash
python -m playwright install-deps chromium
```

### 2. Настройка

```bash
cp .env.example .env
cp config.example.json config.json
```

**`.env`** — секреты Telegram (не попадает в git):
```
TELEGRAM_TOKEN=токен_от_BotFather
TELEGRAM_CHAT_ID=твой_chat_id
# TELEGRAM_PROXY=http://127.0.0.1:12334  # если TG заблокирован
```

Как получить токен: напиши [@BotFather](https://t.me/BotFather) → `/newbot`.  
Как получить chat_id: напиши [@userinfobot](https://t.me/userinfobot).

**`config.json`** — список сайтов и ключевые слова (не попадает в git):
```json
{
  "target_urls": ["https://company.ru/vacancies"],
  "keywords": {
    "hard": ["devops", "docker", "linux"],
    "exclude": ["бухгалтер", "кассир"]
  },
  "timeouts": { "page_ms": 30000, "link_ms": 20000, "networkidle_ms": 5000, "inter_page_sec": 2.0 }
}
```

## Режимы запуска

### CLI — разовый запуск (с ПК, вручную)

```bash
python vacancy_monitor.py
```

Всегда присылает сводку в Telegram — даже если новых вакансий нет:
```
Vacancy Monitor (💻 разовый запуск)
✅ Новых вакансий нет
📊 Итог: 0 новых · 0 обновлено · 10 без изменений
```

Удобно чтобы убедиться что скрипт работает и Telegram доступен.

### CLI — серверный режим (автозапуск по расписанию)

```bash
python vacancy_monitor.py --server
```

Telegram **молчит** если новых вакансий нет. Пишет только при появлении
новой или изменившейся вакансии. Подходит для запуска каждые 2-3 часа —
не будет спамить пустыми сводками.

### Веб-интерфейс

```bash
python app.py
```

Открой `http://localhost:5000`. Позволяет:
- запустить разовую проверку и остановить её прямо из браузера
- редактировать список сайтов, ключевые слова и таймауты
- настроить Telegram (токен, chat_id, прокси)
- просматривать лог в реальном времени
- очистить историю вакансий или лог

Для автоматического запуска по расписанию используй планировщик ОС (см. ниже) —
веб-интерфейс для ручного управления, не для демона.

## Автоматизация

### Windows — Task Scheduler

1. Открой `taskschd.msc`
2. Создай задачу: запускать `python` с аргументом
   `C:\path\to\vacancy_monitor.py --server` каждые 3 часа

### Linux — cron

```bash
crontab -e
```

```cron
0 9,12,15,18,21 * * 1-5 cd /opt/vacancy-monitor && python3 vacancy_monitor.py --server
```

### Linux — systemd timer

`/etc/systemd/system/vacancy-monitor.service`:
```ini
[Unit]
Description=Vacancy Monitor

[Service]
Type=oneshot
WorkingDirectory=/opt/vacancy-monitor
ExecStart=/usr/bin/python3 vacancy_monitor.py --server
EnvironmentFile=/opt/vacancy-monitor/.env
```

`/etc/systemd/system/vacancy-monitor.timer`:
```ini
[Unit]
Description=Vacancy Monitor — запуск каждые 3 часа

[Timer]
OnCalendar=Mon-Fri 09,12,15,18,21:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now vacancy-monitor.timer
systemctl list-timers vacancy-monitor  # проверить расписание
```

## Структура файлов

```
vacancy-monitor/
├── vacancy_monitor.py      # парсер, запускается самостоятельно  (в git)
├── app.py                  # веб-интерфейс Flask                  (в git)
├── templates/              # HTML-шаблоны                         (в git)
├── static/                 # CSS и JS                             (в git)
├── config.example.json     # пример конфига                       (в git)
├── .env.example            # пример переменных окружения          (в git)
├── requirements.txt        # зависимости                          (в git)
├── .gitignore
├── README.md
│
├── config.json             # твои сайты и ключевые слова (не в git)
├── .env                    # токен и chat_id             (не в git)
└── state.json              # состояние парсера           (не в git, создаётся сам)
```

## state.json

Хранит MD5-хэш текста каждой найденной вакансии:

```json
{
  "https://company.ru/vacancy/devops": "a1b2c3d4...",
  "https://company.ru/vacancy/sysadmin": "e5f6g7h8..."
}
```

- Новый URL → алерт "новая вакансия"
- Хэш изменился → алерт "обновилась"
- Удали строку → получишь повторный алерт по этой вакансии
- Удали файл целиком → все вакансии придут заново как новые

## Прокси

Если Telegram заблокирован — укажи HTTP-прокси в `.env`:

```
TELEGRAM_PROXY=http://127.0.0.1:12334
```

В TUN-режиме (приложения перехватывают весь трафик) прокси не нужен — скрипт достучится до Telegram напрямую.
Если прокси указан, но TUN активен — скрипт автоматически попробует без прокси.

## Устранение неполадок

| Симптом | Решение |
|---|---|
| `config.json не найден` | `cp config.example.json config.json` и заполни |
| `TELEGRAM_TOKEN не задан` | Создай `.env` по образцу `.env.example` |
| Таймауты на сайтах | Увеличь `page_ms` до `45000` в `config.json` |
| Сайт не грузит вакансии | Увеличь `networkidle_ms` до `10000` в `config.json` |
| Telegram не отвечает | Добавь `TELEGRAM_PROXY` в `.env` или включи VPN |
| `playwright` не найден | `python -m playwright install chromium` |
| Хочу получить все вакансии заново | Удали `state.json` и запусти без `--server` |
| Веб-интерфейс не запускается | `pip install flask` |

## Архитектурные решения

**Playwright вместо requests/BeautifulSoup**
Большинство сайтов в списке используют React или Vue — статический парсер получает
пустую страницу до того как JS отработает. Playwright запускает настоящий Chromium
и ждёт завершения рендеринга. Проверял на sibirix.ru — без Playwright
контент не виден вообще.

**Двухэтапная фильтрация**
Сначала отбираем ссылки-кандидаты по title/href (быстро), потом проверяем полный
текст детальной страницы (точно). Это нужно потому что ссылка может называться
"SysOps-практики" и вести на статью в блоге, а не на вакансию. Исключения
(1С, бухгалтерия, торговля) проверяются до ключевых слов — иначе "автоматизация"
срабатывает на вакансии 1С-программиста.

**MD5 хэш текста а не HTML**
HTML страницы меняется постоянно — рекламные блоки, счётчики, CSRF-токены.
Если хэшировать HTML, скрипт будет детектировать "изменения" на каждом прогоне.
Берём только innerText body, нормализуем пробелы — хэш меняется только если
реально изменился текст вакансии.

**state.json вместо базы данных**
Для одного пользователя SQLite избыточен. JSON-файл с хэшами достаточен,
читается без зависимостей и легко редактируется вручную если нужно сбросить
конкретную вакансию. При масштабировании на нескольких пользователей —
заменить на SQLite.

**Два режима запуска (--server / разовый)**
На ПК удобно получать сводку при каждом запуске — убедиться что всё работает
и Telegram доступен. На сервере это спам каждые 3 часа. Флаг `--server`
отключает пустые уведомления — Telegram молчит пока нет новых вакансий.

**Веб-интерфейс дополняет, а не заменяет CLI**
`vacancy_monitor.py` работает самостоятельно — без Flask, через cron или вручную.
Flask добавляет удобный UI поверх: можно менять настройки не трогая файлы и
запустить проверку из браузера. Расписание — на стороне ОС, не внутри приложения.

**Секреты только в .env, не в config.json**
config.json содержит сайты и ключевые слова — его можно версионировать и шарить.
Токен и chat_id читаются исключительно из переменных окружения. Это исключает
случайный коммит секретов даже если забыть проверить .gitignore.