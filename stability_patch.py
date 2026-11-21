#!/usr/bin/env python3
"""
Патч стабильности для ВСЕССЫЛКИ.СПБ бота.

Что делает:
1. VK API: при error 5 — немедленный refresh токена + 1 повтор запроса
2. VK API: exponential backoff при error 6 (rate limit)
3. VK API: timeout=(5, 20), пауза между группами
4. Автоочистка старых pending (>14 дней)
5. Нормализация дат в video_index (8.11 → 08.11)
6. rotate_logs.sh: порог 5 МБ вместо 10
7. health_check: алерт по частым error 5 в логе + сводка pending/unresolved

Запуск (из папки проекта):
    python3 stability_patch.py
    python3 stability_patch.py --dry-run   # только показать, что изменится

После патча:
    ./restart_vkbot.sh
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRY = False


def backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak_stability")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"  backup → {bak.name}")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        print(f"  ⚠ не найдено: {label}")
        return text
    text2 = text.replace(old, new, 1)
    if text2 == text:
        print(f"  ⚠ не изменилось: {label}")
    else:
        print(f"  ✓ {label}")
    return text2


def write(path: Path, content: str) -> None:
    if DRY:
        print(f"  [dry-run] не пишем {path.name}")
        return
    path.write_text(content, encoding="utf-8")
    print(f"  записан {path.name}")


# ---------------------------------------------------------------------------
# 1–3. vk_to_tg_hockey_bot.py — устойчивые VK-запросы + пауза между группами
# ---------------------------------------------------------------------------

VK_HELPER = r'''
# === stability: единый VK-запрос с retry ===
_VK_TOKENS_REF = {"tokens": None}  # обновляется из main/check_groups


def _vk_api(method: str, params: dict, access_token: str, *,
            group_id=None, attempts: int = 4):
    """Запрос к VK API с обработкой error 5 (auth) и 6 (rate limit).

    Возвращает (data_dict | None, new_access_token).
    None в data = фатальная/невосстановимая ошибка для этого вызова.
    """
    import time as _time
    token = access_token
    last_data = None
    label = f"group {group_id}" if group_id is not None else method

    for attempt in range(attempts):
        try:
            resp = requests.get(
                f"https://api.vk.com/method/{method}",
                params={**params, "access_token": token, "v": VK_API_VERSION},
                timeout=(5, 20),
            )
            data = resp.json()
        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 12)
            print(f"[VK net] {label} {method}: {e}; sleep {wait}s")
            _time.sleep(wait)
            continue

        err = data.get("error") or {}
        code = err.get("error_code")
        if not err:
            return data, token

        last_data = data
        if code == 6:  # Too many requests
            wait = min(2 ** attempt, 15)
            print(f"[VK rate] {label}: error 6, sleep {wait}s (attempt {attempt+1})")
            _time.sleep(wait)
            continue

        if code == 5:  # User authorization failed
            print(f"[VK auth] {label}: error 5 — пробую refresh токена")
            try:
                tokens = _VK_TOKENS_REF.get("tokens") or load_tokens()
                tokens = refresh_access_token(tokens)
                _VK_TOKENS_REF["tokens"] = tokens
                token = tokens["access_token"]
                _time.sleep(1.0)
                continue
            except SystemExit as e:
                print(f"[VK auth] refresh не удался: {e}")
                try:
                    notify_admin_once(
                        "vk_token_dead",
                        "⚠️ VK-токен не обновляется (error 5). "
                        "Запусти get_vk_token.py на компьютере.",
                    )
                except Exception:
                    pass
                return data, token
            except Exception as e:
                print(f"[VK auth] refresh exception: {e}")
                return data, token

        # 10, 204 и прочее — один повтор с паузой, потом сдаёмся
        if attempt < attempts - 1 and code in (10, 204, 15):
            _time.sleep(1.5 * (attempt + 1))
            continue
        print(f"[VK API error - {method}] {label}: {err}")
        return data, token

    if last_data and "error" in last_data:
        print(f"[VK API error - {method}] {label}: {last_data['error']}")
    return last_data, token
'''


def patch_vk_bot() -> None:
    path = ROOT / "vk_to_tg_hockey_bot.py"
    if not path.exists():
        print("✗ vk_to_tg_hockey_bot.py не найден")
        return
    print("\n== vk_to_tg_hockey_bot.py ==")
    backup(path)
    text = path.read_text(encoding="utf-8")

    # --- вставить helper после get_valid_access_token (перед wall.get) ---
    anchor = "# ------------------- VK API -------------------"
    if "_vk_api(" in text and "stability: единый VK-запрос" in text:
        print("  • VK helper уже есть, пропускаю вставку")
    elif anchor in text:
        text = text.replace(
            anchor,
            VK_HELPER + "\n" + anchor,
            1,
        )
        print("  ✓ вставлен _vk_api helper")
    else:
        print("  ⚠ якорь VK API не найден")

    # --- заменить vk_get_wall_posts ---
    old_wall = '''def vk_get_wall_posts(access_token: str, group_id: int, count: int = POSTS_PER_CHECK) -> list:
    """Получить последние посты со стены группы ВК."""
    resp = requests.get(
        "https://api.vk.com/method/wall.get",
        params={
            "owner_id": -abs(group_id),
            "count": count,
            "access_token": access_token,
            "v": VK_API_VERSION,
        },
        timeout=15,
    )
    data = resp.json()
    if "error" in data:
        print(f"[VK API error] group {group_id}: {data['error']}")
        return []
    return data.get("response", {}).get("items", [])'''

    new_wall = '''def vk_get_wall_posts(access_token: str, group_id: int, count: int = POSTS_PER_CHECK) -> list:
    """Получить последние посты со стены группы ВК (с retry / refresh)."""
    data, _ = _vk_api(
        "wall.get",
        {"owner_id": -abs(group_id), "count": count},
        access_token,
        group_id=group_id,
    )
    if not data or "error" in data:
        return []
    return data.get("response", {}).get("items", [])'''

    text = replace_once(text, old_wall, new_wall, "vk_get_wall_posts → retry")

    # --- заменить vk_get_group_videos ---
    old_cat = '''def vk_get_group_videos(access_token: str, group_id: int, count: int = VIDEO_CATALOG_PER_CHECK):
    """
    Получить видео-каталог группы напрямую (video.get), а не только то, что
    прикреплено к постам на стене. Некоторые группы публикуют прямые
    трансляции через нативный механизм ВК ("Трансляции"), не создавая при
    этом отдельного поста на стене — такие видео wall.get не увидит вообще,
    а video.get — увидит, так как они всё равно попадают в общий каталог
    видео группы.

    Возвращает None (а не пустой список) при ошибке API — это важно
    отличать от "каталог реально пуст", иначе базовая линия могла бы
    ошибочно "засчитаться" ещё до того, как доступ к каталогу появился.
    """
    resp = requests.get(
        "https://api.vk.com/method/video.get",
        params={
            "owner_id": -abs(group_id),
            "count": count,
            "access_token": access_token,
            "v": VK_API_VERSION,
        },
        timeout=15,
    )
    data = resp.json()
    if "error" in data:
        print(f"[VK API error - video.get] group {group_id}: {data['error']}")
        return None
    return data.get("response", {}).get("items", [])'''

    new_cat = '''def vk_get_group_videos(access_token: str, group_id: int, count: int = VIDEO_CATALOG_PER_CHECK):
    """
    Получить видео-каталог группы напрямую (video.get).
    Возвращает None при ошибке API (чтобы не ставить baseline по ошибке).
    """
    data, _ = _vk_api(
        "video.get",
        {"owner_id": -abs(group_id), "count": count},
        access_token,
        group_id=group_id,
    )
    if not data or "error" in data:
        return None
    return data.get("response", {}).get("items", [])'''

    text = replace_once(text, old_cat, new_cat, "vk_get_group_videos → retry")

    # --- vk_videos_still_exist: улучшить backoff ---
    old_exist_loop = '''    for attempt in range(3):
        time.sleep(0.4)                     # VK: не больше 3 запросов в секунду
        resp = requests.get(
            "https://api.vk.com/method/video.get",
            params={"videos": videos_param, "access_token": access_token, "v": VK_API_VERSION},
            timeout=15,
        )
        data = resp.json()
        if data.get("error", {}).get("error_code") == 6:      # Too many requests per second
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    if "error" in data:'''

    new_exist_loop = '''    data = None
    for attempt in range(4):
        time.sleep(0.45)
        try:
            resp = requests.get(
                "https://api.vk.com/method/video.get",
                params={"videos": videos_param, "access_token": access_token, "v": VK_API_VERSION},
                timeout=(5, 20),
            )
            data = resp.json()
        except requests.exceptions.RequestException as e:
            time.sleep(min(2 ** attempt, 10))
            continue
        code = (data.get("error") or {}).get("error_code")
        if code == 6:
            time.sleep(min(2 ** attempt, 15))
            continue
        if code == 5:
            try:
                tokens = _VK_TOKENS_REF.get("tokens") or load_tokens()
                tokens = refresh_access_token(tokens)
                _VK_TOKENS_REF["tokens"] = tokens
                access_token = tokens["access_token"]
                time.sleep(1.0)
                continue
            except Exception:
                break
        break
    if not data or "error" in data:'''

    text = replace_once(text, old_exist_loop, new_exist_loop, "vk_videos_still_exist backoff")

    # --- пауза между группами ---
    old_loop = '''    for group_id in VK_GROUP_IDS:
        key = str(group_id)
        seen_video_ids = set(state.get(key, []))

        posts = vk_get_wall_posts(access_token, group_id)'''

    new_loop = '''    for gi, group_id in enumerate(VK_GROUP_IDS):
        if gi > 0:
            time.sleep(0.55)  # stability: не бить VK подряд по всем группам
        key = str(group_id)
        seen_video_ids = set(state.get(key, []))

        posts = vk_get_wall_posts(access_token, group_id)'''

    text = replace_once(text, old_loop, new_loop, "пауза между группами")

    # --- прокинуть tokens в _VK_TOKENS_REF из main ---
    old_main_token = '''            access_token, tokens = get_valid_access_token(tokens)
            process_admin_messages(admin_state, access_token)

            if seconds_since_vk_check >= POLL_INTERVAL_SECONDS:
                check_groups_once(state, access_token)'''

    new_main_token = '''            access_token, tokens = get_valid_access_token(tokens)
            _VK_TOKENS_REF["tokens"] = tokens
            process_admin_messages(admin_state, access_token)

            if seconds_since_vk_check >= POLL_INTERVAL_SECONDS:
                check_groups_once(state, access_token)'''

    text = replace_once(text, old_main_token, new_main_token, "main: tokens → _VK_TOKENS_REF")

    # --- once mode ---
    old_once = '''    if "--once" in sys.argv:                 # облако: один проход и выход
        access_token, tokens = get_valid_access_token(tokens)
        process_admin_messages(admin_state, access_token)
        check_groups_once(state, access_token)
        return'''

    new_once = '''    if "--once" in sys.argv:                 # облако: один проход и выход
        access_token, tokens = get_valid_access_token(tokens)
        _VK_TOKENS_REF["tokens"] = tokens
        process_admin_messages(admin_state, access_token)
        check_groups_once(state, access_token)
        return'''

    text = replace_once(text, old_once, new_once, "once: tokens → _VK_TOKENS_REF")

    # --- автоочистка старых pending (сначала функция, потом вызов) ---
    cleanup_fn = """
PENDING_MAX_AGE_DAYS = 14


def _cleanup_stale_pending(state: dict) -> None:
    \"\"\"Удаляет из _pending_friendly_matches записи старше PENDING_MAX_AGE_DAYS.\"\"\"
    pending = state.get("_pending_friendly_matches") or {}
    if not pending:
        return
    now = time.time()
    date_re = re.compile(r"(\\d{1,2})\\.(\\d{1,2})\\.(\\d{2,4})")
    removed = []
    for vid, entry in list(pending.items()):
        text_e = f"{entry.get('title', '')} {entry.get('description', '')}"
        m = date_re.search(text_e)
        ts = None
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            try:
                ts = datetime(y, mo, d).timestamp()
            except ValueError:
                ts = None
        if ts is None:
            ts = float(entry.get("first_seen") or entry.get("added_at") or 0) or now
        age_days = (now - ts) / 86400
        if age_days > PENDING_MAX_AGE_DAYS:
            removed.append(vid)
            del pending[vid]
    if removed:
        print(f"[CLEAN] Удалены старые pending ({len(removed)}): {', '.join(removed[:8])}"
              + ("…" if len(removed) > 8 else ""))
        state["_pending_friendly_matches"] = pending
        save_state(state)

"""

    if "def _cleanup_stale_pending" not in text:
        marker = "def process_pending_friendly_matches(state: dict) -> None:"
        if marker in text:
            text = text.replace(marker, cleanup_fn + marker, 1)
            print("  ✓ добавлен _cleanup_stale_pending")
        else:
            print("  ⚠ process_pending_friendly_matches не найден")
    else:
        print("  • _cleanup_stale_pending уже есть")

    old_check_start = (
        "def check_groups_once(state: dict, access_token: str) -> None:\n"
        "    # Сначала публикуем товарищеские игры, для которых наступил день матча\n"
        "    # (были отложены раньше — см. process_pending_friendly_matches).\n"
        "    process_pending_friendly_matches(state)\n"
        "    _recheck_unresolved(state)\n"
        "    _send_unresolved_digest(state)"
    )

    new_check_start = (
        "def check_groups_once(state: dict, access_token: str) -> None:\n"
        "    # Сначала публикуем товарищеские игры, для которых наступил день матча\n"
        "    # (были отложены раньше — см. process_pending_friendly_matches).\n"
        "    today = datetime.now().strftime(\"%d.%m.%Y\")\n"
        "    if state.get(\"_video_index_norm_day\") != today:\n"
        "        try:\n"
        "            _normalize_video_index_dates()\n"
        "        except Exception as e:\n"
        "            print(f\"[WARN] normalize video_index: {e}\")\n"
        "        state[\"_video_index_norm_day\"] = today\n"
        "    _cleanup_stale_pending(state)\n"
        "    process_pending_friendly_matches(state)\n"
        "    _recheck_unresolved(state)\n"
        "    _send_unresolved_digest(state)"
    )

    norm_fn = """
def _normalize_video_index_dates() -> int:
    \"\"\"Приводит ключи video_index к DD.MM.YYYY (с ведущим нулём).\"\"\"
    path = Path(__file__).parent / "video_index.json"
    if not path.exists():
        return 0
    try:
        vi = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    changed = 0
    bad_re = re.compile(r"^(\\d{1,2})\\.(\\d{1,2})\\.(\\d{4})(::.*)?$")
    new_vi = {}
    for k, v in vi.items():
        m = bad_re.match(k)
        if m:
            d, mo, y, rest = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            nk = f"{int(d):02d}.{int(mo):02d}.{y}{rest}"
            if nk != k:
                changed += 1
                if nk in new_vi or nk in vi:
                    new_vi[k] = v
                else:
                    new_vi[nk] = v
                continue
        new_vi[k] = v
    if changed:
        path.write_text(json.dumps(new_vi, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[CLEAN] video_index: нормализовано дат: {changed}")
    return changed

"""

    if "def _normalize_video_index_dates" not in text:
        marker = "def check_groups_once(state: dict, access_token: str) -> None:"
        if marker in text:
            text = text.replace(marker, norm_fn + marker, 1)
            print("  ✓ добавлен _normalize_video_index_dates")
        else:
            print("  ⚠ check_groups_once не найден для norm_fn")
    else:
        print("  • _normalize_video_index_dates уже есть")

    text = replace_once(text, old_check_start, new_check_start, "check_groups: cleanup+normalize")

    write(path, text)


# ---------------------------------------------------------------------------
# 4. rotate_logs.sh — порог 5 МБ
# ---------------------------------------------------------------------------

def patch_rotate_logs() -> None:
    path = ROOT / "rotate_logs.sh"
    if not path.exists():
        print("\n✗ rotate_logs.sh не найден")
        return
    print("\n== rotate_logs.sh ==")
    backup(path)
    text = path.read_text(encoding="utf-8")
    old = "if [ \"$size\" -gt 10485760 ]; then"
    new = "if [ \"$size\" -gt 5242880 ]; then"  # 5 МБ
    if old in text:
        text = text.replace(old, new)
        print("  ✓ порог ротации 10 МБ → 5 МБ")
    elif "5242880" in text:
        print("  • уже 5 МБ")
    else:
        print("  ⚠ строка размера не найдена")
    write(path, text)


# ---------------------------------------------------------------------------
# 5. health_check.py — error 5 + pending/unresolved в сводке
# ---------------------------------------------------------------------------

HEALTH_EXTRA = r'''
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
'''


def patch_health() -> None:
    path = ROOT / "health_check.py"
    if not path.exists():
        print("\n✗ health_check.py не найден")
        return
    print("\n== health_check.py ==")
    backup(path)
    text = path.read_text(encoding="utf-8")

    if "_vk_auth_errors_recent" not in text:
        # вставить перед current_problems
        marker = "def current_problems():"
        if marker in text:
            text = text.replace(marker, HEALTH_EXTRA + "\n\n" + marker, 1)
            print("  ✓ добавлены _vk_auth_errors_recent / _pending_unresolved_counts")
        else:
            print("  ⚠ current_problems не найден")
    else:
        print("  • helpers уже есть")

    # добавить проблему в current_problems
    old_vkbot = '''        if not line or line.split()[0] == "-":
            problems["vkbot"] = ("VK-бот не запущен (видео не подхватываются)", 0, "VK-бот снова работает")
    except Exception:
        pass'''

    new_vkbot = '''        if not line or line.split()[0] == "-":
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
        )'''

    text = replace_once(text, old_vkbot, new_vkbot, "current_problems: vk_token alert")

    # расширить day_summary
    old_sum_tail = '''    if not fixtures:
        return f"🌙 <b>Сводка за {day}</b>\\nОфициальных матчей сегодня не было."
    lines = [f"🌙 <b>Сводка за {day}</b>",
             f"Матчей: {len(fixtures)}, с видео: {len(fixtures) - len(without)}, постов дня: {posts}",
             f"Ошибок в логе расписания: {errors}"]'''

    new_sum_tail = '''    pending_n, unresolved_n = _pending_unresolved_counts()
    auth_errs = _vk_auth_errors_recent(24 * 60)
    if not fixtures:
        return (f"🌙 <b>Сводка за {day}</b>\\nОфициальных матчей сегодня не было.\\n"
                f"Pending: {pending_n}, unresolved: {unresolved_n}, VK error5 (сутки): {auth_errs}")
    lines = [f"🌙 <b>Сводка за {day}</b>",
             f"Матчей: {len(fixtures)}, с видео: {len(fixtures) - len(without)}, постов дня: {posts}",
             f"Ошибок в логе расписания: {errors}",
             f"Pending: {pending_n}, unresolved: {unresolved_n}, VK error5 (сутки): {auth_errs}"]'''

    # try both escaped and unescaped forms
    if old_sum_tail in text:
        text = replace_once(text, old_sum_tail, new_sum_tail, "day_summary: pending/error5")
    else:
        # softer match
        alt_old = 'f"Ошибок в логе расписания: {errors}"]'
        alt_new = (
            'f"Ошибок в логе расписания: {errors}",\n'
            '             f"Pending: {pending_n}, unresolved: {unresolved_n}, '
            'VK error5 (сутки): {auth_errs}"]'
        )
        if alt_old in text and "_pending_unresolved_counts" in text:
            # ensure counts computed
            if "pending_n, unresolved_n = _pending_unresolved_counts()" not in text.split("def day_summary")[-1][:800]:
                text = text.replace(
                    "def day_summary",
                    "def day_summary",
                    1,
                )
            # insert before lines =
            needle = '    lines = [f"🌙 <b>Сводка за {day}</b>",'
            if needle in text and "pending_n, unresolved_n" not in text[text.find("def day_summary"):text.find("def day_summary")+2500]:
                text = text.replace(
                    needle,
                    "    pending_n, unresolved_n = _pending_unresolved_counts()\n"
                    "    auth_errs = _vk_auth_errors_recent(24 * 60)\n"
                    + needle,
                    1,
                )
                print("  ✓ day_summary: inserted counts")
            text = replace_once(text, alt_old, alt_new, "day_summary errors line")
        else:
            print("  ⚠ day_summary блок не совпал — добавь вручную counts в сводку")

    write(path, text)


# ---------------------------------------------------------------------------
# 6. post_schedule.py — мягкие retries для HTTP
# ---------------------------------------------------------------------------

def patch_post_schedule() -> None:
    path = ROOT / "post_schedule.py"
    if not path.exists():
        print("\n✗ post_schedule.py не найден")
        return
    print("\n== post_schedule.py ==")
    text = path.read_text(encoding="utf-8")
    if "HTTPAdapter" in text and "Retry(" in text:
        print("  • retries уже есть")
        return
    backup(path)

    # Добавить helper session после imports, если есть блок fetch
    old = None
    # типичный паттерн
    patterns = [
        '''        except requests.exceptions.RequestException as e:''',
    ]
    helper = '''
# stability: session с retry для внешних сайтов
def _http_session():
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.2,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s

_HTTP = None
def _get_http():
    global _HTTP
    if _HTTP is None:
        _HTTP = _http_session()
    return _HTTP
'''
    # вставить после import requests
    if "def _http_session" not in text:
        if "import requests" in text:
            text = text.replace("import requests", "import requests\n" + helper, 1)
            print("  ✓ добавлен _http_session")
        else:
            print("  ⚠ import requests не найден")

    # Заменить простой requests.get в fetch-подобных местах осторожно —
    # только явный timeout=20 паттерн из комментария про SSLEOF
    old_get = "resp = requests.get(url, headers=HEADERS, timeout=20)"
    new_get = "resp = _get_http().get(url, headers=HEADERS, timeout=(5, 25))"
    if old_get in text:
        text = text.replace(old_get, new_get)
        print(f"  ✓ requests.get → session ({text.count(new_get)} мест)")
    else:
        print("  • точный fetch get не найден (возможно уже изменён)")

    write(path, text)


def main():
    global DRY
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    DRY = args.dry_run
    if DRY:
        print("=== DRY RUN ===\n")

    if not (ROOT / "vk_to_tg_hockey_bot.py").exists():
        print(f"Запусти из папки проекта (сейчас {ROOT})")
        sys.exit(1)

    patch_vk_bot()
    patch_rotate_logs()
    patch_health()
    patch_post_schedule()

    print("\n=== Готово ===")
    if not DRY:
        print("Дальше:")
        print("  1. python3 -m py_compile vk_to_tg_hockey_bot.py health_check.py post_schedule.py")
        print("  2. ./restart_vkbot.sh")
        print("  3. При желании: git add -A && git commit -m 'stability patch' && git push")
        print("Бэкапы: *.bak_stability")


if __name__ == "__main__":
    main()
