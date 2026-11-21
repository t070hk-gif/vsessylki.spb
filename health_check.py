#!/usr/bin/env python3
"""
Проверка здоровья ботов. Запуск по cron раз в 10 минут.

Предупреждения в личку (одна проблема — не чаще раза в час), и сообщение
«✅ восстановлено», когда проблема ушла:
  * прогоны расписания не завершаются дольше 30 минут (live_snapshot.json);
  * VK-бот не запущен (launchctl);
  * fhspb.ru / spb.fhr.ru / VK недоступны дольше 15 минут;
  * Telegram недоступен — сообщение придёт, когда связь вернётся.

Вечерняя сводка в 21:30: матчи дня, видео, посты, ошибки.

    python3 health_check.py            — обычная проверка (для cron)
    python3 health_check.py --report   — показать состояние и сводку в терминале, ничего не отправляя
    python3 health_check.py --summary  — отправить вечернюю сводку прямо сейчас
"""
import io
import json
import re
import subprocess
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
STATE_FILE = BASE / "health_state.json"
from config import TG_BOT_TOKEN  # секреты — в config.json
from config import ADMIN_ID
VK_LAUNCHD = "com.hockeyjunior.vkbot"

SCHEDULE_STALE_MIN = 30
SITE_DOWN_MIN = 15
ALERT_EVERY_SEC = 3600
SUMMARY_AT = (21, 30)

SITES = {
    "fhspb": ("fhspb.ru", "https://fhspb.ru/"),
    "spb": ("spb.fhr.ru", "https://spb.fhr.ru/"),
    "vk": ("VK", "https://api.vk.com/method/utils.getServerTime?v=5.199"),
}


# ----------------------------------------------------------------- утилиты
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"issues": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def reachable(url, timeout=15):
    try:
        requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        return True
    except Exception:
        return False


def send(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                          data={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20).json()
        return bool(r.get("ok"))
    except Exception:
        return False


def minutes(sec):
    m = int(sec // 60)
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


# ----------------------------------------------------------------- проверки

def _vk_auth_errors_recent(minutes=60) -> int:
    """Сколько error 5 / authorization failed в vkbot_log за последние N минут."""
    log = BASE / "vkbot_log.txt"
    if not log.exists():
        return 0
    cutoff = time.time() - minutes * 60
    # грубо: читаем хвост файла
    try:
        with open(log, "rb") as fh:
            fh.seek(max(0, log.stat().st_size - 2_000_000))
            tail = fh.read().decode("utf-8", errors="ignore")
    except Exception:
        return 0
    count = 0
    for line in tail.splitlines():
        if "error_code': 5" in line or "error_code\": 5" in line or "User authorization failed" in line:
            count += 1
    return count


def _pending_unresolved_counts():
    path = BASE / "state.json"
    if not path.exists():
        return 0, 0
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    return len(s.get("_pending_friendly_matches") or {}), len(s.get("_unresolved_videos") or {})


def current_problems():
    """{ключ: (текст, сколько минут ждать до предупреждения)} — то, что не так прямо сейчас."""
    problems = {}
    snap = BASE / "live_snapshot.json"
    age = (time.time() - snap.stat().st_mtime) / 60 if snap.exists() else 9999
    if age > SCHEDULE_STALE_MIN:
        problems["schedule"] = (f"Расписание: прогоны не завершаются уже {minutes(age * 60)} "
                                f"(посты дня не обновляются)", 0, "Расписание снова обновляется")
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        line = next((l for l in out.splitlines() if l.endswith(VK_LAUNCHD)), None)
        if not line or line.split()[0] == "-":
            problems["vkbot"] = ("VK-бот не запущен (видео не подхватываются)", 0, "VK-бот снова работает")
    except Exception:
        pass
    # stability: частые error 5 = токен мёртв
    auth_errs = _vk_auth_errors_recent(90)
    if auth_errs >= 15:
        problems["vk_token"] = (
            f"VK-токен сыпется (error 5 ×{auth_errs} за ~1.5 ч). Запусти get_vk_token.py",
            0,
            "VK-токен снова работает",
        )
    for key, (name, url) in SITES.items():
        if not reachable(url):
            problems[f"site_{key}"] = (f"{name} не отвечает", SITE_DOWN_MIN, f"{name} снова отвечает")
    return problems


def check():
    state = load_state()
    issues = state.setdefault("issues", {})
    now = time.time()
    telegram_ok = reachable(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe")

    # Telegram: если недоступен — запоминаем, сообщим, когда вернётся
    tg = state.get("telegram_down_since")
    if not telegram_ok:
        state.setdefault("telegram_down_since", now)
    elif tg:
        if now - tg >= SITE_DOWN_MIN * 60:
            send(f"⚠️ Telegram был недоступен с {datetime.fromtimestamp(tg):%d.%m %H:%M} "
                 f"до {datetime.now():%H:%M} ({minutes(now - tg)}). Посты в это время не публиковались.")
        state.pop("telegram_down_since", None)

    problems = current_problems()
    for key, (text, wait_min, recovered) in problems.items():
        it = issues.setdefault(key, {"since": now, "alerted": 0})
        it["text"], it["recovered"] = text, recovered
        lasted = now - it["since"]
        if lasted >= wait_min * 60 and now - it["alerted"] >= ALERT_EVERY_SEC and telegram_ok:
            if send(f"⚠️ {text}" + (f" — уже {minutes(lasted)}" if wait_min else "")):
                it["alerted"] = now
    for key in [k for k in issues if k not in problems]:
        it = issues.pop(key)
        if it.get("alerted") and telegram_ok:
            send(f"✅ {it.get('recovered', 'Восстановлено')} (проблема длилась {minutes(now - it['since'])})")

    if telegram_ok and datetime.now().strftime("%H:%M") >= f"{SUMMARY_AT[0]:02d}:{SUMMARY_AT[1]:02d}" \
            and state.get("summary_sent") != datetime.now().strftime("%d.%m.%Y"):
        if send(day_summary()):
            state["summary_sent"] = datetime.now().strftime("%d.%m.%Y")
    save_state(state)


# ----------------------------------------------------------------- вечерняя сводка
def day_summary(day=None):
    day = day or datetime.now().strftime("%d.%m.%Y")
    sys.path.insert(0, str(BASE))
    with redirect_stdout(io.StringIO()):
        import post_schedule as sched
    try:
        fixtures = [f for f in json.loads((BASE / "official_fixtures.json").read_text(encoding="utf-8"))
                    if f["date"] == day]
    except Exception:
        fixtures = []
    index = sched.load_video_index()
    without = []
    for f in sorted(fixtures, key=lambda x: x.get("time") or ""):
        base, age = f["key"], f.get("age")
        rivals = [g for g in fixtures if g["key"] == base and g.get("age") != age]
        entry = index.get(f"{base}::{age}") if age else None
        if not entry and base in index and not rivals:
            entry = index[base]
        ok = False
        if entry:
            with redirect_stdout(io.StringIO()):
                ok = sched._entry_matches_teams(entry, f["team1"], f["team2"])
        if not ok:
            without.append(f"{f.get('time') or '--:--'} {f['team1']} — {f['team2']} ({age or '?'})")

    try:
        posted = json.loads((BASE / "posted_days.json").read_text(encoding="utf-8"))
        posts = len([k for k in posted if day in k])
    except Exception:
        posts = "?"
    errors = 0
    log = BASE / "schedule_log.txt"
    if log.exists():
        with open(log, "rb") as fh:
            fh.seek(max(0, log.stat().st_size - 5_000_000))
            tail = fh.read().decode("utf-8", errors="ignore")
        errors = len(re.findall(rf"^\[{day[:5]} [^\]]*\] \[ERROR\]", tail, re.M))

    pending_n, unresolved_n = _pending_unresolved_counts()
    auth_errs = _vk_auth_errors_recent(24 * 60)
    if not fixtures:
        return (f"🌙 <b>Сводка за {day}</b>\nОфициальных матчей сегодня не было.\n"
                f"Pending: {pending_n}, unresolved: {unresolved_n}, VK error5 (сутки): {auth_errs}")
    lines = [f"🌙 <b>Сводка за {day}</b>",
             f"Матчей: {len(fixtures)}, с видео: {len(fixtures) - len(without)}, постов дня: {posts}",
             f"Ошибок в логе расписания: {errors}",
             f"Pending: {pending_n}, unresolved: {unresolved_n}, VK error5 (сутки): {auth_errs}"]
    if without:
        lines.append(f"\nБез видео ({len(without)}):")
        lines += [f"• {w}" for w in without[:15]]
        if len(without) > 15:
            lines.append(f"… и ещё {len(without) - 15}")
        lines.append("\nНайденное видео можно привязать, прислав ссылку сюда.")
    else:
        lines.append("Видео есть у всех матчей ✅")
    return "\n".join(lines)


def main():
    if "--report" in sys.argv:
        print("Проблемы сейчас:")
        problems = current_problems()
        for key, (text, wait, _) in problems.items():
            print(f"  • {text}" + (f" (предупреждение — если дольше {wait} мин)" if wait else ""))
        if not problems:
            print("  нет, всё работает")
        print("\n" + re.sub(r"<[^>]+>", "", day_summary()))
    elif "--summary" in sys.argv:
        print("Отправлено" if send(day_summary()) else "Не удалось отправить")
    else:
        check()


if __name__ == "__main__":
    main()
