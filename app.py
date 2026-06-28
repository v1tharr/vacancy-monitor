"""
Flask web UI для Vacancy Monitor.
Дополняет CLI - парсер vacancy_monitor.py работает независимо.

Запуск: python app.py
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "state.json"
ENV_FILE    = BASE_DIR / ".env"
LOG_FILE    = BASE_DIR / "vacancy_monitor.log"
PID_FILE    = BASE_DIR / "parser.pid"

app = Flask(__name__)


def _load_dotenv() -> dict:
    result = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _save_dotenv(data: dict) -> None:
    lines = [f"{k}={v}" for k, v in data.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {
            "target_urls": [],
            "keywords": {"hard": [], "exclude": []},
            "timeouts": {
                "page_ms": 30000,
                "link_ms": 20000,
                "networkidle_ms": 5000,
                "inter_page_sec": 2.0,
            },
        }
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _last_run_info() -> dict:
    if not LOG_FILE.exists():
        return {"time": None, "new": 0, "updated": 0}
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if "готово. новых:" in line.lower():
            try:
                time_str = line.split(" [")[0]
                parts = line.lower().split("новых: ")[-1]
                new_count = int(parts.split(",")[0])
                updated_count = int(parts.split("обновлено: ")[-1].strip())
                return {"time": time_str, "new": new_count, "updated": updated_count}
            except Exception:
                pass
    return {"time": None, "new": 0, "updated": 0}


def _is_process_alive(pid: int) -> bool:
    """кроссплатформенная проверка живости процесса."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False


def _parser_status() -> dict:
    if not PID_FILE.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return {"running": False, "pid": None}

    if not _is_process_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return {"running": False, "pid": None}

    return {"running": True, "pid": pid}


@app.route("/")
def index():
    state  = _load_state()
    last   = _last_run_info()
    cfg    = _load_config()
    status = _parser_status()
    return render_template(
        "index.html",
        state_count=len(state),
        last_run=last,
        url_count=len(cfg.get("target_urls", [])),
        kw_count=len(cfg.get("keywords", {}).get("hard", [])),
        parser_running=status["running"],
    )


@app.route("/run", methods=["POST"])
def run():
    if _parser_status()["running"]:
        return jsonify({"error": "парсер уже запущен"}), 409

    script = BASE_DIR / "vacancy_monitor.py"
    cmd    = [sys.executable, str(script)]

    try:
        kwargs: dict = {
            "cwd": str(BASE_DIR),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP изолирует группу процессов —
            # taskkill по PID не затронет Flask
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        proc = subprocess.Popen(cmd, **kwargs)
        PID_FILE.write_text(str(proc.pid))
        return jsonify({"status": "started", "pid": proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stop", methods=["POST"])
def stop():
    status = _parser_status()
    if not status["running"]:
        return jsonify({"status": "stopped"})  # уже не работает - не ошибка
    try:
        pid = status["pid"]
        if sys.platform == "win32":
            # /T завершает дерево процессов, /F принудительно
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True
            )
        else:
            os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/status")
def parser_status_api():
    return jsonify(_parser_status())


@app.route("/log")
def log_page():
    return render_template("log.html")


@app.route("/log/data")
def log_data():
    lines_count = int(request.args.get("lines", 100))
    if not LOG_FILE.exists():
        return jsonify({"lines": []})
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return jsonify({"lines": lines[-lines_count:]})


@app.route("/clear/state", methods=["POST"])
def clear_state():
    try:
        STATE_FILE.unlink(missing_ok=True)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/clear/log", methods=["POST"])
def clear_log():
    try:
        LOG_FILE.write_text("", encoding="utf-8")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = _load_config()
    env = _load_dotenv()

    if request.method == "POST":
        section = request.form.get("section")

        if section == "telegram":
            env["TELEGRAM_TOKEN"]   = request.form.get("token", "").strip()
            env["TELEGRAM_CHAT_ID"] = request.form.get("chat_id", "").strip()
            proxy = request.form.get("proxy", "").strip()
            if proxy:
                env["TELEGRAM_PROXY"] = proxy
            elif "TELEGRAM_PROXY" in env:
                del env["TELEGRAM_PROXY"]
            _save_dotenv(env)
            return redirect(url_for("settings", tab="telegram", success="Настройки Telegram сохранены"))

        elif section == "keywords":
            cfg["keywords"]["hard"]    = [w.strip() for w in request.form.get("hard", "").splitlines() if w.strip()]
            cfg["keywords"]["exclude"] = [w.strip() for w in request.form.get("exclude", "").splitlines() if w.strip()]
            _save_config(cfg)
            return redirect(url_for("settings", tab="keywords", success="Ключевые слова сохранены"))

        elif section == "urls":
            cfg["target_urls"] = [u.strip() for u in request.form.get("urls", "").splitlines() if u.strip()]
            _save_config(cfg)
            return redirect(url_for("settings", tab="urls", success="Список сайтов сохранён"))

        elif section == "timeouts":
            try:
                cfg["timeouts"]["page_ms"]       = int(request.form.get("page_ms", 30000))
                cfg["timeouts"]["link_ms"]        = int(request.form.get("link_ms", 20000))
                cfg["timeouts"]["networkidle_ms"] = int(request.form.get("networkidle_ms", 5000))
                cfg["timeouts"]["inter_page_sec"] = float(request.form.get("inter_page_sec", 2.0))
                _save_config(cfg)
                return redirect(url_for("settings", tab="timeouts", success="Таймауты сохранены"))
            except ValueError:
                return redirect(url_for("settings", tab="timeouts", error="Некорректные значения таймаутов"))

        elif section == "danger":
            # danger actions handled via JS/fetch, not form POST
            pass

    return render_template(
        "settings.html",
        cfg=cfg,
        env=env,
        active_tab=request.args.get("tab", "telegram"),
        success=request.args.get("success"),
        error=request.args.get("error"),
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)