#!/usr/bin/env python3
"""
Бот: пересылка расписания хоккейных игр (видео из постов ВК) в Telegram-канал.

ВАЖНО: используется wall.get, а НЕ video.get — видео-трансляции приходят как
вложения (attachments) внутри постов на стене группы, включая репосты
(copy_history).

ВАЖНО про токен: токены VK ID короткоживущие (~1 час, поле expires_in).
Скрипт автоматически обновляет access_token через refresh_token перед
каждым обращением к API, если срок жизни истёк или скоро истечёт.
Файл vk_tokens.json (созданный get_vk_token.py) должен лежать рядом
с этим скриптом — в нём хранится access_token И refresh_token.

Перед запуском:
    pip3 install requests --break-system-packages

Настройки ниже (или через переменные окружения).
"""

import sys
import builtins
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

_original_print = builtins.print


def print(*args, **kwargs):
    """Обёртка над обычным print — добавляет временную метку в начало
    каждой строки лога, чтобы потом можно было точно определить, КОГДА
    произошло то или иное событие (без этого приходилось гадать по
    контексту соседних строк)."""
    timestamp = datetime.now().strftime("%d.%m %H:%M:%S")
    _original_print(f"[{timestamp}]", *args, **kwargs)


# ========================= НАСТРОЙКИ =========================

VK_API_VERSION = "5.199"
VK_CLIENT_ID = "54726270"  # ID вашего VK ID приложения
VK_TOKEN_REFRESH_URL = "https://id.vk.ru/oauth2/auth"

TOKEN_FILE = Path(__file__).parent / "vk_tokens.json"
TEAMS_FILE = Path(__file__).parent / "teams.json"
VIDEO_INDEX_FILE = Path(__file__).parent / "video_index.json"
FRIENDLY_BANNERS_DIR = Path(__file__).parent / "banners_friendly"
OFFICIAL_MATCHES_FILE = Path(__file__).parent / "official_matches.json"

# ID групп ВК (числовые, БЕЗ минуса — минус добавляется автоматически)
VK_GROUP_IDS = [
    26864924,  # Сестрорецк
    214108632,   # pro_videospb (проверена — wall.get работает)
    211881014,
    227274350,
    99334801,
    110125126,
    210582936,
    219803675,
    25510790,    # vksport (Кубок России) — vksport.vkvideo.ru
    165020927,   # Кубок России — вторая группа
    133823152,   # Кубок России — плейлист, третья группа
    136729253,
    227371906,
    120045135,
    27313226,
    114375744,
    226770005,
]

# Группы, которые используются ТОЛЬКО для поиска видео к Кубку России
# (2011 г.р.) — их видео регистрируются в общем индексе (чтобы ссылка
# подтянулась в пост Кубка России в теме 2011 г.р.), но НИКОГДА не
# публикуются в общий чат как "Товарищеская игра", даже если команды/дата
# не распознались или не совпали с официальным матчем.
ROSSII_ONLY_GROUP_IDS = {25510790, 165020927, 133823152}

from config import TG_BOT_TOKEN  # секреты — в config.json
from config import TG_CHAT_ID as TG_CHANNEL_ID  # группа ВСЕССЫЛКИ.СПБ
from config import ADMIN_ID as ADMIN_TELEGRAM_USER_ID
ADMIN_STATE_FILE = Path(__file__).parent / "admin_conversation.json"


ADMIN_ALERT_INTERVAL = 3600   # одно и то же аварийное уведомление — не чаще раза в час


def send_admin_alert(message: str) -> None:
    """Личное сообщение администратору при критическом сбое (например,
    истёкший/отозванный VK refresh_token) — чтобы заметить проблему сразу,
    а не через дни просмотра логов. Ошибки самой отправки специально
    проглатываются — падать ИЗ-ЗА неудавшегося уведомления не нужно,
    основная ошибка и так приведёт к остановке бота."""
    state_file = Path(__file__).parent / "admin_alerts.json"
    key = message[:60]
    try:
        sent = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        sent = {}
    if time.time() - sent.get(key, 0) < ADMIN_ALERT_INTERVAL:
        return    # такое уведомление уже было меньше часа назад
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_TELEGRAM_USER_ID,
                  "text": f"⚠️ {message}\n\nБот ждёт новый токен и продолжит работу сам. "
                          f"Повтор этого уведомления — не раньше чем через час."},
            timeout=15,
        )
    except Exception:
        pass
    sent[key] = time.time()
    try:
        state_file.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


POLL_INTERVAL_SECONDS = 120  # как часто проверять новые посты (5 минут)
POSTS_PER_CHECK = 20         # сколько последних постов стены смотреть за раз

STATE_FILE = Path(__file__).parent / "state.json"

# =============================================================


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------- УПРАВЛЕНИЕ VK-ТОКЕНОМ (с авто-обновлением) -------------------


def load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"Файл {TOKEN_FILE} не найден. Сначала получите токен через get_vk_token.py"
        )
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def save_tokens(data: dict) -> None:
    data["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


import fcntl

_TOKEN_LOCK_FILE = TOKEN_FILE.with_suffix(".lock")


def _token_lock():
    """Контекстный менеджер для файловой блокировки refresh."""
    class _Lock:
        def __enter__(self):
            self.fd = open(_TOKEN_LOCK_FILE, "a+")
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX)
            return self
        def __exit__(self, *a):
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
                self.fd.close()
            except Exception:
                pass
    return _Lock()


def refresh_access_token(tokens: dict) -> dict:
    """Обменивает refresh_token на новый access_token (с lock и повторными попытками)."""
    with _token_lock():
        # Перечитываем с диска — другой процесс мог уже обновить
        try:
            disk = load_tokens()
            if disk.get("refresh_token"):
                tokens = disk
        except Exception:
            pass

        refresh_token = tokens.get("refresh_token")
        device_id = tokens.get("device_id")

        if not refresh_token:
            send_admin_alert(
                "VK-бот: в vk_tokens.json нет refresh_token. "
                "Нужно заново пройти авторизацию через get_vk_token.py"
            )
            raise SystemExit("нет refresh_token")
        if not device_id:
            send_admin_alert(
                "VK-бот: в vk_tokens.json нет device_id. "
                "Заново пройдите авторизацию через get_vk_token.py"
            )
            raise SystemExit("нет device_id")

        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    VK_TOKEN_REFRESH_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": VK_CLIENT_ID,
                        "refresh_token": refresh_token,
                        "device_id": device_id,
                    },
                    timeout=15,
                )
                new_data = resp.json()
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 200 and "access_token" in new_data:
                # Сохраняем новый refresh_token, если VK его вернул
                if "refresh_token" not in new_data:
                    new_data["refresh_token"] = refresh_token
                new_data.setdefault("device_id", device_id)
                print(f"[VK] Токен обновлён (attempt {attempt+1}), expires_in={new_data.get('expires_in')}")
                save_tokens(new_data)
                return new_data

            last_err = new_data
            # Если refresh_token уже использован — перечитываем файл
            if attempt < 2:
                time.sleep(1.0)
                try:
                    disk = load_tokens()
                    if disk.get("refresh_token") and disk["refresh_token"] != refresh_token:
                        tokens = disk
                        refresh_token = tokens["refresh_token"]
                        device_id = tokens.get("device_id", device_id)
                        continue
                except Exception:
                    pass
            time.sleep(1.5 * (attempt + 1))

        send_admin_alert(
            f"VK-бот: не удалось обновить токен после 3 попыток. "
            f"Последняя ошибка: {last_err}. Запусти get_vk_token.py"
        )
        raise SystemExit(f"refresh failed: {last_err}")



def _vk_service_key():
    """Бессрочный сервисный ключ приложения VK из config.json (vk_service_key) — для чтения
    открытых видео и стен его достаточно: без входа и без продлений токена."""
    try:
        return json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8")).get("vk_service_key") or ""
    except Exception:
        return ""


def get_valid_access_token(tokens: dict) -> tuple:
    """
    Возвращает (access_token, tokens) — обновляя токен, если он истёк
    или истекает в ближайшие 2 минуты.
    Если в config.json задан vk_service_key — возвращает его (без продлений).
    """
    key = _vk_service_key()
    if key:
        return key, tokens
    # Перечитываем файл: токен мог обновить ручной скрипт или get_vk_token.py.
    try:
        disk = load_tokens()
        if disk.get("refresh_token") and disk.get("refresh_token") != tokens.get("refresh_token"):
            tokens = disk
    except Exception:
        pass
    obtained_at = tokens.get("obtained_at", 0)
    expires_in = tokens.get("expires_in", 3600)
    expires_at = obtained_at + expires_in

    if time.time() >= expires_at - 120:  # обновляем заранее, с запасом
        tokens = refresh_access_token(tokens)

    return tokens["access_token"], tokens



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
                _time.sleep(0.8)
                continue
            except SystemExit:
                key = _vk_service_key()
                if key:
                    print(f"[VK auth] {label}: переключаюсь на service_key")
                    token = key
                    _time.sleep(0.5)
                    continue
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

# ------------------- VK API -------------------

def vk_get_wall_posts(access_token: str, group_id: int, count: int = POSTS_PER_CHECK) -> list:
    """Получить последние посты со стены группы ВК (с retry / refresh)."""
    data, _ = _vk_api(
        "wall.get",
        {"owner_id": -abs(group_id), "count": count},
        access_token,
        group_id=group_id,
    )
    if not data or "error" in data:
        return []
    return data.get("response", {}).get("items", [])


VIDEO_CATALOG_PER_CHECK = 40


def vk_videos_still_exist(access_token: str, video_refs: list) -> set:
    """
    Проверяет напрямую через video.get (с конкретным списком videos=...),
    какие из переданных видео ДЕЙСТВИТЕЛЬНО ещё существуют на VK. video_refs
    — список (owner_id, video_id). Возвращает множество video_id (int),
    которые подтверждённо существуют.

    Это НАДЁЖНЕЕ, чем полагаться на отсутствие видео в общей выдаче
    wall.get/video.get(count=N) — то просто последние N видео, и старое
    видео может "выпасть" оттуда естественным образом (не потому что
    удалено, а потому что появились более новые). Прямой запрос по
    конкретным ID отличает "правда удалено" от "просто не попало в топ N".
    """
    if not video_refs:
        return set()

    videos_param = ",".join(f"{owner_id}_{vid}" for owner_id, vid in video_refs)
    data = None
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
    if not data or "error" in data:
        print(f"[VK API error - проверка удалённых видео] {data['error']}")
        # При ошибке API лучше НЕ считать видео удалёнными (безопаснее
        # ничего не удалить, чем удалить существующий пост по ошибке).
        return {vid for _, vid in video_refs}

    existing_ids = {item["id"] for item in data.get("response", {}).get("items", [])}
    return existing_ids


def vk_get_group_videos(access_token: str, group_id: int, count: int = VIDEO_CATALOG_PER_CHECK):
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
    return data.get("response", {}).get("items", [])


def extract_videos_from_post(post: dict) -> list:
    """
    Собирает все видео-вложения поста, включая репосты (copy_history).
    Если у видео нет собственного поля 'date' (такое бывает у видео внутри
    репостов), подставляем дату публикации поста как fallback — это
    используется дальше в parse_video_title, если дата не найдена в
    заголовке видео.

    ВАЖНО: проверяем, что реальный владелец видео (video['owner_id']) —
    ОДНА ИЗ НАШИХ отслеживаемых групп. Это касается ОБОИХ путей — и
    репостов (copy_history), И ПРЯМЫХ вложений тоже: если кто-то в одной
    из наших групп поделился видео из совсем постороннего паблика
    напрямую (не структурным "репостом" в терминах VK, а просто вложением
    в своём посте), такое видео могло случайно "проскочить" через парсер,
    если заголовок хоть отдалённо похож на "команда - команда" с датой —
    именно так один раз в канал попал ролик выставочного матча тренеров
    из совершенно постороннего паблика.
    """
    videos = []
    post_date = post.get("date")
    known_owner_ids = {-abs(g) for g in VK_GROUP_IDS}

    for att in post.get("attachments", []) or []:
        if att.get("type") == "video" and "video" in att:
            video = att["video"]
            if video.get("owner_id") not in known_owner_ids:
                continue  # видео принадлежит постороннему источнику — пропускаем
            if "date" not in video and post_date:
                video = {**video, "date": post_date}
            videos.append(video)

    for repost in post.get("copy_history", []) or []:
        repost_date = repost.get("date", post_date)
        for att in repost.get("attachments", []) or []:
            if att.get("type") == "video" and "video" in att:
                video = att["video"]
                if video.get("owner_id") not in known_owner_ids:
                    continue  # репост из постороннего источника — пропускаем
                if "date" not in video and repost_date:
                    video = {**video, "date": repost_date}
                videos.append(video)

    return videos


# ------------------- СПРАВОЧНИК ОФИЦИАЛЬНЫХ КОМАНД (ФХСПб) -------------------
# teams.json создаётся скриптом fetch_teams.py и содержит списки команд по
# возрастным группам с сайта fhspb.ru. Используется для более точного
# распознавания названий команд в заголовках видео — вместо того чтобы
# полагаться только на эвристику "текст - текст".

def load_known_teams() -> list:
    """Возвращает отсортированный по убыванию длины список 'базовых' названий
    команд (без возрастного суффикса вида ' 09', ' 15' и т.п.), без дублей.

    Для каждой базовой команды дополнительно добавляем женский вариант
    ('ЖХК <название>') как САМОСТОЯТЕЛЬНУЮ запись — женская команда клуба
    это не то же самое, что обычная (мужская), и должна распознаваться
    отдельно, а не путаться с ней. Женский вариант ДЛИННЕЕ обычного, а раз
    список отсортирован по убыванию длины — при поиске сначала проверяется
    именно "ЖХК X", и только если такого совпадения нет — просто "X"."""
    if not TEAMS_FILE.exists():
        return []

    data = json.loads(TEAMS_FILE.read_text(encoding="utf-8"))
    base_names = set()
    # Учитываем как обычный возрастной суффикс ("Шайба 13"), так и составы
    # с дополнительным номером ("Шайба 13 2" — второй состав того же клуба)
    suffix_re = re.compile(r'\s\d{1,2}(\s\d+)?$')

    for age_group, teams in data.items():
        for name in teams:
            base = suffix_re.sub('', name).strip()
            # Если после отрезания "возраста" название заканчивается на "№" —
            # это была НЕ возрастная метка, а часть настоящего названия
            # команды (например, "СШОР № 1" — "1" тут номер школы, а не
            # состава). В этом случае отрезать вообще не нужно.
            if base.endswith('№'):
                base = name.strip()
            if base:
                base_names.add(base)
                base_names.add(f"ЖХК {base}")

    # Длинные названия проверяем первыми, чтобы избежать частичных совпадений
    # (например, чтобы "СКА Петергоф" не терялось внутри более общего "СКА",
    # а "ЖХК Динамо Юниор" — внутри обычного "Динамо Юниор")
    return sorted(base_names, key=len, reverse=True)


def find_known_teams_with_positions(title: str, known_teams: list) -> list:
    """Как find_known_teams_in_title, но возвращает (start, end, название)
    — позиции нужны, чтобы проверить, что именно разделяет две найденные
    команды в тексте (например, слэш означает 'один из двух вариантов').

    Для записей вида "ЖХК <название>" используется ОТДЕЛЬНЫЙ, более гибкий
    паттерн — между "ЖХК" и самим названием в реальных заголовках часто
    стоит кавычка ('ЖХК "Динамо Юниор"'), а не только пробел/дефис.

    ВАЖНО: границы слова (\\b) на обоих концах паттерна — без них короткое
    название вроде "СКА" совпадало бы ВНУТРИ более длинного слова вроде
    "ЦСКА" (Ц+СКА), а "Динамо" — как префикс внутри "Динамо-Джуниверс",
    "съедая" только часть настоящего названия команды."""
    def _build_flexible_pattern(name):
        marked = re.sub(r'(?<=[а-яё])(?=[А-ЯЁ])', '\x00', name)
        escaped = re.escape(marked)
        escaped = re.sub(r"\\[ -]", r"[\\s\\-]", escaped)
        escaped = escaped.replace('\x00', r'[\s\-]?')
        return escaped

    found = []
    remaining = title
    for team in known_teams:
        is_zhk = team.upper().startswith("ЖХК ")
        if is_zhk:
            rest = team[4:]
            rest_pattern = _build_flexible_pattern(rest)
            pattern_str = r"ЖХК\s*[\"'«]?\s*" + rest_pattern + r"\b"
        else:
            pattern_str = r"\b" + _build_flexible_pattern(team) + r"\b"
        pattern = re.compile(pattern_str, re.IGNORECASE)

        # Ищем ВСЕ вхождения этого названия, а не только первое — нужно
        # для случаев "самоигры" (клуб играет против своего же второго
        # состава), где одно и то же базовое название встречается в
        # заголовке ДВАЖДЫ (например, "Красная Машина Юниор" и "Красная
        # Машина Юниор 2018 2" — второе совпадение просто с доп. цифрой
        # состава после общей части, которая при поиске команды не видна).
        while True:
            m = pattern.search(remaining)
            if not m:
                break
            if not is_zhk:
                # Обычное (не-ЖХК) название не должно совпадать, если прямо
                # перед ним в тексте стоит "ЖХК" — та часть уже должна была
                # найтись отдельным (более длинным, проверяется раньше)
                # ЖХК-паттерном; если почему-то не нашлась — на всякий
                # случай не засчитываем это как обычную команду, но всё
                # равно "потребляем" эту позицию, чтобы не зациклиться.
                preceding = remaining[max(0, m.start() - 12):m.start()].upper()
                if "ЖХК" in preceding:
                    remaining = remaining[:m.start()] + " " * (m.end() - m.start()) + remaining[m.end():]
                    continue
            matched_team = team
            # Если сразу после найденного названия в тексте стоит ОДНА
            # цифра (не часть большего числа) — это, скорее всего, номер
            # состава ("Красная Машина Юниор 2" — второй состав), а не
            # возраст (тот всегда двузначный, 09-18). Подхватываем её сразу
            # здесь — иначе автоматическое распознавание теряет номер
            # состава, и второй состав путается с первым при сопоставлении.
            # Учитываем и вариант с полным годом перед номером состава
            # ("Красная Машина Юниор 2017 2" — год пропускаем, "2" в конце
            # это номер состава).
            squad_match = re.match(
                r'\s*"?\s*(?:(?:19|20)\d{2}\s+)?(?:(?:0[9]|1[0-8])\s+)?(\d)\b(?!\d)',
                title[m.end():m.end() + 12],
            )
            if squad_match:
                matched_team = f"{team} {squad_match.group(1)}"

            found.append((m.start(), m.end(), matched_team))
            remaining = remaining[:m.start()] + " " * (m.end() - m.start()) + remaining[m.end():]
    found.sort(key=lambda x: x[0])

    # Убираем "дублирующиеся" короткие совпадения — если короткое название
    # стоит ВПЛОТНУЮ (только пробел между ними) перед более длинным, которое
    # его же и СОДЕРЖИТ (например, "СКА" перед "Армия СКА" — "СКА" входит
    # в "Армия СКА" как часть его полного названия), это не отдельная вторая
    # команда, а случайное повторение/префикс. Отбрасываем короткое.
    filtered = []
    for i, (start, end, team) in enumerate(found):
        if i + 1 < len(found):
            next_start, next_end, next_team = found[i + 1]
            gap = title[end:next_start]
            if gap.strip() == "" and team.upper() in next_team.upper():
                continue  # короткое совпадение — часть/префикс следующего, пропускаем
        filtered.append((start, end, team))

    return filtered


def find_known_teams_in_title(title: str, known_teams: list) -> list:
    """Ищет упоминания известных команд в заголовке, в порядке появления.
    Пробел и дефис считаются взаимозаменяемыми — в справочнике команда
    может значиться как "Динамо Юниор", а в заголовке ВК её часто пишут
    через дефис: "Динамо-Юниор"."""
    return [name for _, _, name in find_known_teams_with_positions(title, known_teams)]


KNOWN_TEAMS = load_known_teams()

KUBOK_ROSSII_TEAMS_FILE = Path(__file__).parent / "kubok_rossii_teams.json"


def load_known_teams_rossii() -> list:
    """Отдельный справочник команд специально для Кубка России — обычный
    teams.json собран с fhspb.ru и содержит только питерские клубы, а в
    Кубке России участвуют команды со всей России ("Динамо-Джуниверс",
    "Крылья Советов" и т.п.), которых там попросту нет. Этот файл строит
    post_schedule.py на каждом прогоне из реального списка участников."""
    if not KUBOK_ROSSII_TEAMS_FILE.exists():
        return []
    teams = json.loads(KUBOK_ROSSII_TEAMS_FILE.read_text(encoding="utf-8"))
    return sorted(set(teams), key=len, reverse=True)


KNOWN_TEAMS_ROSSII = load_known_teams_rossii()

# Некоторые клубы в заголовках VK-видео Кубка России пишут сокращённо —
# такого сокращения нет в официальном списке с junior.fhr.ru (там полное
# название), поэтому автоматическое сопоставление их не находит. Здесь
# вручную поддерживаемый список известных сокращений: ключ — как пишут в
# заголовке, значение — точное официальное название с сайта.
KUBOK_ROSSII_ABBREVIATIONS = {
    "МАХ": "Московская академия хоккея",
}

GENERAL_TEAM_ABBREVIATIONS = {
    "Петергоф": "СКА Петергоф",
    "СШОР 1": "СШОР № 1",
}


def _expand_general_abbreviations(title):
    for abbr, full_name in GENERAL_TEAM_ABBREVIATIONS.items():
        if full_name.upper() in title.upper():
            continue
        title = re.sub(r'\b' + re.escape(abbr) + r'\b', full_name, title, flags=re.IGNORECASE)
    return title


def _expand_rossii_abbreviations(title: str) -> str:
    """Заменяет известные сокращения клубов Кубка России на полные
    официальные названия ДО сопоставления со справочником — иначе
    сокращение просто не найдётся в списке официальных названий."""
    for abbr, full_name in KUBOK_ROSSII_ABBREVIATIONS.items():
        title = re.sub(r'\b' + re.escape(abbr) + r'\b', full_name, title)
    return title


# ------------------- ПАРСИНГ ЗАГОЛОВКА ВИДЕО -------------------
# Заголовки в разных группах сильно различаются по структуре: дата может
# стоять в начале, в конце, через запятую, через "|", без года, словами
# ("7 мая 2026"), с bullet-разделителями (•), с префиксом турнира и т.д.
# Стратегия:
#   1. Ищем дату И время НЕЗАВИСИМО друг от друга где угодно в строке.
#   2. Ищем команды через тире, ОКРУЖЁННОЕ ПРОБЕЛАМИ (это исключает ложные
#      срабатывания вида "3-е место", "2015-2016", "СЕВЕРСТАЛЬ-Б").
#   3. Если дата в заголовке не найдена, но команды распознаны — используем
#      дату публикации видео в ВК как разумный fallback.
#   4. Явно нехоккейный контент (водное поло и т.п.) отфильтровываем.


RU_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
}

DATE_NUMERIC_RE = re.compile(r'\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b')
DATE_NUMERIC_NOYEAR_RE = re.compile(r'\b(\d{1,2})\.(\d{1,2})\b(?!\.\d)')
DATE_RU_WORDS_RE = re.compile(
    r'\b(\d{1,2})\s+(' + '|'.join(RU_MONTHS.keys()) + r')\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')

# Возраст команды часто указывается как двузначное число сразу после
# названия команды (например, "Динамо-Форвард 14" = 2014 г.р.). Диапазон
# 10-18 соответствует возрастным категориям, которые мы отслеживаем.
# ВАЖНО: negative lookahead (?!:) — иначе "16" из времени "16:00" ошибочно
# распознавалось бы как возраст вместо настоящего "14" дальше в строке.
AGE_TOKEN_RE = re.compile(r'(?<![.\d:])(09|1[0-8])(?![.\d:])')
# Некоторые источники пишут возраст полным годом ("Динамо Юниор 2016")
# вместо привычного двузначного кода ("Динамо Юниор 16") — распознаём и
# такой формат тоже, используется как запасной вариант, если основной
# AGE_TOKEN_RE ничего не нашёл (двузначный код в принципе не мог найтись
# внутри "2016", поскольку окружён цифрами — см. lookbehind/lookahead выше).
FULL_YEAR_AGE_RE = re.compile(r'\b20(09|1[0-8])\b')

# Если год в дате не указан (напр. "18.04 15:00...") — подставляем этот год.
DEFAULT_YEAR = 2026

NOISE_WORDS = [
    "МСК", "г.р.", "Товарищеская игра", "Live:",
]
# Явно НЕ хоккейный контент, а также видео-нарезки обзоров (не сама
# трансляция игры) — такие видео пропускаем целиком, не парсим и не пересылаем
EXCLUDE_KEYWORDS = [
    "ВОДНОЕ ПОЛО", "БАСКЕТБОЛ", "ФУТБОЛ", "ВОЛЕЙБОЛ",
    "ОБЗОР МАТЧА", "ОБЗОР ИГРЫ", "ОБЗОР",
    "ТРЕНИРОВКА",  # видео с тренировок — не сама игра/трансляция матча
]

# Только тире, окружённое пробелами, считаем разделителем команд
TEAM_DASH_RE = re.compile(r'\s[-–—]\s')
BIRTH_YEAR_RE = re.compile(r'\b(19|20)\d{2}(-(19|20)?\d{2})?\b')


def _extract_date(title: str) -> tuple:
    """Возвращает (date_str, найденная_подстрока) или (None, None)."""
    m = DATE_RU_WORDS_RE.search(title)
    if m:
        day, month_name, year = m.groups()
        month = RU_MONTHS[month_name.lower()]
        return f"{int(day):02d}.{month:02d}.{year}", m.group(0)

    m = DATE_NUMERIC_RE.search(title)
    if m:
        day, month, year = m.groups()
        if len(year) == 2:
            year = f"20{year}"
        return f"{day}.{month}.{year}", m.group(0)

    m = DATE_NUMERIC_NOYEAR_RE.search(title)
    if m:
        day, month = m.groups()
        return f"{day}.{month}.{DEFAULT_YEAR}", m.group(0)

    return None, None


def _extract_teams(title: str, date_match: str, time_match: str) -> str:
    """Вычищает дату/время/мусор и пытается выделить строку 'команда - команда'."""
    core = title
    if date_match:
        core = core.replace(date_match, " ")
    if time_match:
        core = core.replace(time_match, " ")
    for noise in NOISE_WORDS:
        core = re.sub(re.escape(noise), " ", core, flags=re.IGNORECASE)

    if "•" in core:
        segments = [s.strip() for s in core.split("•")]
        dash_segments = [s for s in segments if TEAM_DASH_RE.search(s)]
        if dash_segments:
            core = dash_segments[0]

    core = re.sub(r'\([^)]*\)', ' ', core)  # убираем уточнения в скобках, напр. "(г. Казань)"
    core = re.sub(r'[|/]', ' ', core)
    core = BIRTH_YEAR_RE.sub(' ', core)  # убираем годы рождения (2015, 2015-2016 и т.п.)
    core = re.sub(r'\s+', ' ', core).strip(" .,-–—")

    if ". " in core:
        prefix, _, rest = core.partition(". ")
        if TEAM_DASH_RE.search(rest) and not TEAM_DASH_RE.search(prefix):
            core = rest.strip()

    core = core.replace('"', '').replace('«', '').replace('»', '')
    core = re.sub(r'\s+', ' ', core).strip(" .,-–—")

    return core if core else None


def parse_video_title(title: str, description: str = "", upload_timestamp: int = None,
                       live_start_time: int = None, extra_known_teams: list = None) -> dict:
    result = {"date": None, "time": None, "teams": None, "age": None, "alt_teams": None,
               "teams_verified": False}

    # Проверяем и заголовок, и описание — вид спорта часто указан только
    # в описании ("Региональные соревнования по футболу..."), а заголовок
    # при этом состоит из одних названий клубов, которые можно спутать
    # с хоккейными командами.
    combined_for_exclusion = f"{title} {description}".upper()
    if any(kw in combined_for_exclusion for kw in EXCLUDE_KEYWORDS):
        return result  # не хоккей — не распознаём вообще

    title = _expand_general_abbreviations(title)

    if extra_known_teams:
        # extra_known_teams передаётся ТОЛЬКО для видео из групп Кубка
        # России — там же применяем известные сокращения клубов.
        title = _expand_rossii_abbreviations(title)

    date_str, date_match = _extract_date(title)

    time_match_obj = TIME_RE.search(description) or TIME_RE.search(title)
    time_match_str = time_match_obj.group(0) if time_match_obj else None
    if time_match_obj:
        result["time"] = f"{time_match_obj.group(1)}:{time_match_obj.group(2)}"

    teams = None
    alt_teams = None
    teams_verified = False  # True — обе команды найдены в официальном списке (teams.json)

    # extra_known_teams — например, справочник Кубка России (команды со
    # всей России, которых нет в обычном teams.json с fhspb.ru). Объединяем
    # с основным списком — длинные названия всё так же проверяются первыми.
    effective_known_teams = KNOWN_TEAMS
    if extra_known_teams:
        effective_known_teams = sorted(set(KNOWN_TEAMS) | set(extra_known_teams), key=len, reverse=True)

    # 1. Приоритетный способ: сверка с официальным списком команд ФХСПб.
    #    Надёжнее эвристики по тире — не зависит от форматирования заголовка.
    if effective_known_teams:
        matched_pos = find_known_teams_with_positions(title, effective_known_teams)
        if len(matched_pos) >= 2:
            teams = f"{matched_pos[0][2]} — {matched_pos[1][2]}"
            teams_verified = True

            # Если найдены ТРИ команды, и между 2-й и 3-й в тексте стоит "/" —
            # это, вероятно, ещё не определившаяся пара ("победитель другой
            # игры", формат "Команда А - Команда Б/Команда В"). Регистрируем
            # ОБА варианта пары под одним видео — какой бы ни подтвердился
            # на fhspb.ru, ссылка на трансляцию найдётся.
            if len(matched_pos) >= 3:
                gap = title[matched_pos[1][1]:matched_pos[2][0]]
                if "/" in gap:
                    alt_teams = f"{matched_pos[0][2]} — {matched_pos[2][2]}"

    # 2. Fallback: эвристика по тире, если официальный список не помог.
    # ВАЖНО: команды из этой ветки НЕ считаются подтверждёнными — если
    # ни одна из них не значится в teams.json (официальный список ФХСПб),
    # это, вероятно, вообще не наш турнир/не хоккей, и публиковать такое
    # в общий чат как "Товарищескую игру" не стоит (см. register_video_in_index
    # и вызывающий код — используют teams_verified для решения о публикации).
    if not teams:
        heuristic_teams = _extract_teams(title, date_match, time_match_str)
        if heuristic_teams and TEAM_DASH_RE.search(heuristic_teams):
            teams = heuristic_teams

    # Возраст команды теряется при сверке со справочником (там названия
    # без возрастного суффикса), поэтому распознаём его отдельно —
    # иначе "Динамо-Форвард 10" и "Динамо-Форвард 14" неразличимы в посте.
    age_match = AGE_TOKEN_RE.search(title)
    if age_match:
        result["age"] = age_match.group(1)
    else:
        full_year_match = FULL_YEAR_AGE_RE.search(title)
        if full_year_match:
            result["age"] = full_year_match.group(1)

    if teams:
        result["teams"] = teams
        result["alt_teams"] = alt_teams

        # Если дата не найдена в заголовке — пробуем найти её в описании
        # (организаторы часто пишут дату/время игры текстом в описании,
        # например: "Матч состоится в субботу 05.09.2026 г., начало в 19:00")
        if not date_str and description:
            desc_date_str, _ = _extract_date(description)
            if desc_date_str:
                date_str = desc_date_str

        # Если это прямая трансляция ВК (live) — у неё есть отдельное поле
        # live_start_time с реальным временем начала эфира. Оно надёжнее,
        # чем дата СОЗДАНИЯ объекта трансляции (та может быть заранее,
        # за несколько дней до самой игры).
        if not date_str and live_start_time:
            dt = datetime.fromtimestamp(live_start_time)
            date_str = dt.strftime("%d.%m.%Y")

        # Тот же live_start_time даёт и точное ВРЕМЯ начала эфира, если оно
        # не было найдено в заголовке/описании обычным способом.
        if not result["time"] and live_start_time:
            dt = datetime.fromtimestamp(live_start_time)
            result["time"] = dt.strftime("%H:%M")

        # Последний по приоритету fallback — дата публикации/создания видео
        if not date_str and upload_timestamp:
            dt = datetime.fromtimestamp(upload_timestamp)
            date_str = dt.strftime("%d.%m.%Y")
            result["date_source"] = "upload"      # дата игры неизвестна — взята дата загрузки

    result["date"] = date_str
    result["teams_verified"] = teams_verified
    return result



# ------------------- ОТПРАВКА В TELEGRAM -------------------

def get_friendly_banner_path(age: str) -> Path:
    """Подбирает баннер 'Товарищеская игра' по возрасту (двузначный код,
    например '14' -> '2014'). Если возраст не распознан — общий баннер
    со знаком вопроса вместо года."""
    if age:
        full_year = f"20{age}"
        path = FRIENDLY_BANNERS_DIR / f"friendly_{full_year}.png"
        if path.exists():
            return path
    return FRIENDLY_BANNERS_DIR / "friendly_unknown.png"


def send_photo_to_telegram(banner_path: Path, caption: str):
    """Возвращает message_id при успехе, иначе None."""
    with open(banner_path, "rb") as photo_file:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            data={
                "chat_id": TG_CHANNEL_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "disable_notification": True,
            },
            files={"photo": photo_file},
            timeout=30,
        )
    data = resp.json()
    ok = resp.status_code == 200 and data.get("ok")
    if not ok:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")
        return None
    return data["result"]["message_id"]


def delete_telegram_message(message_id: int) -> bool:
    """Удаляет сообщение целиком — используется, когда пост дня опустел
    (из него убрали все матчи, например, из-за отмены/переноса
    товарищеской игры и последующего удаления/переименования видео на
    VK)."""
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteMessage",
        data={"chat_id": TG_CHANNEL_ID, "message_id": message_id},
        timeout=30,
    )
    data = resp.json()
    ok = resp.status_code == 200 and data.get("ok")
    if not ok:
        description = str(data.get("description", ""))
        if "not found" in description:
            print(f"[INFO] Пост {message_id} уже удалён")
            return True
        # Бот не может удалять сообщения старше 48 часов (ограничение
        # Telegram, действует и для администраторов). Редактировать свои
        # сообщения можно без ограничения — помечаем пост как неактуальный.
        try:
            edit = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageCaption",
                data={"chat_id": TG_CHANNEL_ID, "message_id": message_id,
                      "caption": "❌ Матч отменён или перенесён"},
                timeout=30,
            ).json()
        except Exception as e:
            edit = {"ok": False, "description": str(e)}
        if edit.get("ok"):
            print(f"[INFO] Пост {message_id} удалить нельзя (старше 48 ч) — помечен как отменённый")
            return True
        print(f"[WARN] Не удалось ни удалить, ни пометить пост {message_id}: "
              f"{resp.text} / {edit.get('description')}")
    return ok


def load_admin_state() -> dict:
    if ADMIN_STATE_FILE.exists():
        return json.loads(ADMIN_STATE_FILE.read_text(encoding="utf-8"))
    return {"update_offset": 0, "conversation": None}


def save_admin_state(state: dict) -> None:
    ADMIN_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_telegram_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=15,
        )
        data = resp.json()
    except Exception:
        return []
    if not data.get("ok"):
        return []
    return data.get("result", [])


def send_admin_message(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_TELEGRAM_USER_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception:
        pass


VK_VIDEO_LINK_RE = re.compile(r'(?:vk\.com|vkvideo\.ru)/(?:video|live)(-?\d+)_(\d+)')


def _try_autodetect_match(owner_id: int, video_id: int, access_token: str):
    videos = vk_get_group_videos(access_token, abs(owner_id), count=100)
    target = None
    for v in (videos or []):
        if v["id"] == video_id:
            target = v
            break
    if target is None:
        return None

    extra_teams = KNOWN_TEAMS_ROSSII if abs(owner_id) in ROSSII_ONLY_GROUP_IDS else None
    parsed = parse_video_title(
        target.get("title", ""), target.get("description", ""),
        upload_timestamp=target.get("date"), live_start_time=target.get("live_start_time"),
        extra_known_teams=extra_teams,
    )
    try:
        fx, _ = _match_video_to_fixture(target.get("title", ""), target.get("date"),
                                        target.get("live_start_time"), owner_id=owner_id)
    except Exception:
        fx = None
    live = target.get("live_start_time")
    return {
        "fx": fx,
        "time": datetime.fromtimestamp(live).strftime("%H:%M") if live else None,
        "title": target.get("title", ""),
        "description": target.get("description", ""),
        "teams": parsed["teams"],
        "date": parsed["date"],
        "age": parsed.get("age"),
        "teams_verified": parsed.get("teams_verified"),
    }


def _advance_conversation(admin_state: dict, answer_text: str) -> bool:
    """Пошаговый ввод: команды, дата, время, возраст; затем привязка."""
    conv = admin_state["conversation"]
    prev = conv.setdefault("prev", {})
    step = conv.get("step")
    ans = answer_text.strip()
    keep = ans.lower() in ("+", "да", "ок", "ok")

    if step == "awaiting_teams":
        if keep and prev.get("teams"):
            conv["team1"], conv["team2"] = prev["teams"]
        else:
            parts = re.split(r"\s*[-–—]\s*|\s+vs\.?\s+", ans, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                send_admin_message('Не понял. Напишите две команды через дефис: «Команда1 - Команда2»')
                return True
            conv["team1"], conv["team2"] = parts[0].strip(), parts[1].strip()
    elif step == "awaiting_date":
        if keep and prev.get("date"):
            conv["date"] = prev["date"]
        elif re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", ans):
            d, mth, y = ans.split(".")
            conv["date"] = f"{int(d):02d}.{int(mth):02d}.{y}"
        elif re.match(r"^\d{1,2}\.\d{1,2}$", ans):
            d, mth = ans.split(".")
            conv["date"] = f"{int(d):02d}.{int(mth):02d}.{datetime.now().year}"
        else:
            send_admin_message("Не понял дату. Напишите ДД.ММ.ГГГГ или ДД.ММ:")
            return True
    elif step == "awaiting_time":
        if keep and prev.get("time"):
            conv["time"] = prev["time"]
        elif ans == "-":
            conv["time"] = "-"
        elif re.match(r"^\d{1,2}[:.]\d{2}$", ans):
            h, mi = re.split(r"[:.]", ans)
            conv["time"] = f"{int(h):02d}:{mi}"
        else:
            send_admin_message("Не понял время. Напишите ЧЧ:ММ (например 14:00) или «-»:")
            return True
    elif step == "awaiting_age":
        if keep and prev.get("age"):
            conv["age"] = prev["age"]
        else:
            a = re.sub(r"\D", "", ans) if ans != "-" else "-"
            if re.match(r"^20(09|1[0-8])$", a):
                a = a[2:]
            if a != "-" and not re.match(r"^(09|1[0-8])$", a):
                send_admin_message("Не понял возраст. Напишите год рождения (например 2013 или 13) или «-»:")
                return True
            conv["age"] = a

    def hint(key):
        if not prev.get(key):
            return ""
        value = " — ".join(prev[key]) if key == "teams" else (f"20{prev[key]}" if key == "age" else prev[key])
        return f"\nПодсказка из видео: {value} — «+», чтобы оставить."

    if not conv.get("team1") or not conv.get("team2"):
        conv["step"] = "awaiting_teams"
        send_admin_message("1/4. Команды через дефис, как в расписании (например: Северсталь - СКА Стрельна)."
                           + hint("teams"))
        return True
    if not conv.get("date"):
        conv["step"] = "awaiting_date"
        send_admin_message("2/4. Дата матча (ДД.ММ.ГГГГ или ДД.ММ)." + hint("date"))
        return True
    if conv.get("time") is None:
        conv["step"] = "awaiting_time"
        send_admin_message("3/4. Время начала матча (например 14:00), или «-», если не знаете." + hint("time"))
        return True
    if conv.get("age") is None:
        conv["step"] = "awaiting_age"
        send_admin_message("4/4. Год рождения (например 2013), или «-», если не важен." + hint("age"))
        return True

    age = None if conv["age"] == "-" else conv["age"]
    typed = f'{conv["team1"]} - {conv["team2"]}' + (f" {age}" if age else "")
    start_dt = None
    if conv["time"] != "-":
        try:
            start_dt = datetime.strptime(f'{conv["date"]} {conv["time"]}', "%d.%m.%Y %H:%M")
        except ValueError:
            start_dt = None
    try:
        fx, why = team_matcher.match_title(typed, [conv["date"]], fixtures=_get_fixtures(),
                                           alias_groups=team_matcher.load_alias_groups(), start_dt=start_dt)
    except Exception as e:
        fx, why = None, str(e)
    if not fx and "_closest_fixture" in globals():
        fx = _closest_fixture(conv["date"], conv["team1"], conv["team2"], age)
    if fx:
        send_admin_message(_register_manual(fx, conv["video_url"], conv.get("title") or typed))
    else:
        video_index = load_video_index()
        key = build_match_key(conv["date"], conv["team1"], conv["team2"], age=age)
        video_index[key] = {"video_url": conv["video_url"], "title": conv.get("title") or typed, "manual": True}
        save_video_index(video_index)
        send_admin_message(f"⚠️ Официального матча «{conv['team1']} — {conv['team2']}», {conv['date']}"
                           f"{f', 20{age} г.р.' if age else ''} в расписании нет ({why}).\n"
                           f"Записал как есть — если это товарищеская игра, так и нужно. "
                           f"Если матч турнира — пришлите ссылку ещё раз и проверьте данные.")
    admin_state["conversation"] = None
    return True


def _closest_fixture(date, team1, team2, age=None):
    """Официальный матч даты с самыми похожими названиями команд (на случай
    опечатки). None, если похожего нет или выбор неоднозначен."""
    import difflib
    sk = team_matcher._skeleton
    a, b = sk(team1), sk(team2)
    scored = []
    for f in _get_fixtures().get(date, []):
        if age and f.get("age") and f["age"] != age:
            continue
        f1, f2 = sk(f["team1"]), sk(f["team2"])
        straight = min(difflib.SequenceMatcher(None, a, f1).ratio(), difflib.SequenceMatcher(None, b, f2).ratio())
        crossed = min(difflib.SequenceMatcher(None, a, f2).ratio(), difflib.SequenceMatcher(None, b, f1).ratio())
        scored.append((max(straight, crossed), f))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.1):
        return scored[0][1]
    return None


def _register_manual(fx, video_url, title):
    """Ручная привязка видео к официальному матчу (перезаписывает прежнюю ссылку)."""
    index = load_video_index()
    entry = {"video_url": video_url, "title": title, "manual": True}
    keys = [fx["key"]] + ([f'{fx["key"]}::{fx["age"]}'] if fx.get("age") else [])
    for k in keys:
        index[k] = entry
    save_video_index(index)
    age = f", 20{fx['age']} г.р." if fx.get("age") else ""
    return (f"✅ Привязал к матчу: {fx['team1']} — {fx['team2']}, {fx['date']}{age}\n"
            f"Ссылка появится в посте в течение пары минут.")


def handle_admin_message(text: str, admin_state: dict, access_token: str) -> bool:
    """Ручная привязка видео. Возвращает True, если сообщение обработано."""
    conv = admin_state.get("conversation")
    if conv is None:
        m = VK_VIDEO_LINK_RE.search(text)
        if not m:
            return False  # не ссылка на видео и диалог не идёт — игнорируем
        owner_id, video_id = int(m.group(1)), int(m.group(2))
        video_url = f"https://vk.com/video{owner_id}_{video_id}"
        detected = _try_autodetect_match(owner_id, video_id, access_token)
        if detected is not None and detected.get("fx"):
            send_admin_message(_register_manual(detected["fx"], video_url, detected["title"]))
            return True
        prev = {}
        if detected is not None:
            if detected.get("teams"):
                parts = detected["teams"].split(" — ")
                if len(parts) == 2:
                    prev["teams"] = [parts[0], parts[1]]
            for k in ("date", "time", "age"):
                if detected.get(k):
                    prev[k] = detected[k]
        conv = {"step": None, "owner_id": owner_id, "video_id": video_id, "video_url": video_url,
                "title": detected["title"] if detected else None,
                "description": detected["description"] if detected else "",
                "team1": None, "team2": None, "date": None, "time": None, "age": None, "prev": prev}
        intro = "Сам не нашёл этот матч в официальном расписании — уточним по шагам."
        if prev:
            intro += " Где есть подсказка из видео, ответьте «+», чтобы её оставить."
        send_admin_message(intro + " «отмена» — прервать.")
        admin_state["conversation"] = conv
        return _advance_conversation(admin_state, "")
    return _advance_conversation(admin_state, text)


def process_admin_messages(admin_state: dict, access_token: str) -> None:
    updates = get_telegram_updates(admin_state.get("update_offset", 0))
    for update in updates:
        admin_state["update_offset"] = update["update_id"] + 1
        message = update.get("message")
        if (message and "pinned_message" in message
                and str(message.get("chat", {}).get("id")) == str(TG_CHANNEL_ID)):
            # служебное «… закрепил(а) сообщение» — убираем из группы
            if delete_telegram_message(message["message_id"]):
                print(f"[OK] Удалено уведомление о закреплении ({message['message_id']})")
            continue
        if not message or message.get("from", {}).get("id") != ADMIN_TELEGRAM_USER_ID:
            continue
        text = message.get("text", "")
        if not text:
            continue
        try:
            handle_admin_message(text, admin_state, access_token)
        except Exception as e:
            send_admin_message(f"Ошибка при обработке: {e}")
            admin_state["conversation"] = None
    save_admin_state(admin_state)


def edit_photo_message(message_id: int, banner_path: Path, caption: str) -> bool:
    """Редактирует уже отправленное фото-сообщение — меняет и картинку, и
    подпись за один вызов (editMessageMedia). Используется, когда заголовок
    видео на ВК изменился ПОСЛЕ того, как мы его уже переслали (пока видео
    ещё попадает в последние 40/20 при обычном опросе)."""
    media = {
        "type": "photo",
        "media": "attach://photo",
        "caption": caption,
        "parse_mode": "HTML",
    }
    with open(banner_path, "rb") as photo_file:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageMedia",
            data={"chat_id": TG_CHANNEL_ID, "message_id": message_id, "media": json.dumps(media)},
            files={"photo": photo_file},
            timeout=30,
        )
    data = resp.json()
    ok = resp.status_code == 200 and data.get("ok")
    description = str(data.get("description", ""))
    if not ok and "message is not modified" not in description:
        print(f"[Telegram error - edit] {resp.status_code}: {resp.text}")
        if "message to edit not found" in description or "MESSAGE_ID_INVALID" in description:
            return "not_found"  # устаревшая ссылка на пост — вызывающий код очистит её
    return ok


def _is_today_or_past(date_str: str) -> bool:
    """True, если дата матча — сегодня или уже в прошлом (значит, публиковать
    можно сразу). False — если матч ещё в будущем (публикацию нужно
    отложить до наступления этого дня)."""
    try:
        match_date = datetime.strptime(date_str, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return True  # не смогли разобрать дату — не блокируем публикацию
    return match_date <= datetime.now().date()


def get_friendly_group_key(age: str, date_str: str) -> str:
    """Один пост в общем чате на каждую пару (возраст, день) — вместо
    отдельного сообщения на каждое видео. 'unknown' — общая группа для
    видео, где возраст распознать не удалось."""
    return f"{age or 'unknown'}_{date_str}"


def format_friendly_day_caption(date_str: str, matches: dict) -> str:
    """Собирает текст поста со списком ВСЕХ товарищеских игр одного
    возраста за один день. matches: {video_id_str: {teams, age, time, video_url}}"""
    lines = [f"📅 {date_str} — Товарищеские игры", ""]

    # Сортируем по времени (игры без времени — в конец списка)
    sorted_matches = sorted(matches.values(), key=lambda m: m.get("time") or "99:99")

    for m in sorted_matches:
        teams_line = f"👥 {m['teams']}"
        if m.get("age"):
            teams_line += f" (20{m['age']} г.р.)"
        lines.append(teams_line)
        if m.get("time"):
            lines.append(f"🕐 {m['time']}")
        lines.append(f'🔗 <a href="{m["video_url"]}">Смотреть трансляцию ВКонтакте</a>')
        lines.append("")

    return "\n".join(lines).strip()


# ------------------- ОБЩИЙ ИНДЕКС ВИДЕО (для бота расписания) -------------------
# Записываем сюда каждое распознанное видео (дата + команды), чтобы
# post_schedule.py мог найти соответствующую трансляцию для матча с
# fhspb.ru и вставить на неё реальную ссылку вместо ссылки на протокол.

AGE_SUFFIX_RE = re.compile(r'\s+\d{2}(-\d{2})?(\s+(\d+))?$')
FULL_YEAR_SUFFIX_RE = re.compile(r'\s+(19|20)\d{2}(\s+(\d+))?$')


def _strip_suffix_keep_squad_number(name: str, pattern) -> str:
    """Убирает совпавший суффикс (возраст/год), но СОХРАНЯЕТ номер состава,
    если он был последней группой в совпадении."""
    m = pattern.search(name)
    if not m:
        return name
    squad_num = m.groups()[-1] if m.groups() else None
    base = name[:m.start()].rstrip()
    return f"{base} {squad_num}" if squad_num else base


def normalize_team_name(name: str) -> str:
    """Синхронизировано с той же функцией в post_schedule.py — важно, чтобы
    оба бота строили ОДИНАКОВЫЕ ключи для одних и тех же матчей. Номер
    состава ("Шайба 13 2") сохраняется — иначе первый и второй составы
    одного клуба стали бы неразличимы. Применяем ТОЛЬКО ОДИН из двух
    проходов (год ИЛИ возраст), не оба подряд — иначе уже сохранённый
    номер состава от первого прохода мог бы ошибочно "съесться" вторым."""
    if not name:
        return ""
    name = name.strip()
    original = name
    stripped = _strip_suffix_keep_squad_number(name, FULL_YEAR_SUFFIX_RE)
    name = stripped if stripped != name else _strip_suffix_keep_squad_number(name, AGE_SUFFIX_RE)
    if name.endswith('№'):
        name = original
    name = re.sub(r'^ЖХК\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^ХК\s*', '', name, flags=re.IGNORECASE)
    name = name.replace('«', '').replace('»', '').replace('"', '')
    name = name.replace('-', ' ')
    name = re.sub(r'\s+', ' ', name).strip()
    return name.lower()


def normalize_team_name_rossii(name: str) -> str:
    """Как normalize_team_name, но ТОЛЬКО для Кубка России — убирает
    уточнение города у "Динамо" (СПб, Мск и т.п.), приводя к тому же
    голому виду, что и на junior.fhr.ru. Синхронизировано с одноимённой
    функцией в post_schedule.py."""
    name = normalize_team_name(name)
    name = re.sub(r'^динамо\s+(спб|мск|москва|питер)$', 'динамо', name)
    return name.strip()


def build_match_key(date_str: str, team1: str, team2: str, age: str = None) -> str:
    """Единый формат ключа для сопоставления матчей между ботами.

    ВАЖНО: возраст (если известен) — часть ключа, синхронизировано с
    post_schedule.py — иначе два разных матча (например, 2011 и 2013 г.р.)
    между ОДНИМИ И ТЕМИ ЖЕ клубами в ОДИН И ТОТ ЖЕ день дают одинаковый
    ключ, и видео одного матча перезаписывает видео другого."""
    t1, t2 = normalize_team_name(team1), normalize_team_name(team2)
    teams_part = "|".join(sorted([t1, t2]))
    age_part = f"::{age}" if age else ""
    return f"{date_str}::{teams_part}{age_part}"


def build_match_key_rossii(date_str: str, team1: str, team2: str, age: str = None) -> str:
    """Как build_match_key, но с normalize_team_name_rossii — используется
    ТОЛЬКО для видео из групп Кубка России (ROSSII_ONLY_GROUP_IDS)."""
    t1, t2 = normalize_team_name_rossii(team1), normalize_team_name_rossii(team2)
    teams_part = "|".join(sorted([t1, t2]))
    age_part = f"::{age}" if age else ""
    return f"{date_str}::{teams_part}{age_part}"


def load_video_index() -> dict:
    if VIDEO_INDEX_FILE.exists():
        return json.loads(VIDEO_INDEX_FILE.read_text(encoding="utf-8"))
    return {}


def save_video_index(index: dict) -> None:
    VIDEO_INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def load_official_matches() -> set:
    """Загружает список ключей официальных матчей турнира (Кубок/Первенство),
    который ведёт post_schedule.py. Используется, чтобы НЕ дублировать такие
    матчи в общий чат как 'Товарищескую игру' — они уже публикуются в теме
    по возрасту с настоящим турнирным баннером."""
    if OFFICIAL_MATCHES_FILE.exists():
        return set(json.loads(OFFICIAL_MATCHES_FILE.read_text(encoding="utf-8")))
    return set()


def is_official_match(parsed: dict, official_keys: set) -> bool:
    """Проверяет, совпадает ли распознанный матч (основной вариант ИЛИ
    альтернативный, если соперник был неоднозначен через '/') с одним из
    официальных матчей турнира."""
    if not parsed["teams"] or not parsed["date"] or not official_keys:
        return False

    for teams_str in filter(None, [parsed["teams"], parsed.get("alt_teams")]):
        team_parts = TEAM_DASH_RE.split(teams_str)
        if len(team_parts) != 2:
            continue
        team1, team2 = team_parts[0].strip(), team_parts[1].strip()
        key = build_match_key(parsed["date"], team1, team2)
        if key in official_keys:
            return True

    return False


def register_video_in_index(parsed: dict, video: dict) -> None:
    """Сохраняет найденное видео в общий индекс, если распознаны и команды, и дата.
    Если пара команд неоднозначна (видео вида "Команда А - Команда Б/Команда В"
    — соперник ещё не определён), регистрирует ОБА варианта под одним и тем
    же видео — какой бы вариант ни подтвердился в реальном расписании,
    ссылка на трансляцию будет найдена.

    Регистрирует под ДВУМЯ видами ключей сразу — обычным и ROSSII-версией
    (без уточнения города, "Динамо СПб" -> "динамо") — безопасно (лишняя
    запись не мешает) и гарантирует совпадение независимо от того, какой
    именно турнир будет искать эту ссылку."""
    if not parsed["teams"] or not parsed["date"]:
        return

    video_url = f"https://vk.com/video{video['owner_id']}_{video['id']}"
    index = load_video_index()

    for teams_str in filter(None, [parsed["teams"], parsed.get("alt_teams")]):
        # Команды могут быть разделены ЛЮБЫМ видом тире — обычным "-"
        # (эвристика по заголовку) или длинным "—" (сверка со справочником).
        team_parts = TEAM_DASH_RE.split(teams_str)
        if len(team_parts) != 2:
            continue
        team1, team2 = team_parts[0].strip(), team_parts[1].strip()

        entry = {"video_url": video_url, "title": video.get("title", "")}
        age = parsed.get("age")

        key = build_match_key(parsed["date"], team1, team2)
        index[key] = entry

        rossii_key = build_match_key_rossii(parsed["date"], team1, team2)
        if rossii_key != key:
            index[rossii_key] = entry

        # Если возраст удалось распознать — регистрируем ДОПОЛНИТЕЛЬНО и под
        # ключом С возрастом. Это точный, приоритетный ключ — предотвращает
        # ситуацию, когда два разных матча (например, 2011 и 2013 г.р.)
        # между ОДНИМИ И ТЕМИ ЖЕ клубами в ОДИН И ТОТ ЖЕ день "затирают"
        # друг друга в индексе (см. build_match_key).
        if age:
            key_age = build_match_key(parsed["date"], team1, team2, age=age)
            index[key_age] = entry

            rossii_key_age = build_match_key_rossii(parsed["date"], team1, team2, age=age)
            if rossii_key_age != key_age:
                index[rossii_key_age] = entry

    save_video_index(index)


# ------------------------- ОСНОВНОЙ ЦИКЛ -------------------------


PENDING_MAX_AGE_DAYS = 14


def _cleanup_stale_pending(state: dict) -> None:
    """Удаляет из _pending_friendly_matches записи старше PENDING_MAX_AGE_DAYS."""
    pending = state.get("_pending_friendly_matches") or {}
    if not pending:
        return
    now = time.time()
    date_re = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
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

def process_pending_friendly_matches(state: dict) -> None:
    """
    Публикует отложенные ранее товарищеские игры (см. _is_today_or_past),
    для которых наступил день матча — используя сохранённые
    заголовок/описание видео, без повторного запроса к VK. Вызывается в
    начале каждого прогона, ДО обработки текущих постов ВК-групп.
    """
    pending = state.get("_pending_friendly_matches", {})
    if not pending:
        return

    official_keys = load_official_matches()
    day_posts = state.setdefault("_friendly_day_posts", {})
    sent_videos = state.setdefault("_sent_videos", {})

    due_vid_ids = []
    for vid_str, entry in pending.items():
        parsed = parse_video_title(entry["title"], entry["description"])
        if parsed["date"] and _is_today_or_past(parsed["date"]):
            due_vid_ids.append(vid_str)

    for vid_str in due_vid_ids:
        entry = pending.pop(vid_str)
        title = entry["title"]
        parsed = parse_video_title(title, entry["description"])

        if not parsed["teams"] or not parsed.get("teams_verified"):
            continue
        if is_official_match(parsed, official_keys):
            continue

        video_url = f"https://vk.com/video{entry['owner_id']}_{entry['id']}"
        try:       # день игры: ещё раз ищем официальный матч (заголовок + обложка)
            fx, why = _match_video_to_fixture(f"{title} {parsed['date']}", None, None,
                                              owner_id=entry["owner_id"],
                                              video_id=f"{entry['owner_id']}_{entry['id']}")
        except Exception as e:
            fx, why = None, str(e)
        if fx:
            written = register_official_video(fx, video_url, title)
            print(f"[OK] Отложенное видео {entry['id']} — официальный матч {fx['date']} "
                  f"{fx['team1']} — {fx['team2']} ({fx.get('age') or '?'}), {why}"
                  f"{'' if written else ', ссылка уже была'}: {title}")
            save_state(state)
            continue
        match_entry = {"teams": parsed["teams"], "age": parsed.get("age"),
                        "time": parsed.get("time"), "video_url": video_url}
        group_key = get_friendly_group_key(parsed.get("age"), parsed["date"])
        banner_path = get_friendly_banner_path(parsed.get("age"))

        post = day_posts.get(group_key)

        if post is None:
            matches = {vid_str: match_entry}
            caption = format_friendly_day_caption(parsed["date"], matches)
            sent_message_id = send_photo_to_telegram(banner_path, caption)
            if sent_message_id:
                print(f"[OK] Отложенный пост дня опубликован (день игры наступил) "
                      f"({group_key}), видео {entry['id']}: {title}")
                day_posts[group_key] = {"message_id": sent_message_id, "matches": matches}
            else:
                print(f"[FAIL] Не удалось опубликовать отложенный пост ({group_key})")
                pending[vid_str] = entry  # попробуем снова на следующем прогоне
                continue
        else:
            post["matches"][vid_str] = match_entry
            caption = format_friendly_day_caption(parsed["date"], post["matches"])
            result = edit_photo_message(post["message_id"], banner_path, caption)
            if result == "not_found":
                new_msg_id = send_photo_to_telegram(banner_path, caption)
                if new_msg_id:
                    day_posts[group_key] = {"message_id": new_msg_id, "matches": post["matches"]}
                else:
                    pending[vid_str] = entry
                    continue
            elif not result:
                pending[vid_str] = entry
                continue
            print(f"[OK] Отложенный пост дня дополнен (день игры наступил) "
                  f"({group_key}), видео {entry['id']}: {title}")

        sent_videos[vid_str] = {"group_key": group_key, "title": title,
                                 "description": entry["description"]}
        save_state(state)


# ======================= ЭТАП 3: сопоставление по расписанию =======================
import team_matcher

UNRESOLVED_RECHECK_SECONDS = 3600      # перепроверка очереди раз в час
UNRESOLVED_KEEP_DAYS = 10              # дальше — удаляем из очереди
UNRESOLVED_DIGEST_HOUR = 21            # ежедневная сводка администратору

_fixtures_cache = {"mtime": None, "data": {}}


def _get_fixtures() -> dict:
    """Официальное расписание с возрастами; перечитывается, только если
    post_schedule.py обновил файл."""
    path = team_matcher.FIXTURES_FILE if team_matcher.FIXTURES_FILE.exists() else team_matcher.OFFICIAL_KEYS_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _fixtures_cache["data"]
    if mtime != _fixtures_cache["mtime"]:
        _fixtures_cache["data"] = team_matcher.load_fixtures()
        _fixtures_cache["mtime"] = mtime
    return _fixtures_cache["data"]


# Подсказки по группам: как клуб называет СЕБЯ в своих трансляциях.
# Ключ — owner_id группы VK (отрицательный), значение — (шаблон, замена).
GROUP_TITLE_HINTS = {
    # «Динамо-Юниор»: «ЖХК 16 - Заневский Молот 16» = «ЖХК Динамо Юниор»
    -114375744: [(r"\bЖХК\b(?!\s*[\"«]?\s*[А-ЯЁA-Zа-яёa-z]{3,})", "ЖХК Динамо Юниор")],
}


def _apply_group_hints(title: str, owner_id=None) -> str:
    for pattern, replacement in GROUP_TITLE_HINTS.get(owner_id, []):
        title = re.sub(pattern, replacement, title)
    return title


def _match_video_to_fixture(title: str, upload_ts=None, live_start=None, owner_id=None, video_id=None):
    """Матч по заголовку; не нашёлся — по обложке видео (если передан video_id)."""
    fx, why = _match_video_to_fixture_title(title, upload_ts, live_start, owner_id)
    if fx is not None or not video_id:
        return fx, why
    try:
        alt, alt_why = _match_by_cover(video_id, title, upload_ts, live_start)
    except Exception as e:
        print(f"[WARN] Обложка {video_id} не прочитана: {e}")
        return fx, why
    return (alt, alt_why) if alt else (fx, why)


def _match_by_cover(full_id, title, upload_ts, live_start):
    import cover_ocr
    if not cover_ocr.available() or any(w in (title or "").lower() for w in team_matcher.REVIEW_WORDS):
        return None, ""
    token, _ = get_valid_access_token(load_tokens())
    lines = cover_ocr.read_cover(full_id, token, VK_API_VERSION)
    start_dt = datetime.fromtimestamp(live_start) if live_start else None
    fx, why = cover_ocr.match(lines, team_matcher.match_title, _get_fixtures(),
                              team_matcher.load_alias_groups(),
                              team_matcher.build_date_candidates(title, live_start, upload_ts), start_dt,
                              title=title)
    if fx:
        key = f'{fx["key"]}::{fx.get("age") or ""}'
        print(f"[INFO] Видео {full_id} — {why}")
        if cover_ocr.mark_notified(full_id, key):
            send_admin_message(
                f"🖼 Привязано по обложке: {fx['date']} {fx['team1']} — {fx['team2']}"
                f" ({fx.get('age') or '?'} г.р.)\nЗаголовок: {title}\n{why}\n"
                f"https://vk.com/video{full_id}")
    return fx, why


def _match_video_to_fixture_title(title: str, upload_ts=None, live_start=None, owner_id=None):
    title = _apply_group_hints(title, owner_id)
    dates = team_matcher.build_date_candidates(title, live_start, upload_ts)
    start_dt = datetime.fromtimestamp(live_start) if live_start else None
    fx, why = team_matcher.match_title(title, dates, fixtures=_get_fixtures(),
                                       alias_groups=team_matcher.load_alias_groups(),
                                       start_dt=start_dt)
    if not fx and live_start and owner_id:
        alt = _match_by_group_team(owner_id, live_start)
        if alt:
            return alt, "по клубу группы и времени трансляции"
    return fx, why


_GROUP_TEAM_CACHE = {}


def _group_team(owner_id):
    """Клуб группы VK — по истории привязанных видео (≥3 видео, клуб в ≥60% из них)."""
    if owner_id in _GROUP_TEAM_CACHE:
        return _GROUP_TEAM_CACHE[owner_id]
    from collections import Counter
    counts, total, seen = Counter(), 0, set()
    for key, v in load_video_index().items():
        url = (v or {}).get("video_url", "") if isinstance(v, dict) else ""
        if f"video{owner_id}_" not in url or url in seen:
            continue
        parts = key.split("::")
        if len(parts) < 2 or "|" not in parts[1]:
            continue
        seen.add(url)
        total += 1
        for t in parts[1].split("|"):
            counts[t] += 1
    team = None
    if total >= 3:
        name, n = counts.most_common(1)[0]
        team = name if n >= 0.6 * total else None
    _GROUP_TEAM_CACHE[owner_id] = team
    return team


def _match_by_group_team(owner_id, live_start):
    """Матч клуба группы, начинающийся в пределах 30 минут от начала трансляции (если он один)."""
    team = _group_team(owner_id)
    if not team:
        return None
    start = datetime.fromtimestamp(live_start)
    found = []
    for f in _get_fixtures().get(start.strftime("%d.%m.%Y"), []):
        if team not in (f.get("team1"), f.get("team2")):
            continue
        hm = re.match(r"(\d{1,2}):(\d{2})", f.get("time") or "")
        if hm:
            begin = start.replace(hour=int(hm.group(1)), minute=int(hm.group(2)), second=0)
            if abs((begin - start).total_seconds()) <= 30 * 60:
                found.append(f)
    return found[0] if len(found) == 1 else None


def register_official_video(fx: dict, video_url: str, title: str) -> list:
    """Регистрирует видео под ключами официального матча. Существующие
    ссылки НЕ перезаписываются (дубли трансляций, чужие видео под общим
    ключом без возраста). Возвращает список записанных ключей."""
    index = load_video_index()
    entry = {"video_url": video_url, "title": title, "verified": True}
    date, t1, t2, age = fx["date"], fx["team1"], fx["team2"], fx.get("age")

    candidates = []
    if age:
        candidates.append(f'{fx["key"]}::{age}')
        candidates.append(build_match_key_rossii(date, t1, t2, age=age))
    candidates.append(fx["key"])
    candidates.append(build_match_key_rossii(date, t1, t2))

    written = []
    for k in dict.fromkeys(candidates):
        if k not in index:
            index[k] = entry
            written.append(k)
    if written:
        save_video_index(index)
    return written


_MATCH_LIKE_RE = re.compile(r"\s[-–—]\s|🆚|&|\bvs\b|\S-\S", re.IGNORECASE)


QUEUE_EXCLUDE_WORDS = (
    "первенство россии", "w18", "w16", "студент", "вуз", "полпред",
    "футбол", "баскетбол", "водное поло", "плавани", "волейбол",
    "лучший игрок", "тренировк", "мастер-класс", "церемони",
)
_OTHER_CITY_RE = re.compile(r"\([А-ЯЁ][а-яё .\-]+\)")   # "(Оренбург)", "(Мытищи)"


def _known_fixture_teams() -> set:
    teams = set()
    for day in _get_fixtures().values():
        for f in day:
            teams.add(f["team1"])
            teams.add(f["team2"])
    return teams


def _looks_like_our_match(title: str, owner_id=None) -> bool:
    """Похоже ли видео на матч наших турниров — иначе в очередь его не берём."""
    if owner_id is not None and abs(owner_id) in ROSSII_ONLY_GROUP_IDS:
        return False
    low = (title or "").lower()
    if any(w in low for w in team_matcher.REVIEW_WORDS) or any(w in low for w in QUEUE_EXCLUDE_WORDS):
        return False
    if _OTHER_CITY_RE.search(title or ""):
        return False
    tokens, _ = team_matcher.tokenize_title(_apply_group_hints(title, owner_id))
    groups = team_matcher.load_alias_groups()
    return any(team_matcher.mentions_team(tokens, t, groups) for t in _known_fixture_teams())


def _queue_unresolved(state: dict, video: dict, title: str, description: str, reason: str) -> None:
    """Кладёт похожее на матч видео в очередь на перепроверку."""
    low = (title or "").lower()
    if any(w in low for w in team_matcher.REVIEW_WORDS):
        return
    if not _MATCH_LIKE_RE.search(title or ""):
        return
    if not _looks_like_our_match(title, video.get("owner_id")):
        return
    queue = state.setdefault("_unresolved_videos", {})
    full_id = f'{video["owner_id"]}_{video["id"]}'
    if full_id in queue:
        return
    queue[full_id] = {
        "title": title, "description": description or "",
        "upload_ts": video.get("date"), "live_start": video.get("live_start_time"),
        "first_seen": int(time.time()), "reason": reason,
    }


def _recheck_unresolved(state: dict) -> None:
    """Раз в час перепроверяет очередь по свежему расписанию и синонимам."""
    now = int(time.time())
    if now - state.get("_unresolved_last_check", 0) < UNRESOLVED_RECHECK_SECONDS:
        return
    state["_unresolved_last_check"] = now
    queue = state.get("_unresolved_videos", {})
    for full_id in list(queue):
        item = queue[full_id]
        if now - item.get("first_seen", now) > UNRESOLVED_KEEP_DAYS * 86400:
            del queue[full_id]
            continue
        if not _looks_like_our_match(item["title"], int(full_id.rsplit("_", 1)[0])):
            del queue[full_id]   # не наш матч (другой регион, студенты, другой вид спорта...)
            continue
        fx, reason = _match_video_to_fixture(item["title"], item.get("upload_ts"), item.get("live_start"),
                                             owner_id=int(full_id.rsplit("_", 1)[0]), video_id=full_id)
        if fx:
            written = register_official_video(fx, f"https://vk.com/video{full_id}", item["title"])
            print(f'[OK] Из очереди: {fx["date"]} {fx["team1"]} — {fx["team2"]} '
                  f'({"записано" if written else "видео уже было"}): {item["title"]}')
            del queue[full_id]
        else:
            item["reason"] = reason
    save_state(state)


def _send_unresolved_digest(state: dict) -> None:
    """Раз в день присылает администратору список нераспознанных видео за
    последние сутки — их можно переслать боту в ручной диалог."""
    now = datetime.now()
    today = now.strftime("%d.%m.%Y")
    if now.hour < UNRESOLVED_DIGEST_HOUR or state.get("_unresolved_last_digest") == today:
        return
    state["_unresolved_last_digest"] = today
    cutoff = int(time.time()) - 86400
    items = [(fid, it) for fid, it in state.get("_unresolved_videos", {}).items()
             if it.get("first_seen", 0) >= cutoff]
    save_state(state)
    if not items:
        return
    lines = [f"📋 Нераспознанные видео за сутки: {len(items)}", ""]
    for fid, it in items[:25]:
        lines.append(f'• {it["title"][:80]}')
        lines.append(f'  https://vk.com/video{fid}  ({it.get("reason", "")})')
    if len(items) > 25:
        lines.append(f"… и ещё {len(items) - 25}")
    lines.append("")
    lines.append("Чтобы добавить видео вручную — перешлите ссылку сюда.")
    send_admin_message("\n".join(lines))


SWEEP_MAX_AGE_DAYS = 10
SWEEP_RETRY_MIN = 30          # видео без матча перепроверяется раз в 30 минут


def _registered_video_ids() -> set:
    """Полные id видео, привязанных к ОФИЦИАЛЬНЫМ матчам под правильным
    ключом. Запись под неверным ключом (старая логика: дата загрузки
    анонса, опечатка в годе, общий ключ без возраста при нескольких
    возрастах, не тот состав) привязкой не считается."""
    ages_by_key = {}
    for day in _get_fixtures().values():
        for f in day:
            ages_by_key.setdefault(f["key"], set()).add(f.get("age"))
    ids = {}                               # id видео -> заголовки, под которыми его привязали
    for k, entry in load_video_index().items():
        parts = k.split("::")
        ages = ages_by_key.get("::".join(parts[:2]))
        if ages is None:
            continue                       # такого официального матча нет
        age = parts[2] if len(parts) > 2 else None
        if age is not None and age not in ages:
            continue                       # возраст не тот
        if age is None and len(ages) > 1:
            continue                       # общий ключ, а пара играет в нескольких возрастах
        m = re.search(r"video(-?\d+)_(\d+)", entry.get("video_url", ""))
        if m:
            ids.setdefault(f"{m.group(1)}_{m.group(2)}", set()).add((entry.get("title") or "").strip())
    return ids


def _sweep_unplaced(state: dict, group_id: int, unique_videos: dict, seen_ids: set) -> None:
    queue = state.setdefault("_unresolved_videos", {})
    sent = state.get("_sent_videos", {})
    pending = state.get("_pending_friendly_matches", {})
    swept = state.setdefault("_swept_videos", {})
    if isinstance(swept, list):                       # прежний формат — «проверено когда-то давно»
        swept = state["_swept_videos"] = {k: 0 for k in swept}
    registered = None
    now = time.time()
    found = queued = 0

    for vid, v in unique_videos.items():
        full_id = f'{v["owner_id"]}_{vid}'
        if full_id in queue:
            q = queue[full_id]
            if v.get("title") and v["title"] != q.get("title"):     # видео переименовали — берём новый заголовок
                q["title"] = v["title"]
                q["live_start"] = v.get("live_start_time") or q.get("live_start")
                state["_unresolved_last_check"] = 0                 # перепроверить очередь на этом цикле
                print(f'[INFO] Видео {full_id} переименовано, перепроверю: {v["title"]}')
            continue
        if vid not in seen_ids:
            continue
        if now - swept.get(full_id, 0) < SWEEP_RETRY_MIN * 60:
            continue                                  # проверяли недавно — ждём
        if str(vid) in sent or str(vid) in pending:
            continue
        ts = v.get("live_start_time") or v.get("date") or 0
        if now - ts > SWEEP_MAX_AGE_DAYS * 86400:
            continue
        if registered is None:
            registered = _registered_video_ids()
        titles = registered.get(full_id)
        if titles is not None and ("" in titles or (v.get("title") or "").strip() in titles):
            continue                                  # привязано, заголовок с тех пор не менялся

        first_try = full_id not in swept
        swept[full_id] = now
        title = v.get("title", "")
        fx, reason = _match_video_to_fixture(title, v.get("date"), v.get("live_start_time"),
                                             owner_id=v["owner_id"], video_id=full_id)
        if fx:
            written = register_official_video(fx, f"https://vk.com/video{full_id}", title)
            if written:
                found += 1
                print(f"[OK] Ранее пропущенное видео {vid} — официальный матч {fx['date']} "
                      f"{fx['team1']} — {fx['team2']} (возр. {fx.get('age') or '?'}): {title}")
        elif first_try:                               # в очередь — только при первой проверке
            before = len(queue)
            _queue_unresolved(state, v, title, v.get("description", ""), reason)
            queued += len(queue) - before

    if len(swept) > 5000:
        for k in sorted(swept, key=swept.get)[:-5000]:
            del swept[k]
    if found or queued:
        print(f"[INFO] Группа {group_id}: ранее пропущенных видео — привязано {found}, в очередь {queued}")
        save_state(state)



def _normalize_video_index_dates() -> int:
    """Приводит ключи video_index к DD.MM.YYYY (с ведущим нулём)."""
    path = Path(__file__).parent / "video_index.json"
    if not path.exists():
        return 0
    try:
        vi = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    changed = 0
    bad_re = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(::.*)?$")
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

def check_groups_once(state: dict, access_token: str) -> None:
    # Сначала публикуем товарищеские игры, для которых наступил день матча
    # (были отложены раньше — см. process_pending_friendly_matches).
    today = datetime.now().strftime("%d.%m.%Y")
    if state.get("_video_index_norm_day") != today:
        try:
            _normalize_video_index_dates()
        except Exception as e:
            print(f"[WARN] normalize video_index: {e}")
        state["_video_index_norm_day"] = today
    _cleanup_stale_pending(state)
    process_pending_friendly_matches(state)
    _recheck_unresolved(state)
    _send_unresolved_digest(state)

    # Отдельная "базовая линия" для видео-каталога (второй источник) —
    # хранится под зарезервированным ключом с подчёркиванием, чтобы не
    # конфликтовать с ID групп (они всегда числовые строки).
    catalog_baseline = state.setdefault("_catalog_baseline_done", {})

    # Список официальных матчей турнира — обновляется post_schedule.py.
    # Загружаем один раз за прогон (не на каждое видео), чтобы не грузить диск.
    official_keys = load_official_matches()

    # Собираем ID ВСЕХ видео, реально увиденных в этом прогоне (по всем
    # группам сразу) — после основного цикла сравниваем с тем, что мы
    # когда-то отправляли (_sent_videos): если видео пропало из выдачи VK
    # совсем (удалено/скрыто автором — например, отменённая товарищеская
    # игра), считаем его отменённым и убираем матч из соответствующего
    # поста дня (см. ниже, после основного цикла по группам).
    all_seen_video_ids_this_run = set()

    for gi, group_id in enumerate(VK_GROUP_IDS):
        if gi > 0:
            time.sleep(0.55)  # stability: не бить VK подряд по всем группам
        key = str(group_id)
        seen_video_ids = set(state.get(key, []))

        posts = vk_get_wall_posts(access_token, group_id)
        wall_videos = []
        for post in posts:
            wall_videos.extend(extract_videos_from_post(post))

        # Второй источник — видео-каталог группы напрямую. Ловит трансляции,
        # опубликованные через нативный механизм ВК без отдельного поста
        # на стене (см. комментарий у vk_get_group_videos).
        catalog_videos = vk_get_group_videos(access_token, group_id)
        catalog_fetch_failed = catalog_videos is None
        if catalog_fetch_failed:
            catalog_videos = []  # для этого прогона просто нет доп. видео из каталога

        if not catalog_baseline.get(key):
            if catalog_fetch_failed:
                # Каталог недоступен (ошибка API) — НЕ отмечаем базовую линию
                # как установленную. Иначе как только доступ появится (например,
                # после вступления в группу), все старые видео из каталога
                # ошибочно посчитаются "новыми" и будут разом пересланы.
                pass
            else:
                # Первое УСПЕШНОЕ включение нового источника для этой группы —
                # молча запоминаем всё, что там уже есть, БЕЗ пересылки. Иначе
                # весь архив видео-каталога разом попал бы в чат.
                for v in catalog_videos:
                    seen_video_ids.add(v["id"])
                catalog_baseline[key] = True
                state[key] = sorted(seen_video_ids)
                save_state(state)
                print(f"[INFO] Базовая линия видео-каталога установлена для группы {group_id} "
                      f"({len(catalog_videos)} видео помечено как уже виденные)")

        all_videos = wall_videos + catalog_videos
        if not all_videos:
            continue

        unique_videos = {}
        for v in all_videos:
            unique_videos[v["id"]] = v
        all_seen_video_ids_this_run.update(str(vid) for vid in unique_videos.keys())

        # --- Проверка правок в уже отправленных видео ---
        # Видео, которые мы уже переслали раньше, могут попасть в этот же
        # список опять (VK всегда отдаёт последние 40/20, включая старые).
        # Пока видео там встречается — сравниваем заголовок/описание с тем,
        # что было на момент отправки. Если изменилось — перепарсиваем и
        # редактируем уже отправленное сообщение. Без единого лишнего
        # запроса к VK API — используем те же данные, что и так получили.
        sent_videos = state.setdefault("_sent_videos", {})
        day_posts = state.setdefault("_friendly_day_posts", {})

        for vid, video in unique_videos.items():
            vid_str = str(vid)
            snapshot = sent_videos.get(vid_str)
            if not snapshot:
                continue  # это видео либо новое, либо не пересылалось (официальный матч)

            if group_id in ROSSII_ONLY_GROUP_IDS:
                # Пост мог быть опубликован ЕЩЁ ДО того, как эта группа стала
                # "только для Кубка России" — не редактируем и не "оживляем"
                # такие старые посты дальше, просто оставляем как есть.
                continue

            current_title = video.get("title", "")
            current_description = video.get("description", "")
            if (current_title == snapshot["title"]
                    and current_description == snapshot["description"]):
                continue  # без изменений

            # Номер видео уникален только ВНУТРИ группы VK, а снимок хранится
            # по голому номеру. Без этой проверки ролик одной группы
            # сравнивался со снимком ролика ДРУГОЙ группы с тем же номером,
            # и бот "видел" правку, которой не было (матчи переезжали между
            # постами туда-обратно каждый цикл).
            if current_title.strip().lower() in ("видео недоступно", "video unavailable",
                                                  "video is unavailable"):
                continue  # заглушка VK для скрытого/удалённого ролика — не правка
            owner_map = state.setdefault("_sent_owner", {})
            snap_owner = owner_map.get(vid_str)
            if snap_owner is None:
                gk = snapshot.get("group_key")
                entry = ((day_posts.get(gk) or {}).get("matches") or {}).get(vid_str) if gk else None
                m_owner = re.search(r"video(-?\d+)_", (entry or {}).get("video_url", ""))
                if m_owner:
                    snap_owner = int(m_owner.group(1))
                    owner_map[vid_str] = snap_owner
            if snap_owner != video["owner_id"]:
                continue  # снимок от другого ролика (другая группа) или владелец неизвестен

            print(f"[EDIT-CHECK] Видео {vid} изменилось: {snapshot['title']!r} -> {current_title!r}")

            upload_ts = video.get("date")
            live_start = video.get("live_start_time")
            new_parsed = parse_video_title(current_title, current_description,
                                            upload_timestamp=upload_ts, live_start_time=live_start)

            if not new_parsed["teams"] and not new_parsed["date"]:
                continue  # стало неразборчивым — оставляем как было

            if is_official_match(new_parsed, official_keys):
                print(f"[WARN] Видео {vid} после правки стало официальным матчем — "
                      f"рекомендуется вручную проверить пост дня {snapshot.get('group_key')}")
                sent_videos[vid_str] = {"group_key": snapshot.get("group_key"),
                                         "title": current_title, "description": current_description}
                save_state(state)
                continue

            if not new_parsed["teams"] or not new_parsed.get("teams_verified"):
                # Команд вообще нет ИЛИ не подтверждены официальным списком —
                # не даём такому содержимому "затереть" существующий пост.
                print(f"[SKIP] Видео {vid} — после правки команды не найдены или не подтверждены "
                      f"официальным списком, пост не трогаем: {current_title}")
                sent_videos[vid_str] = {"group_key": snapshot.get("group_key"),
                                         "title": current_title, "description": current_description}
                save_state(state)
                continue

            register_video_in_index(new_parsed, video)

            video_url = f"https://vk.com/video{video['owner_id']}_{video['id']}"
            new_match_entry = {"teams": new_parsed["teams"], "age": new_parsed.get("age"),
                                "time": new_parsed.get("time"), "video_url": video_url}
            new_group_key = get_friendly_group_key(new_parsed.get("age"), new_parsed["date"])
            old_group_key = snapshot.get("group_key")

            if old_group_key and old_group_key != new_group_key:
                # Возраст/дата изменились — убираем матч из старого поста дня
                old_post = day_posts.get(old_group_key)
                if old_post and vid_str in old_post["matches"]:
                    del old_post["matches"][vid_str]
                    if old_post["matches"]:
                        old_age, old_date = old_group_key.split("_", 1)
                        old_caption = format_friendly_day_caption(old_date, old_post["matches"])
                        old_banner = get_friendly_banner_path(None if old_age == "unknown" else old_age)
                        if edit_photo_message(old_post["message_id"], old_banner, old_caption) == "not_found":
                            day_posts.pop(old_group_key, None)
                    else:
                        # Матчей в посте больше не осталось — сам пост
                        # больше не нужен, удаляем его из Telegram и из
                        # нашего состояния (иначе он "зависает" со старым,
                        # уже неактуальным текстом).
                        delete_telegram_message(old_post["message_id"])
                        day_posts.pop(old_group_key, None)

            target_post = day_posts.get(new_group_key)
            new_banner_path = get_friendly_banner_path(new_parsed.get("age"))

            if target_post is None:
                matches = {vid_str: new_match_entry}
                caption = format_friendly_day_caption(new_parsed["date"], matches)
                msg_id = send_photo_to_telegram(new_banner_path, caption)
                if msg_id:
                    day_posts[new_group_key] = {"message_id": msg_id, "matches": matches}
                    print(f"[OK] Пост дня создан после правки ({new_group_key}), видео {vid}")
            else:
                target_post["matches"][vid_str] = new_match_entry
                caption = format_friendly_day_caption(new_parsed["date"], target_post["matches"])
                result = edit_photo_message(target_post["message_id"], new_banner_path, caption)
                if result == "not_found":
                    # Пост, на который у нас была ссылка, больше не существует
                    # (например, Telegram посчитал сообщение устаревшим/удалённым).
                    # Очищаем всю запись — при следующем изменении этого видео
                    # (или другого в этой же группе даты) она будет создана
                    # заново, а не будет бесконечно проваливаться с той же ошибкой.
                    print(f"[WARN] Пост дня ({new_group_key}) больше не существует — очищаем ссылку")
                    day_posts.pop(new_group_key, None)
                elif result:
                    print(f"[OK] Пост дня обновлён после правки ({new_group_key}), видео {vid}")

            sent_videos[vid_str] = {"group_key": new_group_key,
                                     "title": current_title, "description": current_description}
            save_state(state)

        # Страховка: видео, помеченные просмотренными, но никуда не попавшие.
        _sweep_unplaced(state, group_id, unique_videos, seen_video_ids)

        new_videos = [v for vid, v in unique_videos.items() if vid not in seen_video_ids]
        new_videos.sort(key=lambda v: v.get("date", 0))

        first_run = key not in state

        for video in new_videos:
            if first_run:
                seen_video_ids.add(video["id"])
                continue

            title = video.get("title", "")
            description = video.get("description", "")
            upload_ts = video.get("date")
            live_start = video.get("live_start_time")
            extra_teams = KNOWN_TEAMS_ROSSII if group_id in ROSSII_ONLY_GROUP_IDS else None
            parsed = parse_video_title(title, description, upload_timestamp=upload_ts,
                                        live_start_time=live_start, extra_known_teams=extra_teams)

            # Этап 3: сначала ищем видео в официальном расписании дня.
            fx, fx_reason = _match_video_to_fixture(title, upload_ts, live_start, owner_id=video["owner_id"],
                                                    video_id=f'{video["owner_id"]}_{video["id"]}')
            if fx:
                video_url = f"https://vk.com/video{video['owner_id']}_{video['id']}"
                written = register_official_video(fx, video_url, title)
                print(f"[OK] Видео {video['id']} — официальный матч {fx['date']} "
                      f"{fx['team1']} — {fx['team2']} (возр. {fx.get('age') or '?'})"
                      f"{'' if written else ', ссылка уже была'}: {title}")
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue
            if fx_reason.startswith("неоднозначно"):
                # Не регистрируем наугад под общим ключом без возраста —
                # видео одного возраста попало бы в пост другого.
                print(f"[WARN] Видео {video['id']} — {fx_reason}; в очередь на проверку: {title}")
                _queue_unresolved(state, video, title, description, fx_reason)
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue

            # Пропускаем видео, где не удалось распознать ни команды, ни дату —
            # обычно это клипы, хайлайты не про конкретную игру, промо-ролики и т.п.
            if not parsed["teams"] and not parsed["date"]:
                _queue_unresolved(state, video, title, description, fx_reason)
                seen_video_ids.add(video["id"])
                continue

            # Регистрируем в общем индексе для бота расписания — независимо
            # от того, удастся ли переслать сообщение в Telegram ниже.
            register_video_in_index(parsed, video)

            # Если команд вообще нет (только дата — например, анонс турнира
            # без указания конкретных соперников), ИЛИ команды найдены
            # только эвристикой по тире (не подтверждены официальным
            # списком с fhspb.ru) — это, вероятно, не конкретный матч или
            # вообще не хоккей. В общий чат такое не публикуем — только
            # регистрируем в индексе строкой выше (вдруг всё же пригодится).
            if not parsed["teams"] or not parsed.get("teams_verified"):
                print(f"[SKIP] Видео {video['id']} — команды не найдены или не подтверждены "
                      f"официальным списком, не публикуем в общий чат: {title}")
                _queue_unresolved(state, video, title, description, fx_reason)
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue

            # Группы, предназначенные ТОЛЬКО для поиска видео к Кубку России —
            # их видео никогда не публикуются в общий чат как "Товарищеская
            # игра", даже если распознавание не нашло совпадения с
            # официальным матчем. Ссылка уже зарегистрирована в индексе
            # строкой выше — подтянется в пост Кубка России автоматически.
            if group_id in ROSSII_ONLY_GROUP_IDS:
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue

            # Если это официальный матч турнира (Кубок/Первенство) — он уже
            # публикуется в теме по возрасту с настоящим турнирным баннером
            # (post_schedule.py), не дублируем в общий чат как "Товарищескую
            # игру". Ссылка на видео при этом уже зарегистрирована выше —
            # она подтянется в тот пост автоматически.
            if is_official_match(parsed, official_keys):
                print(f"[SKIP] Видео {video['id']} — официальный матч турнира, "
                      f"не дублируем в общий чат: {title}")
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue

            if parsed.get("date_source") == "upload" and video.get("live_status") == "started":
                parsed["date"] = datetime.now().strftime("%d.%m.%Y")      # трансляция идёт — игра сегодня
            elif parsed.get("date_source") == "upload" and video.get("live_status") == "upcoming":
                # Анонс трансляции без даты игры: день неизвестен, товарищеским не публикуем.
                # Видео не помечаем просмотренным — на каждом цикле оно проверяется заново
                # (официальный матч, дата в заголовке, время начала трансляции).
                undated = state.setdefault("_undated_announces", {})
                if str(video["id"]) not in undated:
                    undated[str(video["id"])] = int(time.time())
                    print(f"[INFO] Видео {video['id']} — анонс без даты игры, жду дату или начало трансляции: {title}")
                    save_state(state)
                continue
            if not _is_today_or_past(parsed["date"]):
                # Матч ещё впереди — не публикуем прямо сейчас, а
                # откладываем до наступления самого дня игры. Видео при
                # этом помечаем как "увиденное" (чтобы не считать его
                # снова новым каждый прогон), но саму публикацию делает
                # отдельный проход по _pending_friendly_matches ниже.
                pending = state.setdefault("_pending_friendly_matches", {})
                pending[str(video["id"])] = {
                    "owner_id": video["owner_id"], "id": video["id"],
                    "title": title, "description": description,
                }
                print(f"[INFO] Видео {video['id']} отложено до дня игры "
                      f"({parsed['date']}): {title}")
                seen_video_ids.add(video["id"])
                state[key] = sorted(seen_video_ids)
                save_state(state)
                continue

            vid_str = str(video["id"])
            video_url = f"https://vk.com/video{video['owner_id']}_{video['id']}"
            match_entry = {"teams": parsed["teams"], "age": parsed.get("age"),
                            "time": parsed.get("time"), "video_url": video_url}
            group_key = get_friendly_group_key(parsed.get("age"), parsed["date"])
            banner_path = get_friendly_banner_path(parsed.get("age"))

            post = day_posts.get(group_key)

            if post is None:
                # Первая игра этого возраста за этот день — создаём новый пост
                matches = {vid_str: match_entry}
                caption = format_friendly_day_caption(parsed["date"], matches)
                sent_message_id = send_photo_to_telegram(banner_path, caption)
                if sent_message_id:
                    print(f"[OK] Новый пост дня создан ({group_key}), видео {video['id']}: {title}")
                    day_posts[group_key] = {"message_id": sent_message_id, "matches": matches}
                else:
                    print(f"[FAIL] Не удалось создать пост дня ({group_key}) для видео {video['id']}")
                    continue  # не помечаем виденным — попробуем ещё раз на следующем прогоне
            else:
                # Пост дня уже есть — добавляем игру и обновляем текст
                post["matches"][vid_str] = match_entry
                caption = format_friendly_day_caption(parsed["date"], post["matches"])
                result = edit_photo_message(post["message_id"], banner_path, caption)
                if result == "not_found":
                    # Старый пост утрачен — создаём новый с тем же набором матчей
                    print(f"[WARN] Пост дня ({group_key}) больше не существует — создаём заново")
                    new_msg_id = send_photo_to_telegram(banner_path, caption)
                    if new_msg_id:
                        day_posts[group_key] = {"message_id": new_msg_id, "matches": post["matches"]}
                        print(f"[OK] Пост дня пересоздан ({group_key}), видео {video['id']}: {title}")
                    else:
                        print(f"[FAIL] Не удалось пересоздать пост дня ({group_key}) для видео {video['id']}")
                        continue
                elif result:
                    print(f"[OK] Пост дня дополнен ({group_key}), видео {video['id']}: {title}")
                else:
                    print(f"[FAIL] Не удалось обновить пост дня ({group_key}) для видео {video['id']}")
                    continue

            seen_video_ids.add(video["id"])
            state[key] = sorted(seen_video_ids)
            sent_videos[vid_str] = {"group_key": group_key, "title": title, "description": description}
            save_state(state)

        state[key] = sorted(seen_video_ids)
        save_state(state)

    # После обработки ВСЕХ групп — ищем видео, которые мы когда-то
    # отправляли, но которых теперь совсем не видно ни в одной из групп
    # (удалено/скрыто автором — например, отменённая или перенесённая
    # товарищеская игра). В отличие от EDIT-CHECK (где видео МЕНЯЕТСЯ, но
    # остаётся видимым), здесь видео пропадает из выдачи VK полностью.
    #
    # ВАЖНО: отсутствие в общей выдаче (последние 20/40 видео на группу)
    # само по себе НЕ доказывает удаление — старое видео могло просто
    # "выпасть" из этого окна из-за новых публикаций. Поэтому дополнительно
    # проверяем КОНКРЕТНО эти видео напрямую через video.get — и удаляем
    # пост, только если VK подтверждает, что видео правда больше нет.
    sent_videos = state.get("_sent_videos", {})
    day_posts = state.get("_friendly_day_posts", {})
    candidate_vid_strs = [v for v in sent_videos if v not in all_seen_video_ids_this_run]

    if candidate_vid_strs:
        video_refs = []
        for vid_str in candidate_vid_strs:
            group_key = sent_videos[vid_str].get("group_key")
            post = day_posts.get(group_key) if group_key else None
            match_entry = post["matches"].get(vid_str) if post else None
            video_url = match_entry.get("video_url") if match_entry else None
            if video_url:
                m = re.search(r'video(-?\d+)_(\d+)', video_url)
                if m:
                    video_refs.append((int(m.group(1)), int(m.group(2))))

        confirmed_existing_ids = vk_videos_still_exist(access_token, video_refs)

        for vid_str in candidate_vid_strs:
            if int(vid_str) in confirmed_existing_ids:
                continue  # видео подтверждённо ещё существует — просто не попало в последние N, не трогаем

            snapshot = sent_videos[vid_str]
            group_key = snapshot.get("group_key")
            post = day_posts.get(group_key) if group_key else None
            if not post or vid_str not in post["matches"]:
                del sent_videos[vid_str]
                continue

            del post["matches"][vid_str]
            print(f"[INFO] Видео {vid_str} подтверждённо удалено на VK — убрано из поста дня ({group_key})")

            if post["matches"]:
                old_age, old_date = group_key.split("_", 1)
                caption = format_friendly_day_caption(old_date, post["matches"])
                banner = get_friendly_banner_path(None if old_age == "unknown" else old_age)
                if edit_photo_message(post["message_id"], banner, caption) == "not_found":
                    day_posts.pop(group_key, None)
            else:
                delete_telegram_message(post["message_id"])
                day_posts.pop(group_key, None)
                print(f"[OK] Пост дня удалён ({group_key}) — не осталось матчей")

            del sent_videos[vid_str]
            save_state(state)


def _role_allows_run():
    """Резервирование (папка backup/): работать ли боту на этом компьютере сейчас."""
    base = Path(__file__).parent
    role_file = base / "backup" / "role"
    role = role_file.read_text().strip() if role_file.exists() else ""
    if role == "standby":
        return (base / ".standby_active").exists()
    if role == "primary":
        ok = base / ".primary_ok"
        return ok.exists() and time.time() - ok.stat().st_mtime < 300
    return True


def main() -> None:
    state = load_state()
    tokens = load_tokens()
    admin_state = load_admin_state()
    print("Бот запущен. Проверка групп каждые", POLL_INTERVAL_SECONDS, "секунд.")

    ADMIN_CHECK_INTERVAL = 10
    seconds_since_vk_check = POLL_INTERVAL_SECONDS

    if "--once" in sys.argv:                 # облако: один проход и выход
        access_token, tokens = get_valid_access_token(tokens)
        _VK_TOKENS_REF["tokens"] = tokens
        process_admin_messages(admin_state, access_token)
        check_groups_once(state, access_token)
        return

    while True:
        if not _role_allows_run():          # резервирование: сейчас работает другой компьютер
            time.sleep(30)
            continue
        try:
            access_token, tokens = get_valid_access_token(tokens)
            _VK_TOKENS_REF["tokens"] = tokens
            process_admin_messages(admin_state, access_token)

            if seconds_since_vk_check >= POLL_INTERVAL_SECONDS:
                check_groups_once(state, access_token)
                seconds_since_vk_check = 0
        except SystemExit as e:
            # Ошибка токена: не завершаемся (иначе перезапуск по кругу), а ждём
            # новый токен — файл перечитывается в get_valid_access_token.
            print(f"[FATAL] {e}")
            print("[INFO] Жду новый токен: vk_tokens.json перечитывается раз в минуту")
            time.sleep(60)
            continue
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(ADMIN_CHECK_INTERVAL)
        seconds_since_vk_check += ADMIN_CHECK_INTERVAL




# --- разбор «турнирных» заголовков: «Территория хоккея U15. Санкт-Петербург-Республика Беларусь | дата» ---
_parse_video_title_orig = parse_video_title
_U_AGE_RE = re.compile(r"\b[UУ]\s?(\d{2})\b\.?\s*", re.I)


def _prep_title(title):
    t = re.sub(r"(?i)территория\s+хоккея\.?\s*", "", title or "")
    t = re.sub(r"(?i)первенство\s+санкт-петербурга\.?\s*", "", t)          # название турнира
    t = re.sub(r"📍.*$", "", t)                                              # арена в конце
    t = re.sub(r"\s*(?:🆚|\bVS\b|\bvs\.?)\s*", " - ", t)                 # 🆚 / vs → дефис
    t = re.sub(r"(?i)\bгуд\s+гол\b", "ГУДГОЛ", t)                       # «ГУД ГОЛ» → «ГудГол»
    t = re.sub(r"(?i)⏰\s*начало\s+в", " ", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", " ", t)    # прочие эмодзи
    t = re.sub(r"\s{2,}", " ", t)
    m = _U_AGE_RE.search(t)
    year = ""
    if m:                                    # U15 в сезоне 2026/27 → 2012 г.р.
        now = datetime.now()
        season = now.year if now.month >= 7 else now.year - 1
        year = str(season - int(m.group(1)) + 1)
        t = _U_AGE_RE.sub("", t, count=1)
    t = re.sub(r"(Санкт-Петербург[а-я]*)-(?=[А-ЯЁ])", r"\1 - ", t)   # «Санкт-Петербург-Команда»
    t = re.sub(r"(?<=[а-яё])-(?=Санкт-Петербург)", " - ", t)          # «Команда-Санкт-Петербург»
    if year:
        t = t.replace(" |", f" {year} |", 1) if " |" in t else f"{t} {year}"
    return t.strip()


def parse_video_title(title, *args, **kwargs):
    r = _parse_video_title_orig(title, *args, **kwargs)
    if r and not r.get("teams"):
        t2 = _prep_title(title)
        if t2 != title:
            r2 = _parse_video_title_orig(t2, *args, **kwargs)
            if r2 and r2.get("teams"):
                return r2
    return r




# --- подтверждение по официальному расписанию (сборные и команды, которых нет в teams.json) ---
_parse_video_title_v1 = parse_video_title
_FX_CACHE = {}


def _stems(name):
    n = (name or "").lower().replace("ё", "е")
    n = re.sub(r"(?<![а-яa-z])(сб|сборная|команда|хк|жхк)(?![а-яa-z])\.?", " ", n)
    return {w[:5] for w in re.findall(r"[a-zа-я]{4,}", n)}


def _official_fixtures():
    f = Path(__file__).parent / "official_fixtures.json"
    try:
        m = f.stat().st_mtime
        if _FX_CACHE.get("m") != m:
            d = json.loads(f.read_text(encoding="utf-8"))
            _FX_CACHE.update(m=m, items=d if isinstance(d, list) else list(d.values()))
        return _FX_CACHE["items"]
    except Exception as e:
        print(f"[WARN] official_fixtures.json не прочитан: {e}")
        return []


def parse_video_title(title, *args, **kwargs):
    r = _parse_video_title_v1(title, *args, **kwargs)
    if not r or not r.get("teams") or r.get("teams_verified") or not r.get("date"):
        return r
    parts = re.split(r"\s+[-–—]\s+", r["teams"], maxsplit=1)
    if len(parts) != 2:
        return r
    a, b = _stems(parts[0]), _stems(parts[1])
    if not a or not b:
        return r
    hits = []
    for x in _official_fixtures():
        if x.get("date") != r["date"] or (r.get("age") and x.get("age") and x["age"] != r["age"]):
            continue
        k = (x.get("key") or "").split("::")
        if len(k) < 2 or "|" not in k[1]:
            continue
        f1, f2 = (_stems(t) for t in k[1].split("|", 1))
        if f1 and f2 and {frozenset(f1), frozenset(f2)} == {frozenset(a), frozenset(b)}:
            hits.append(x)
    if len(hits) == 1:
        x = hits[0]
        n1, n2 = x["key"].split("::")[1].split("|", 1)
        n1, n2 = x.get("team1") or n1.title(), x.get("team2") or n2.title()
        r["teams"] = f"{n1} - {n2}"
        r["teams_verified"] = True
        r["age"] = r.get("age") or x.get("age")
        print(f"[OK] Команды подтверждены по расписанию: {title[:70]} → {r['teams']}")
    return r


if __name__ == "__main__":
    main()
