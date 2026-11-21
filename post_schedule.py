#!/usr/bin/env python3
"""
Итоговый бот: тянет расписание/результаты с fhspb.ru, группирует матчи по
дням, подбирает нужный баннер (возраст + группа + Кубок/Первенство) и
публикует один пост на день в соответствующую тему Telegram-группы. При
появлении нового счёта — редактирует уже опубликованный пост, а не создаёт
новый (как в оригинальном канале — "изменено HH:MM").

РЕЖИМЫ ЗАПУСКА:
    python3 post_schedule.py --dry-run          # только показать, что будет
                                                    отправлено, БЕЗ реальной
                                                    отправки в Telegram
    python3 post_schedule.py --live              # реальная отправка
    python3 post_schedule.py --dry-run --test-last-season
                                                   # прогон на данных ПРОШЛОГО
                                                    сезона для проверки логики
                                                    (полезно, пока новый сезон
                                                    ещё не начался)

Перед первым реальным запуском (--live) обязательно проверьте вывод
--dry-run — он не отправляет ничего в Telegram, только печатает.
"""

import argparse
import builtins
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

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


_original_print = builtins.print


def print(*args, **kwargs):
    """Обёртка над обычным print — добавляет временную метку в начало
    каждой строки лога (синхронизировано с vk_to_tg_hockey_bot.py)."""
    timestamp = datetime.now().strftime("%d.%m %H:%M:%S")
    _original_print(f"[{timestamp}]", *args, **kwargs)


try:
    import spb_fhr_client
except ImportError:
    spb_fhr_client = None  # модуль опционален — при отсутствии просто не обогащаем данные

# ========================= НАСТРОЙКИ =========================

from config import TG_BOT_TOKEN  # секреты — в config.json
from config import TG_CHAT_ID
TG_CHANNEL_USERNAME = "hockeyjunior_spb"  # для прямых ссылок на посты (t.me/<username>/<тема>/<номер>)

BANNERS_DIR = Path(__file__).parent / "banners"
STATE_FILE = Path(__file__).parent / "posted_days.json"
VIDEO_INDEX_FILE = Path(__file__).parent / "video_index.json"
OFFICIAL_MATCHES_FILE = Path(__file__).parent / "official_matches.json"
PUBLISH_EVE_HOUR = 22  # пост на завтра — не раньше этого часа накануне
OFFICIAL_FIXTURES_FILE = Path(__file__).parent / "official_fixtures.json"
AUTO_TEAMS_FILE = Path(__file__).parent / "teams.json"
OFFICIAL_RETENTION_DAYS = 60

# Собираются за один прогон (процесс каждый раз новый — cron):
_official_fixtures = {}      # "ключ::возраст" -> матч с возрастом
_schedule_team_names = {}    # "2016" -> {"Варяги 16", ...} как в расписании
SPB_DETAILS_CACHE_FILE = Path(__file__).parent / "spb_match_details.json"
KUBOK_ROSSII_TEAMS_FILE = Path(__file__).parent / "kubok_rossii_teams.json"
LIVE_SNAPSHOT_FILE = Path(__file__).parent / "live_snapshot.json"
LOCK_FILE = Path(__file__).parent / "post_schedule.lock"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Возраст -> ID темы в Telegram-группе (собрано через getUpdates)
TOPIC_IDS = {
    "2009": 3,
    "2010": 5,
    "2011": 8,
    "2012": 9,
    "2013": 10,
    "2014": 11,
    "2015": 104,
    "2016": 105,
    "2017": 106,
    "2018": 868,
}

# Кубок СПб: по регламенту делится ТОЛЬКО по возрастам, БЕЗ групп А/Б
CUP_CONFIG = {
    "2010": 6565,
    "2011": 6566,
    "2012": 6567,
    "2013": 6568,
    "2014": 6569,
    "2015": 6570,
    "2016": 6583,  # Кубок Федерации
    "2017": 6584,
    "2018": 6585,
}

# Первенство СПб: делится по возрастам И группам А/Б (кроме 2009, 2010)
CHAMPIONSHIP_CONFIG = [
    ("2009", None, 6571),
    ("2010", None, 6572),
    ("2011", "A", 6573),
    ("2011", "Б", 6574),
    ("2012", "A", 6575),
    ("2012", "Б", 6576),
    ("2013", "A", 6577),
    ("2013", "Б", 6578),
    ("2014", "A", 6579),
    ("2014", "Б", 6580),
    ("2015", "A", 6581),
    ("2015", "Б", 6582),
]

# =============================================================


# ------------------- ПАРСИНГ РАСПИСАНИЯ (fhspb.ru) -------------------

MATCH_ROW_RE = re.compile(
    r'<td style="white-space:nowrap;">([^<]+)</td>'
    r'<td class="text-center">\s*<span class="label secondary(?: round)?">([\d:]+)</span>\s*</td>'
    r'<td class="text-center">\s*<a href="/?Arena\?[^"]*"[^>]*>([^<]*)</a>\s*</td>'
    r'<td>\s*<a href="/?Match\?TournamentID=(\d+)&MatchID=(\d+)">([^<]+)</a>\s*</td>'
    r'<td class="text-center" style="font-size:Larger;font-weight:bold;white-space:nowrap;">([^<]*)</td>',
    re.DOTALL
)

MATCH_ROW_NO_SCORE_RE = re.compile(
    r'<td style="white-space:nowrap;">([^<]+)</td>'
    r'<td class="text-center">\s*<span class="label secondary(?: round)?">([\d:]+)</span>\s*</td>'
    r'<td class="text-center">\s*<a href="/?Arena\?[^"]*"[^>]*>([^<]*)</a>\s*</td>'
    r'<td>\s*<a href="/?Match\?TournamentID=(\d+)&MatchID=(\d+)">([^<]+)</a>\s*</td>',
    re.DOTALL
)


def fetch_with_retry(url: str, max_attempts: int = 6, base_delay: float = 5.0) -> str:
    """
    fhspb.ru периодически обрывает соединение (SSLEOFError, Connection reset)
    при частых последовательных запросах — судя по всему, сайт не очень
    стабилен под нагрузкой в начале сезона. Повторяем попытку с
    увеличивающейся паузой, прежде чем сдаться.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _get_http().get(url, headers=HEADERS, timeout=(5, 25))
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_attempts:
                time.sleep(base_delay * attempt)  # 3с, 6с, 9с...
    raise last_error


def fetch_schedule_html(tournament_id: int, month: int = None) -> str:
    url = f"https://fhspb.ru/Schedule?TournamentID={tournament_id}"
    if month is not None:
        url += f"&Month={month}"
    return fetch_with_retry(url)


def clean_score(raw_score: str) -> str:
    """
    fhspb.ru для ещё не сыгранных матчей кладёт в ячейку счёта не пустую
    строку, а буквально "&nbsp;" (неразрывный пробел) — без этой очистки
    бот показывал бы "Итог матча: &nbsp;" вместо того, чтобы просто
    промолчать про счёт для будущих игр.
    """
    if not raw_score:
        return None
    cleaned = raw_score.strip()
    empty_markers = {"", "&nbsp;", "\xa0", "-", "—"}
    if cleaned in empty_markers:
        return None
    return cleaned


def parse_matches(html: str) -> list:
    matches = []
    seen_ids = set()

    for m in MATCH_ROW_RE.finditer(html):
        date_str, time_str, arena, tid, match_id, teams, score = m.groups()
        if match_id in seen_ids:
            continue
        seen_ids.add(match_id)
        team_parts = re.split(r'\s-\s', teams.strip(), maxsplit=1)
        matches.append({
            "date": date_str.strip(),
            "time": time_str.strip(),
            "arena": arena.strip(),
            "tournament_id": tid,
            "match_id": match_id,
            "team1": team_parts[0].strip() if len(team_parts) > 0 else None,
            "team2": team_parts[1].strip() if len(team_parts) > 1 else None,
            "score": clean_score(score),
            "protocol_url": f"https://fhspb.ru/Match?TournamentID={tid}&MatchID={match_id}",
        })

    for m in MATCH_ROW_NO_SCORE_RE.finditer(html):
        date_str, time_str, arena, tid, match_id, teams = m.groups()
        if match_id in seen_ids:
            continue
        seen_ids.add(match_id)
        team_parts = re.split(r'\s-\s', teams.strip(), maxsplit=1)
        matches.append({
            "date": date_str.strip(),
            "time": time_str.strip(),
            "arena": arena.strip(),
            "tournament_id": tid,
            "match_id": match_id,
            "team1": team_parts[0].strip() if len(team_parts) > 0 else None,
            "team2": team_parts[1].strip() if len(team_parts) > 1 else None,
            "score": None,
            "protocol_url": f"https://fhspb.ru/Match?TournamentID={tid}&MatchID={match_id}",
        })

    return matches


# ------------------- ФОРМАТИРОВАНИЕ СООБЩЕНИЯ -------------------

# Unicode keycap-цифры для отображения счёта "плашками" (как в оригинале)
KEYCAP_DIGITS = {str(d): f"{d}\u20E3" for d in range(10)}

WEEKDAY_PREFIX_RE = re.compile(r'^[А-Яа-я]{2}\s+')
# Обе регулярки теперь ЗАХВАТЫВАЮТ номер состава последней группой (если он
# есть) — чтобы normalize_team_name мог его СОХРАНИТЬ (см. ниже). Без этого
# "Клуб 18" и "Клуб 18 2" (второй состав) становились неразличимы после
# нормализации — а раз оба играют друг с другом в одном турнире, ключ
# матча терял смысл (обе "команды" превращались в одну и ту же строку).
AGE_SUFFIX_RE = re.compile(r'\s+\d{2}(-\d{2})?(\s+(\d+))?$')
# Полный год рождения в конце названия (формат spb.fhr.ru, напр. "Шайба 2013"
# вместо привычного "Шайба 13" на fhspb.ru) — убираем ДО обычного суффикса.
FULL_YEAR_SUFFIX_RE = re.compile(r'\s+(19|20)\d{2}(\s+(\d+))?$')


def score_to_keycaps(score: str) -> str:
    """Преобразует '6 : 1' в '6️⃣ : 1️⃣', сохраняя суффиксы вида ОТ/ПБ/ТР как есть."""
    if not score:
        return ""
    parts = score.split(":")
    result = []
    for part in parts:
        part = part.strip()
        digits = "".join(KEYCAP_DIGITS.get(ch, ch) for ch in part)
        result.append(digits)
    return " : ".join(result)


def _strip_suffix_keep_squad_number(name: str, pattern) -> str:
    """Убирает совпавший суффикс (возраст/год), но СОХРАНЯЕТ номер состава,
    если он был последней группой в совпадении (см. комментарий у AGE_SUFFIX_RE)."""
    m = pattern.search(name)
    if not m:
        return name
    squad_num = m.groups()[-1] if m.groups() else None
    base = name[:m.start()].rstrip()
    return f"{base} {squad_num}" if squad_num else base


def normalize_team_name(name: str) -> str:
    """Приводит название команды к единому виду — используется и для
    сопоставления с video_index.json (VK-видео), и с данными spb.fhr.ru.
    У разных источников разные соглашения: fhspb.ru использует двузначный
    год ("Шайба 13"), spb.fhr.ru — четырёхзначный ("Шайба 2013"); дефис и
    пробел как разделитель слов тоже используются непоследовательно
    ("Динамо-Питер" vs "Динамо Питер") — всё это приводится к одному виду.
    Номер состава ("Шайба 13 2" — второй состав) СОХРАНЯЕТСЯ — иначе
    первый и второй составы одного клуба стали бы неразличимы.

    ВАЖНО: применяем ТОЛЬКО ОДИН из двух проходов (год ИЛИ возраст), не
    оба подряд — иначе уже сохранённый номер состава от первого прохода
    мог бы ошибочно "съесться" вторым проходом (приняв его за возраст)."""
    if not name:
        return ""
    name = name.strip()
    original = name
    stripped = _strip_suffix_keep_squad_number(name, FULL_YEAR_SUFFIX_RE)
    name = stripped if stripped != name else _strip_suffix_keep_squad_number(name, AGE_SUFFIX_RE)
    # Если после отрезания название заканчивается на "№" — это была НЕ
    # возрастная метка/номер состава, а часть настоящего названия команды
    # (например, "СШОР № 1" — "1" тут номер школы, не состава). В этом
    # случае отрезать вообще не нужно, возвращаемся к исходному названию.
    if name.endswith('№'):
        name = original
    name = re.sub(r'^ЖХК\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^ХК\s*', '', name, flags=re.IGNORECASE)
    name = name.replace('«', '').replace('»', '').replace('"', '')
    name = name.replace('-', ' ')
    name = re.sub(r'\s+', ' ', name).strip()
    return name.lower()


def normalize_team_name_rossii(name: str) -> str:
    """Как normalize_team_name, но ТОЛЬКО для Кубка России — дополнительно
    убирает уточнение города у "Динамо" (СПб, Мск и т.п.). junior.fhr.ru
    в своём расписании даёт короткое "Динамо" БЕЗ города вообще — под этим
    именем в разных группах турнира может скрываться и питерский, и
    московский клуб. VK-заголовки же часто уточняют город явно ("Динамо
    СПб", "Динамо Мск"). Раньше мы приводили ТОЛЬКО голое "Динамо" к
    "Динамо СПб" — это работало для питерских матчей, но ломало московские
    (и любые другие). Правильнее — убирать ЛЮБОЙ город, приводя все
    варианты к тому же голому виду, что и на самом junior.fhr.ru.
    Коллизий между разными "Динамо" это практически не создаёт: ключ матча
    всё равно включает СОПЕРНИКА и дату — то же самое "Динамо" против
    ДВУХ РАЗНЫХ соперников в один день даёт разные ключи.
    Для остальных турниров (Кубок СПБ/Первенство) это НЕ применяется."""
    name = normalize_team_name(name)
    name = re.sub(r'^динамо\s+(спб|мск|москва|питер)$', 'динамо', name)
    return name.strip()


def build_match_key(date_str: str, team1: str, team2: str, age: str = None) -> str:
    """Тот же формат ключа, что и в vk_to_tg_hockey_bot.py — для сопоставления.

    ВАЖНО: возраст (если известен) — ОБЯЗАТЕЛЬНАЯ часть ключа. Без него два
    разных матча (например, 2011 и 2013 г.р.) между ОДНИМИ И ТЕМИ ЖЕ двумя
    клубами в ОДИН И ТОТ ЖЕ день дают одинаковый ключ — и один матч просто
    "затирает" данные другого. Если возраст неизвестен (например, video
    playing title не сообщает его явно) — ключ строится по-старому, без
    возраста (может по-прежнему коллизировать, но это лучше, чем совсем
    не найти совпадение)."""
    t1, t2 = normalize_team_name(team1), normalize_team_name(team2)
    teams_part = "|".join(sorted([t1, t2]))
    age_part = f"::{age}" if age else ""
    return f"{date_str}::{teams_part}{age_part}"


def build_match_key_rossii(date_str: str, team1: str, team2: str, age: str = None) -> str:
    """Как build_match_key, но с normalize_team_name_rossii — используется
    ТОЛЬКО для Кубка России, чтобы "Динамо СПб" и "Динамо" совпадали."""
    t1, t2 = normalize_team_name_rossii(team1), normalize_team_name_rossii(team2)
    teams_part = "|".join(sorted([t1, t2]))
    age_part = f"::{age}" if age else ""
    return f"{date_str}::{teams_part}{age_part}"


def plain_date(date_label: str) -> str:
    """Убирает день недели из даты fhspb.ru: 'Сб 27.09.2025' -> '27.09.2025'."""
    return WEEKDAY_PREFIX_RE.sub('', date_label.strip())


def parse_match_date(date_label: str):
    """Превращает 'Сб 05.09.2026' в объект date(2026, 9, 5)."""
    day, month, year = plain_date(date_label).split('.')
    return datetime(int(year), int(month), int(day)).date()


# Стандартная длительность юношеского хоккейного матча + запас на паузы,
# овертайм, буллиты и задержку официального протокола.
GAME_DURATION = timedelta(hours=2, minutes=15)


def is_match_live(date_label: str, time_str: str) -> bool:
    """Определяет, идёт ли матч прямо СЕЙЧАС — по времени начала из
    расписания, без счёта. Пересчитывается заново при каждом прогоне,
    поэтому отметка появляется и исчезает сама, без ручного обновления."""
    try:
        match_date = parse_match_date(date_label)
        hour, minute = map(int, time_str.split(':'))
        match_start = datetime(match_date.year, match_date.month, match_date.day, hour, minute)
    except (ValueError, IndexError, AttributeError):
        return False

    now = datetime.now()
    return match_start <= now <= match_start + GAME_DURATION


def is_match_probably_over(date_label: str, time_str: str) -> bool:
    """Определяет, прошло ли уже больше GAME_DURATION с начала матча
    (и счёта при этом ещё нет) — вероятно, игра закончилась, но результат
    на fhspb.ru ещё не опубликован. В отличие от is_match_live, здесь нет
    верхней границы — пометка держится, пока не появится реальный счёт."""
    try:
        match_date = parse_match_date(date_label)
        hour, minute = map(int, time_str.split(':'))
        match_start = datetime(match_date.year, match_date.month, match_date.day, hour, minute)
    except (ValueError, IndexError, AttributeError):
        return False

    now = datetime.now()
    return now > match_start + GAME_DURATION


RESULT_GRACE_PERIOD = timedelta(minutes=45)


def is_match_worth_fast_checking(date_label: str, time_str: str) -> bool:
    """Матч либо ИДЁТ прямо сейчас, либо закончился НЕДАВНО (в пределах
    RESULT_GRACE_PERIOD после окончания игрового времени) — используется
    live_watcher.py, чтобы не пропускать матчи, где итоговый счёт
    появляется на сайте с небольшой задержкой уже ПОСЛЕ финальной сирены,
    когда is_match_live уже вернёт False."""
    try:
        match_date = parse_match_date(date_label)
        hour, minute = map(int, time_str.split(':'))
        match_start = datetime(match_date.year, match_date.month, match_date.day, hour, minute)
    except (ValueError, IndexError, AttributeError):
        return False

    now = datetime.now()
    return match_start <= now <= match_start + GAME_DURATION + RESULT_GRACE_PERIOD


def load_spb_details_cache() -> dict:
    """Кэш детальных данных (счёт по периодам, авторы шайб) для УЖЕ
    завершённых матчей — чтобы не запрашивать их у spb.fhr.ru заново на
    каждом прогоне (только один раз, когда матч впервые оказывается
    завершён). Для матчей, которые ИДУТ СЕЙЧАС, кэш не используется —
    там данные меняются, нужен свежий запрос каждый раз."""
    if SPB_DETAILS_CACHE_FILE.exists():
        return json.loads(SPB_DETAILS_CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_spb_details_cache(cache: dict) -> None:
    SPB_DETAILS_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def load_video_index() -> dict:
    if VIDEO_INDEX_FILE.exists():
        return json.loads(VIDEO_INDEX_FILE.read_text(encoding="utf-8"))
    return {}


def _entry_matches_teams(entry: dict, team1: str, team2: str) -> bool:
    """
    Лёгкая самопроверка — сверяет СОХРАНЁННЫЙ заголовок видео (video_index
    хранит его вместе со ссылкой) с названиями команд, которым мы это
    видео сейчас приписываем. Если ни одно ключевое слово команды не
    встречается в заголовке — ссылка, скорее всего, "прилипла" не к тому
    матчу (например, из-за коллизии ключей при регистрации), и лучше
    вообще не показывать ссылку, чем показать неверную.

    Специально НЕ строгая проверка (не требует точного совпадения имени
    целиком) — названия в заголовке видео и в расписании часто пишутся
    по-разному (сокращения, регистр, дефисы). Достаточно, чтобы самое
    длинное (обычно самое узнаваемое) слово каждой команды нашлось в
    тексте заголовка.
    """
    if entry.get("manual") or entry.get("verified"):
        return True  # привязано вручную или модулем сопоставления — уже проверено
    title = (entry.get("title") or "").upper()
    if not title:
        return True  # нет сохранённого заголовка — сверить не с чем, доверяем как есть

    title_no_sep = re.sub(r'[\s\-]+', '', title)

    def _team_recognized(team_name):
        words = [w for w in re.split(r'[\s\-]+', team_name) if len(w) >= 3]
        if not words:
            return True
        longest_word = max(words, key=len)
        w = longest_word.upper()
        # падежи: «ПЕТЕРБУРГА» ↔ «Петербург», «МОСКВЫ» ↔ «Москва» — сверяем основу слова
        return w in title_no_sep or (len(w) >= 6 and w[:-2] in title_no_sep)

    if _team_recognized(team1) and _team_recognized(team2):
        return True
    # Вторая попытка — с учётом синонимов (МАХ = Московская академия хоккея,
    # Петергоф = СКА Петергоф, Красная Машина = Красная Машина Юниор ...),
    # которые ведутся в team_matcher.py.
    try:
        import team_matcher as _tm
        tokens, _ = _tm.tokenize_title(entry.get("title") or "")
        groups = _tm.load_alias_groups()
        return all(_tm._team_spans(tokens, normalize_team_name(t), groups) for t in (team1, team2))
    except Exception:
        return False


def find_video_url(date_label: str, team1: str, team2: str, video_index: dict,
                    use_rossii_key: bool = False, age: str = None) -> str:
    key_fn = build_match_key_rossii if use_rossii_key else build_match_key

    if not age:
        # Возраст неизвестен — сразу запасной вариант без него (контексты,
        # где возраст в принципе не передаётся).
        key = key_fn(plain_date(date_label), team1, team2)
        entry = video_index.get(key)
        if entry and not _entry_matches_teams(entry, team1, team2):
            print(f"[WARN] Ссылка для \"{team1} vs {team2}\" не прошла самопроверку "
                  f"(сохранённый заголовок: {entry.get('title')!r}) — не показываем")
            return None
        return entry["video_url"] if entry else None

    # Возраст ИЗВЕСТЕН — сначала пробуем точный ключ с возрастом.
    key_with_age = key_fn(plain_date(date_label), team1, team2, age=age)
    entry = video_index.get(key_with_age)
    if entry:
        if not _entry_matches_teams(entry, team1, team2):
            print(f"[WARN] Ссылка для \"{team1} vs {team2}\" (возраст {age}) не прошла "
                  f"самопроверку (сохранённый заголовок: {entry.get('title')!r}) — не показываем")
            return None
        return entry["video_url"]

    # Своего видео под этим возрастом нет. Прежде чем сдаться, проверяем
    # запасной (безвозрастной) ключ — но ТОЛЬКО если он БЕЗОПАСЕН: если бы
    # существовал матч тех же двух клубов в ДРУГОМ возрасте в тот же день
    # СО своим видео (см. пример "Варяги vs Красная Машина Юниор" — 2016
    # без видео получил бы ссылку на видео 2013 года), мы бы это заметили
    # здесь — при регистрации у ТОГО матча появился бы СВОЙ age-ключ. Раз
    # ни для одного другого возраста age-ключа нет — конкурирующего матча,
    # скорее всего, не было, и запасной ключ безопасен.
    plain = plain_date(date_label)
    for other_age in ("09", "10", "11", "12", "13", "14", "15", "16", "17", "18"):
        if other_age == age:
            continue
        other_key = key_fn(plain, team1, team2, age=other_age)
        if other_key in video_index:
            return None  # найден конкурирующий возраст — запасной ключ рискован

    key = key_fn(plain, team1, team2)
    entry = video_index.get(key)
    if entry and not _entry_matches_teams(entry, team1, team2):
        print(f"[WARN] Ссылка для \"{team1} vs {team2}\" (запасной ключ) не прошла "
              f"самопроверку (сохранённый заголовок: {entry.get('title')!r}) — не показываем")
        return None
    return entry["video_url"] if entry else None


TELEGRAM_CAPTION_LIMIT = 1024

# Счёт может содержать суффикс ОТ/ПБ/ТР (овертайм/буллиты/техническое
# поражение) — отбрасываем его перед сравнением чисел.
SCORE_SUFFIX_RE = re.compile(r'(ОТ|ПБ|ТР)$')


def _breakdown_matches_score(periods_breakdown: list, effective_score: str) -> bool:
    """Проверяет, что сумма голов по периодам совпадает с уже известным
    итоговым счётом. Если нет — данные со spb.fhr.ru ещё не полные
    (сайт публикует периоды с задержкой), и кэшировать их нельзя —
    иначе разбивка "застрянет" в неполном виде навсегда (см. пример
    с итогом 10:1, где сохранился только 1-й период 2:1)."""
    parts = SCORE_SUFFIX_RE.sub('', effective_score).split(":")
    if len(parts) != 2:
        return True  # не смогли разобрать счёт — не блокируем кэширование
    try:
        expected = [int(parts[0]), int(parts[1])]
    except ValueError:
        return True

    totals = [0, 0]
    for period in periods_breakdown:
        for i, tg in enumerate(period.get("team_goals", [])):
            if i < 2:
                totals[i] += len(tg.get("goals", []))

    return totals == expected


# ---------- неполные данные завершённого матча: перепроверка с затуханием ----------
_DETAILS_RETRY_FILE = Path(__file__).parent / "spb_details_retry.json"
_DETAILS_RETRY = None


def _details_retry_state():
    global _DETAILS_RETRY
    if _DETAILS_RETRY is None:
        try:
            _DETAILS_RETRY = json.loads(_DETAILS_RETRY_FILE.read_text(encoding="utf-8"))
        except Exception:
            _DETAILS_RETRY = {}
    return _DETAILS_RETRY


def _details_retry_due(game_id):
    """Пора ли снова скачать страницу матча: 6 раз — раз в 10 мин, потом раз в час, после 30 — нет."""
    last, tries = _details_retry_state().get(str(game_id), [0, 0])
    if tries >= 30:
        return False
    return time.time() - last >= (600 if tries < 6 else 3600)


def _details_retry_mark(game_id):
    state = _details_retry_state()
    last, tries = state.get(str(game_id), [0, 0])
    state[str(game_id)] = [time.time(), tries + 1]
    try:
        _DETAILS_RETRY_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _icon(key, fallback):
    """Свой значок площадки (emoji_icons.py) или обычный эмодзи."""
    try:
        import emoji_icons
        return emoji_icons.tag(key, fallback)
    except Exception:
        return fallback


def _youtube_url(date_label, team1, team2, age, rossii=False, module="youtube_watch"):
    """Ссылка на видео матча на YouTube / RUTUBE (youtube_watch.py, rutube_watch.py) или None."""
    try:
        import importlib
        youtube_watch = importlib.import_module(module)
    except Exception:
        return None
    d = plain_date(date_label)
    keys = [build_match_key(d, team1, team2)]
    if rossii:
        keys.insert(0, build_match_key_rossii(d, team1, team2))
    for key in keys:
        url = youtube_watch.url_for(key, age)
        if url:
            return url
    return None


def format_caption(date_label: str, matches: list, video_index: dict,
                    spb_lookup: dict = None, spb_session=None,
                    spb_details_cache: dict = None, domain: str = "spb.fhr.ru",
                    age: str = None) -> str:
    spb_lookup = spb_lookup or {}
    spb_details_cache = spb_details_cache if spb_details_cache is not None else {}

    header = date_label.upper()
    # Каждый матч собираем как ДВЕ версии: полную (со спойлером авторов
    # шайб) и компактную (без него) — если у Telegram не хватит места под
    # полную версию (лимит подписи к фото — 1024 символа), заменяем самые
    # длинные спойлеры на компактную версию, пока сообщение не уместится.
    match_blocks = []  # [{full: str, compact: str, spoiler_len: int}]

    # Для турниров с группами (Кубок России) — заголовок группы вставляется
    # отдельным блоком каждый раз, когда группа меняется по сравнению с
    # предыдущим матчем. Работает только если матчи уже отсортированы по
    # группе на вызывающей стороне — сама format_caption порядок не меняет.
    last_group_label = None

    for m in matches:
        if m.get("group_label") and m["group_label"] != last_group_label:
            group_header = f"<b>{m['group_label']}</b>"
            match_blocks.append({"full": group_header, "compact": group_header, "spoiler_len": 0})
            last_group_label = m["group_label"]

        is_rossii_domain = spb_fhr_client is not None and domain == spb_fhr_client.KUBOK_ROSSII_DOMAIN
        video_url = find_video_url(date_label, m["team1"], m["team2"], video_index,
                                    use_rossii_key=is_rossii_domain, age=age)

        # Название команд — ссылка на видео ТОЛЬКО если видео уже найдено.
        # Пока видео не подтянулось — обычный текст без ссылки, чтобы сразу
        # визуально было видно, для каких матчей видео ещё не появилось.
        if video_url:
            team_line = f"{_icon('vk', '📺')} <a href=\"{video_url}\">{m['team1']} - {m['team2']}</a>"
        else:
            team_line = f"📺 {m['team1']} - {m['team2']}"
        yt_url = _youtube_url(date_label, m["team1"], m["team2"], age, is_rossii_domain)
        if yt_url:
            team_line += f"\n{_icon('youtube', '▶️')} <a href=\"{yt_url}\">Смотреть на YouTube</a>"
        rt_url = _youtube_url(date_label, m["team1"], m["team2"], age, is_rossii_domain, "rutube_watch")
        if rt_url:
            team_line += f"\n{_icon('rutube', '▶️')} <a href=\"{rt_url}\">Смотреть на RUTUBE</a>"

        # Ищем этот же матч в данных spb.fhr.ru (по нормализованным именам
        # команд + дате) — если найден, используем его game_id для ссылки
        # на протокол (богаче фактами) и для живых данных по ходу матча.
        spb_key = build_match_key(plain_date(date_label), m["team1"], m["team2"])
        spb_match = spb_lookup.get(spb_key)

        # Эффективный счёт: приоритет у fhspb.ru (официальный источник),
        # но если там результата ещё нет — берём со spb.fhr.ru/junior.fhr.ru
        # (сначала из календаря, а если и там пусто, но матч, судя по
        # времени, уже должен был закончиться — проверяем страницу самого
        # матча напрямую: там счёт иногда появляется раньше общего календаря).
        effective_score = m["score"] or (spb_match["score"] if spb_match else None)

        _direct_periods_breakdown = None
        if (not effective_score and spb_match and spb_session is not None
                and spb_fhr_client is not None and is_match_probably_over(date_label, m["time"])):
            try:
                _details = spb_fhr_client.fetch_match_live_details(spb_session, spb_match["game_id"], domain=domain)
                if _details["score"]:
                    effective_score = _details["score"]
                    _direct_periods_breakdown = _details["periods_breakdown"]
            except Exception as e:
                print(f"[WARN] Не удалось проверить страницу матча {spb_match['game_id']}: {e}")

        is_live_now = not effective_score and is_match_live(date_label, m["time"])

        live_marker = ""
        if not effective_score:
            if is_live_now:
                live_marker = " 🔴 <b>LIVE</b>"
            elif is_match_probably_over(date_label, m["time"]):
                # Больше GAME_DURATION с начала — вероятно, игра уже
                # закончилась, просто результат ещё нигде не появился.
                live_marker = " 🟢 Матч окончен"

        time_line = f"🕐 {m['time']}{live_marker}"

        # Разбивка по периодам с авторами шайб — для LIVE-матчей всегда
        # запрашиваем заново (данные меняются каждую минуту), а для УЖЕ
        # завершённых — используем кэш, чтобы не дёргать spb.fhr.ru заново
        # на каждом прогоне за все матчи сезона подряд. Работает для
        # любого матча, найденного на spb.fhr.ru — не только для live.
        periods_breakdown = None
        live_score_line = None

        if spb_match and spb_fhr_client is not None:
            if is_live_now and spb_session is not None:
                try:
                    details = spb_fhr_client.fetch_match_live_details(spb_session, spb_match["game_id"], domain=domain)
                    if details["score"]:
                        live_score_line = details["score"]
                    periods_breakdown = details["periods_breakdown"]
                except Exception as e:
                    print(f"[WARN] Не удалось получить живые данные матча {spb_match['game_id']}: {e}")
            elif effective_score:
                cached = spb_details_cache.get(spb_match["game_id"])
                cache_is_complete = cached is not None and _breakdown_matches_score(cached, effective_score)

                if cache_is_complete:
                    # В кэше уже полные, проверенные данные — сходятся с
                    # итоговым счётом, дальше запрашивать не нужно.
                    periods_breakdown = cached
                elif _direct_periods_breakdown is not None:
                    # Уже получили разбивку чуть выше (пока искали счёт
                    # напрямую на странице матча) — используем её, не
                    # запрашиваем повторно.
                    periods_breakdown = _direct_periods_breakdown
                    spb_details_cache[spb_match["game_id"]] = periods_breakdown
                elif spb_session is not None and _details_retry_due(spb_match["game_id"]):
                    _details_retry_mark(spb_match["game_id"])
                    try:
                        details = spb_fhr_client.fetch_match_live_details(spb_session, spb_match["game_id"], domain=domain)
                        periods_breakdown = details["periods_breakdown"]
                        # Сохраняем ВСЕГДА, даже если данные ещё не полностью
                        # сходятся с итоговым счётом (spb.fhr.ru иногда
                        # публикует периоды с задержкой) — иначе цитата
                        # временно пропадала бы совсем, пока данные не
                        # "дозреют". На каждом следующем прогоне будем
                        # пробовать получить более полную версию и обновим
                        # кэш, как только сумма голов сойдётся со счётом.
                        spb_details_cache[spb_match["game_id"]] = periods_breakdown
                        if not _breakdown_matches_score(periods_breakdown, effective_score):
                            print(f"[INFO] Данные матча {spb_match['game_id']} пока не полностью "
                                  f"сходятся со счётом {effective_score} — сохранили как есть, "
                                  f"попробуем дополнить на следующем прогоне.")
                    except Exception as e:
                        print(f"[WARN] Не удалось получить данные завершённого матча "
                              f"{spb_match['game_id']}: {e}")
                        if cached is not None:
                            # Сети/сайту не удалось ответить — используем то,
                            # что уже есть в кэше (пусть и неполное), лишь бы
                            # цитата не пропадала совсем.
                            periods_breakdown = cached
                elif cached is not None:
                    # Нет сессии spb.fhr.ru в этом прогоне, но в кэше хоть
                    # что-то уже есть — показываем это, не оставляем пусто.
                    periods_breakdown = cached

        if is_live_now and periods_breakdown and spb_match:
            _log_live_goals(spb_match, m, periods_breakdown, date_label)
        live_score_line_text = None                # live-счёт — на баннере, не в тексте
        if live_score_line:
            _LIVE_SCORES[spb_key] = live_score_line
            if periods_breakdown and spb_match:
                _LIVE_BREAKDOWN[spb_match["game_id"]] = periods_breakdown

        spoiler_block = None
        last_goal_line = None
        if periods_breakdown:
            # Кто именно забил — в виде раскрывающейся цитаты Telegram
            # (<blockquote expandable>), чтобы не "перегружать" сообщение
            # сразу открытым текстом при большом числе шайб — Live-счёт
            # при этом остаётся видимым как есть. Каждый гол — на своей
            # строке (не через запятую) — так проще читать при многих шайбах.
            #
            # ВО ВРЕМЯ LIVE — список развёрнут СРАЗУ (без "expandable"), а
            # сверху добавляется отдельная заметная строка "⚡ Гол!" с
            # последним по времени голом — люди, следящие за живым матчем,
            # хотят видеть это сразу, без лишнего клика.
            spoiler_lines = []
            last_goal = None  # (minute_int, team_name, number, player, period_label)
            for period in periods_breakdown:
                period_label = period.get("label") or f"{period.get('period_number', '?')}-й период"
                spoiler_lines.append(f"{period_label} ({period['score']}):")
                for tg in period["team_goals"]:
                    if tg["goals"]:
                        spoiler_lines.append(f"  {tg['name']}:")
                        for g in tg["goals"]:
                            spoiler_lines.append(f"    {g['number']}.{g['player']} {g['minute']}'")
                            try:
                                minute_int = int(str(g["minute"]).rstrip("'"))
                            except ValueError:
                                minute_int = -1
                            if last_goal is None or minute_int >= last_goal[0]:
                                last_goal = (minute_int, tg["name"], g["number"], g["player"], period_label)
                spoiler_lines.append("")  # пустая строка между периодами — легче читать

            spoiler_text = "\n".join(spoiler_lines).rstrip()
            if is_live_now:
                # Список голов показываем ТОЛЬКО пока матч идёт онлайн —
                # после окончания (счёт уже известен) список голов больше
                # НЕ показываем вообще, остаётся только итоговый счёт.
                spoiler_block = None              # список голов и счёт — на баннере
                if last_goal is not None:
                    _, g_team, g_num, g_player, g_period = last_goal
                    last_goal_line = (f"⚡ <b>Гол!</b> {g_num}.{g_player}, {g_period}, "
                                       f"{last_goal[0]}' — {g_team}")

        # Промежуточный, компактный вариант ("Голы: Команда — 9.Фамилия
        # 8', ...; Команда2 — ...") — на одну строку, без деления по
        # периодам. Используется как запасной вариант при нехватке места в
        # посте (см. ниже) — заметно короче полного блока, но не теряет
        # данные о голах полностью, в отличие от совсем компактной версии.
        one_line_summary = None
        if periods_breakdown and not is_live_now:
            goals_by_team = {}
            for period in periods_breakdown:
                for tg in period["team_goals"]:
                    if tg["goals"]:
                        goals_by_team.setdefault(tg["name"], [])
                        for g in tg["goals"]:
                            goals_by_team[tg["name"]].append(f"{g['number']}.{g['player']} {g['minute']}'")
            parts = [f"{team} — {', '.join(scorers)}" for team, scorers in goals_by_team.items()]
            if parts:
                one_line_summary = "Голы: " + "; ".join(parts)

        # "Протокол матча" появляется ТОЛЬКО после того, как матч сыгран —
        # до этого страница протокола на fhspb.ru всё равно пустая, ссылка
        # была бы бесполезной. Предпочитаем ссылку на spb.fhr.ru (там
        # больше деталей — счёт по периодам, авторы шайб), fhspb.ru — как
        # запасной вариант, если матч не нашёлся на spb.fhr.ru.
        result_lines = []
        if effective_score:
            if not RESULTS_IMAGES_ENABLED:
                result_lines.append(f"Итог матча: {score_to_keycaps(effective_score)}")
            # Пока на spb.fhr.ru матч не заполнен (нет счёта) — ссылка на протокол fhspb.ru
            protocol_link = spb_match["game_url"] if spb_match and spb_match.get("score") else m["protocol_url"]
            result_lines.append(f"📋 <a href=\"{protocol_link}\">Протокол матча</a>")

        # Сыгранный матч: время уже не нужно — счёт на картинке с результатами.
        if effective_score and RESULTS_IMAGES_ENABLED:
            common_lines = [team_line]
        else:
            common_lines = [team_line, time_line]
        if live_score_line_text:
            common_lines.append(live_score_line_text)
        # «⚡ Гол!» — тоже только на баннере

        full_lines = common_lines + ([spoiler_block] if spoiler_block else []) + result_lines
        mid_lines = common_lines + ([one_line_summary] if one_line_summary else []) + result_lines
        compact_lines = common_lines + result_lines

        match_blocks.append({
            "full": "\n".join(full_lines),
            "mid": "\n".join(mid_lines),
            "compact": "\n".join(compact_lines),
            "spoiler_len": len(spoiler_block) if spoiler_block else 0,
            "has_mid": one_line_summary is not None,
        })

    # Подпись с источниками данных — один раз в самом конце ВСЕГО поста
    # (не на каждый матч). fhspb.ru — всегда, второй источник — по домену
    # обогащения (какой из двух сайтов дал live-счёт/голы для этого поста).
    if spb_fhr_client is not None and domain == spb_fhr_client.KUBOK_ROSSII_DOMAIN:
        sources_footer = "Источники: fhspb.ru, junior.fhr.ru"
    else:
        sources_footer = "Источники: fhspb.ru, spb.fhr.ru"
    footer_block = f"\n\n<i>{sources_footer}</i>"

    def _assemble(blocks_text: list) -> str:
        return "\n".join([header, ""] + [b + "\n" for b in blocks_text]).strip()

    # Сначала пробуем "полную" версию для всех матчей сразу
    versions = [b["full"] for b in match_blocks]
    caption = _assemble(versions)

    # Резервируем место под подпись с источниками при проверке лимита —
    # иначе обрезка спойлеров могла бы решить, что место ещё есть, и
    # подпись потом не влезла бы.
    effective_limit = TELEGRAM_CAPTION_LIMIT - len(footer_block)

    # Если не помещается в лимит Telegram — сворачиваем поэтапно:
    # 1) сначала заменяем самые длинные развёрнутые блоки на компактную
    #    однострочную сводку голов (заметно короче, но данные не теряются
    #    полностью);
    # 2) если этого всё ещё недостаточно — убираем голы полностью,
    #    начиная с тех, что всё ещё длиннее всего после шага 1.
    if len(caption) > effective_limit:
        order = sorted(range(len(match_blocks)), key=lambda i: match_blocks[i]["spoiler_len"], reverse=True)
        for idx in order:
            if len(caption) <= effective_limit:
                break
            if match_blocks[idx]["spoiler_len"] == 0:
                continue
            versions[idx] = match_blocks[idx]["mid"] if match_blocks[idx]["has_mid"] else match_blocks[idx]["compact"]
            caption = _assemble(versions)

    if len(caption) > effective_limit:
        order = sorted(range(len(match_blocks)), key=lambda i: len(match_blocks[i]["mid"]), reverse=True)
        for idx in order:
            if len(caption) <= effective_limit:
                break
            if versions[idx] == match_blocks[idx]["compact"]:
                continue
            versions[idx] = match_blocks[idx]["compact"]
            caption = _assemble(versions)

    if len(caption) <= effective_limit:
        caption += footer_block

    return caption


def get_banner_path(age: str, group: str, tournament_type: str) -> Path:
    group_letter_map = {"A": "a", "А": "a", "B": "b", "Б": "b"}
    group_suffix = f"_group{group_letter_map.get(group, '')}" if group else ""
    type_suffix = "cup" if tournament_type == "КУБОК" else "champ"
    filename = f"{age}{group_suffix}_{type_suffix}.png"
    return BANNERS_DIR / filename


# ------------------- TELEGRAM -------------------

def _build_share_buttons(topic_id: int, message_id: int) -> str:
    """Две inline-кнопки под постом: подписка на канал и "поделиться" с
    прямой ссылкой именно на ЭТОТ пост. Прямая ссылка на сообщение
    продолжает открываться для просмотра даже при запрете пересылки в
    самой группе — запрет касается именно кнопки "Переслать" внутри
    Telegram, а не перехода по ссылке."""
    post_link = f"https://t.me/{TG_CHANNEL_USERNAME}/{topic_id}/{message_id}"
    share_link = f"https://t.me/share/url?url={post_link}"
    return json.dumps({
        "inline_keyboard": [[
            {"text": "Подписаться", "url": f"https://t.me/{TG_CHANNEL_USERNAME}"},
            {"text": "Поделиться", "url": share_link},
        ]]
    })


def send_photo(topic_id: int, banner_path: Path, caption: str) -> int:
    with open(banner_path, "rb") as photo_file:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            data={
                "chat_id": TG_CHAT_ID,
                "message_thread_id": topic_id,
                "caption": caption,
                "parse_mode": "HTML",
                "disable_notification": True,
            },
            files={"photo": photo_file},
            timeout=30,
        )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto error: {data}")
    message_id = data["result"]["message_id"]

    # Кнопки добавляем ОТДЕЛЬНЫМ вызовом — ссылка "Поделиться" требует
    # message_id, а его мы узнаём только ПОСЛЕ отправки самого поста.
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageReplyMarkup",
            data={
                "chat_id": TG_CHAT_ID,
                "message_id": message_id,
                "reply_markup": _build_share_buttons(topic_id, message_id),
            },
            timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Не удалось добавить кнопки под постом {message_id}: {e}")

    return message_id


def edit_caption(message_id: int, caption: str, topic_id: int = None) -> None:
    payload = {
        "chat_id": TG_CHAT_ID,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if topic_id is not None:
        payload["reply_markup"] = _build_share_buttons(topic_id, message_id)
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageCaption",
        data=payload,
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok") and "message is not modified" not in str(data.get("description", "")):
        raise RuntimeError(f"Telegram editMessageCaption error: {data}")


# ------------------- СОСТОЯНИЕ (какие дни уже опубликованы) -------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------- ЗАЩИТА ОТ ПЕРЕКРЫВАЮЩИХСЯ ПРОГОНОВ -------------------
# При частом запуске (каждые 2 минуты) один прогон может не успеть
# завершиться до начала следующего — особенно если fhspb.ru/spb.fhr.ru
# отвечают медленно. Без защиты два одновременных прогона могли бы
# конфликтовать при записи в одни и те же файлы состояния.

def _acquire_lock() -> bool:
    """Возвращает True, если блокировка получена (можно продолжать работу).
    False — если предыдущий прогон, судя по всему, ещё выполняется."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            os.kill(old_pid, 0)  # не завершает процесс — только проверяет, жив ли он
            return False  # процесс с этим PID всё ещё работает
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # "протухший" файл блокировки от аварийно завершившегося прогона — можно перезаписать
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ------------------------- ОСНОВНАЯ ЛОГИКА -------------------------

def _record_official_fixture(key, age_full, tournament_id, raw_team1=None, raw_team2=None, time=None):
    """Запоминает официальный матч вместе с возрастом (для team_matcher.py)
    и исходные названия команд (для автопополнения teams.json)."""
    parts = key.split("::")
    if len(parts) < 2 or "|" not in parts[1]:
        return
    team1, team2 = parts[1].split("|", 1)
    age = str(age_full)[-2:] if age_full else None
    _official_fixtures[f"{key}::{age}"] = {
        "date": parts[0], "team1": team1, "team2": team2,
        "age": age, "key": key, "tournament_id": tournament_id, "time": time,
    }
    if age_full and (raw_team1 or raw_team2):
        names = _schedule_team_names.setdefault(str(age_full), set())
        for n in (raw_team1, raw_team2):
            if n:
                names.add(n)


def _is_recent(date_str):
    try:
        d = datetime.strptime(date_str, "%d.%m.%Y")
    except (ValueError, TypeError):
        return False
    return d >= datetime.now() - timedelta(days=OFFICIAL_RETENTION_DAYS)


def _auto_update_teams():
    """Дописывает в teams.json команды, которые есть в расписании, но
    отсутствуют в справочнике. Ручные разделы не трогаем — новые названия
    идут в отдельные разделы auto_<год>."""
    if not AUTO_TEAMS_FILE.exists() or not _schedule_team_names:
        return
    try:
        data = json.loads(AUTO_TEAMS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] teams.json не прочитан, автопополнение пропущено: {e}")
        return
    existing = {n for lst in data.values() for n in lst}
    added = []
    for age_full, names in sorted(_schedule_team_names.items()):
        new = sorted(n for n in names if n not in existing)
        if new:
            data.setdefault(f"auto_{age_full}", []).extend(new)
            existing.update(new)
            added.extend(new)
    if added:
        AUTO_TEAMS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] В teams.json добавлены команды из расписания: {', '.join(added)}")


def _save_official_data(official_keys):
    """Сохраняет официальные матчи СЛИЯНИЕМ со старыми данными, а не
    перезаписью: если fhspb.ru в этот прогон отдал неполное расписание
    (сбой сайта/сети), уже известные матчи не теряются. Записи старше
    OFFICIAL_RETENTION_DAYS дней удаляются."""
    old_keys = []
    if OFFICIAL_MATCHES_FILE.exists():
        try:
            old_keys = json.loads(OFFICIAL_MATCHES_FILE.read_text(encoding="utf-8"))
        except Exception:
            old_keys = []
    merged_keys = {k for k in old_keys if _is_recent(k.split("::")[0])} | set(official_keys)
    OFFICIAL_MATCHES_FILE.write_text(
        json.dumps(sorted(merged_keys), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nСписок официальных матчей сохранён: {len(merged_keys)} записей "
          f"(в этом прогоне собрано {len(official_keys)})")

    old_fixtures = {}
    if OFFICIAL_FIXTURES_FILE.exists():
        try:
            for fx in json.loads(OFFICIAL_FIXTURES_FILE.read_text(encoding="utf-8")):
                if _is_recent(fx.get("date")):
                    old_fixtures[f'{fx["key"]}::{fx.get("age")}'] = fx
        except Exception:
            old_fixtures = {}
    old_fixtures.update(_official_fixtures)
    fixtures = sorted(old_fixtures.values(), key=lambda f: (f["date"][6:] + f["date"][3:5] + f["date"][:2], f["key"]))
    OFFICIAL_FIXTURES_FILE.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Расписание с возрастами сохранено: {len(fixtures)} матчей")

    _auto_update_teams()


# ================= Первенство: пометки ОТ / ПБ со spb.fhr.ru =================
PERV_OT_CACHE_FILE = Path(__file__).parent / "pervenstvo_ot_cache.json"
PERV_OT_RETRY_SECONDS = 600
PERV_SPB_TOURNAMENT_ID = "24813508"
PERV_SPB_SLUG = "pervenstvo-spb-24813508"
PERV_SPB_YEAR_IDS = {
    "2009": "24813510", "2010": "24813512", "2011": "24813519", "2012": "24813565",
    "2013": "24813568", "2014": "24813569", "2015": "24813584",
}
_PERV_FILTER_RE = re.compile(r'data-ajax-link="([^"]+)">\s*Группа\s*([А-ЯA-Z])\s*</div>')
_PERV_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_PERV_SCORE_RE = re.compile(r'<div class="cell score[^"]*">\s*(.*?)\s*</div>', re.S)
_ONE_GOAL_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


def _perv_group_ids(session, year_id):
    """{буква группы: GROUP_ID} из кнопок фильтра на странице года (пусто,
    если турнир не разбит на группы)."""
    import base64, urllib.parse
    url = f"https://spb.fhr.ru/tournaments/{PERV_SPB_SLUG}-year-{year_id}/"
    resp = session.get(url, timeout=20)
    groups = {}
    for link, letter in _PERV_FILTER_RE.findall(resp.text):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link.replace("&amp;", "&")).query)
        raw = qs.get("params", [""])[0]
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(errors="ignore")
        m = re.search(r'GROUP_ID";s:\d+:"(\d+)"', decoded)
        if m:
            groups[letter] = m.group(1)
    return groups


def _perv_calendar(session, year_id, group_id):
    """Матчи Первенства со spb.fhr.ru: [{game_id, date, team1, team2, score}].
    Разметка строк — как у Кубка России (ссылка на игру в каждой ячейке)."""
    import base64, urllib.parse
    group_part = f's:{len(group_id)}:"{group_id}"' if group_id else "N"
    php = (f'a:4:{{s:13:"TOURNAMENT_ID";s:8:"{PERV_SPB_TOURNAMENT_ID}";s:8:"GROUP_ID";{group_part};'
           f's:7:"YEAR_ID";s:8:"{year_id}";s:7:"is_ajax";b:1;}}')
    params = urllib.parse.quote(base64.b64encode(php.encode()).decode(), safe="")
    url = (f"https://spb.fhr.ru/fhr-ajax/{spb_fhr_client.AJAX_COMPONENT_PATH}/"
           f"?component=itprofit%3Acompetitions-calendar&template=.default&params={params}")
    referer = f"https://spb.fhr.ru/tournaments/{PERV_SPB_SLUG}-year-{year_id}/"
    resp = session.get(url, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}, timeout=20)
    games = []
    for row in _PERV_ROW_RE.findall(resp.text):
        gid = spb_fhr_client.ROSSII_GAME_ID_RE.search(row)
        date = spb_fhr_client.ROSSII_DATE_RE.search(row)
        teams = spb_fhr_client.ROSSII_TEAM_RE.findall(row)
        score = _PERV_SCORE_RE.search(row)
        if gid and date and len(teams) >= 2:
            games.append({"game_id": gid.group(1), "date": date.group(1),
                          "team1": re.sub(r"<[^>]+>", "", teams[0]).strip(),
                          "team2": re.sub(r"<[^>]+>", "", teams[1]).strip(),
                          "score": re.sub(r"<[^>]+>", "", score.group(1)).strip() if score else ""})
    return games


def _overtime_suffix(periods):
    """«ОТ» / «ПБ» / "" по счёту периодов со spb.fhr.ru; None — данных нет.
    Сайт выводит блок периодов дважды, а строку буллитов показывает всегда
    (часто "0:0") — поэтому решаем по счёту, а не по числу периодов."""
    n = len(periods)
    if n >= 6 and n % 2 == 0 and periods[:n // 2] == periods[n // 2:]:
        periods = periods[:n // 2]
    if len(periods) < 3:
        return None
    scored = lambda p: p.replace(" ", "") not in ("0:0", ":", "")
    if len(periods) >= 4 and scored(periods[3]):
        return "ОТ"
    if len(periods) >= 5 and scored(periods[4]):
        return "ПБ"
    return ""


def _mark_pervenstvo_overtime(age, group, matches):
    """Дописывает к счёту матчей Первенства «ОТ» / «ПБ» (на месте, в matches)."""
    todo = []
    for m in matches:
        sc = (m.get("score") or "").strip()
        g = _ONE_GOAL_RE.match(sc)
        if g and abs(int(g.group(1)) - int(g.group(2))) == 1:
            todo.append(m)
    if not todo:
        return

    try:
        cache = json.loads(PERV_OT_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    marks = cache.setdefault("marks", {})
    tries = cache.setdefault("last_try", {})

    def apply(m, suffix):
        if suffix:
            m["score"] = m["score"].strip() + suffix

    pending = []
    for m in todo:
        if m["match_id"] in marks:
            apply(m, marks[m["match_id"]])
        else:
            pending.append(m)

    slot = f"{age}{group or ''}"
    if not pending or spb_fhr_client is None or time.time() - tries.get(slot, 0) < PERV_OT_RETRY_SECONDS:
        return
    tries[slot] = time.time()

    year_id = PERV_SPB_YEAR_IDS.get(age)
    if year_id:
        session = spb_fhr_client.create_session()
        groups = _perv_group_ids(session, year_id)
        letter = {"A": "А", "B": "Б"}.get(group, group)
        group_id = groups.get(letter) if groups else None
        if not groups or group_id:
            lookup = {}
            for gm in _perv_calendar(session, year_id, group_id):
                lookup[build_match_key(gm["date"], gm["team1"], gm["team2"])] = gm
            for m in pending:
                gm = lookup.get(build_match_key(plain_date(m["date"]), m["team1"], m["team2"]))
                if not gm or not gm["score"]:
                    continue    # spb.fhr.ru ещё не внёс игру — попробуем позже
                periods = spb_fhr_client.fetch_match_live_details(session, gm["game_id"]).get("periods", [])
                suffix = _overtime_suffix(periods)
                if suffix is None:
                    continue    # периоды ещё не внесены
                marks[m["match_id"]] = suffix
                apply(m, suffix)
                if suffix:
                    print(f"[INFO] {m['date']} {m['team1']} — {m['team2']}: {m['score']} (по данным spb.fhr.ru)")
                time.sleep(1)

    PERV_OT_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ===================== Баннер с результатами в постах дня =====================
try:
    import results_banner
except Exception as _e:
    print(f"[WARN] results_banner.py недоступен ({_e}) — посты без картинки с результатами")
    results_banner = None
RESULTS_IMAGES_ENABLED = results_banner is not None
RESULTS_IMAGES_DIR = Path(__file__).parent / "results_images"
RESULTS_EDITS_PER_RUN = 15
BANNER_STYLE = "tablo"     # стиль картинок; пост в другом стиле перерисуется один раз
_results_edits_done = 0


# ---------- spb.fhr.ru для Первенства и Кубка Федерации ----------
SPB_EXTRA_SOURCES = {
    "ПЕРВЕНСТВО": ("24813508", "pervenstvo-spb-24813508", {
        "2009": "24813510", "2010": "24813512", "2011": "24813519", "2012": "24813565",
        "2013": "24813568", "2014": "24813569", "2015": "24813584"}),
    "КУБОК_ФЕДЕРАЦИИ": ("24813588", "kubok-federatsii-spb-24813588", {
        "2016": "24813589", "2017": "24813590", "2018": "24813594"}),
}
SPB_MANUAL_GROUPS = {"24813590": ["25856129", "25856164"]}   # у 2017 кнопок групп на сайте нет
SPB_GROUPS_CACHE_FILE = Path(__file__).parent / "spb_groups_cache.json"
_RESULTS_DETAILS_CACHE = {}
_LIVE_SCORES = {}        # ключ матча -> live-счёт (для баннера)
GOALS_LOG_FILE = Path(__file__).parent / "live_goals_log.jsonl"
_GOALS_SEEN = None


def _log_live_goals(spb_match, m, breakdown, date_label):
    """Записывает время, когда бот впервые увидел каждый гол (для проверки задержки)."""
    global _GOALS_SEEN
    if _GOALS_SEEN is None:
        _GOALS_SEEN = set()
        try:
            for line in GOALS_LOG_FILE.read_text(encoding="utf-8").splitlines()[-3000:]:
                _GOALS_SEEN.add(json.loads(line)["id"])
        except Exception:
            pass
    new = []
    for period in breakdown:
        label = period.get("label") or f"{period.get('period_number', '?')}-й период"
        for tg in period.get("team_goals", []):
            for g in tg.get("goals", []):
                gid = f"{spb_match['game_id']}|{label}|{g.get('minute')}|{g.get('number')}"
                if gid in _GOALS_SEEN:
                    continue
                _GOALS_SEEN.add(gid)
                new.append({"id": gid, "seen": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                            "date": plain_date(date_label), "start": m.get("time"),
                            "match": f"{m['team1']} — {m['team2']}", "team": tg.get("name"),
                            "period": label, "minute": g.get("minute"),
                            "player": f"{g.get('number')}.{g.get('player')}",
                            "score": period.get("score")})
    if new:
        with open(GOALS_LOG_FILE, "a", encoding="utf-8") as fh:
            for rec in new:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
_LIVE_BREAKDOWN = {}     # game_id -> голы по ходу матча (для баннера)
_SPB_GROUP_LINK_RE = re.compile(r'data-ajax-link="([^"]+)">\s*Группа\s*[А-ЯA-Z0-9]+\s*</div>')
_SPB_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_SPB_SCORE_RE = re.compile(r'<div class="cell score[^"]*">\s*(.*?)\s*</div>', re.S)


def _spb_extra_source(age, tournament_type):
    if tournament_type == "ПЕРВЕНСТВО":
        src = SPB_EXTRA_SOURCES["ПЕРВЕНСТВО"]
    elif tournament_type == "КУБОК" and age in ("2016", "2017", "2018"):
        src = SPB_EXTRA_SOURCES["КУБОК_ФЕДЕРАЦИИ"]
    else:
        return None
    return src if age in src[2] else None


def _has_recent_games(matches):
    today = datetime.now().date()
    for m in matches:
        try:
            delta = (parse_match_date(m["date"]) - today).days
        except Exception:
            continue
        if -3 <= delta <= 1:
            return True
    return False


def _spb_group_ids(session, slug, year_id):
    """Номера групп года на spb.fhr.ru (запоминаются в файл на весь сезон)."""
    import base64, urllib.parse
    if year_id in SPB_MANUAL_GROUPS:
        return SPB_MANUAL_GROUPS[year_id]
    try:
        cache = json.loads(SPB_GROUPS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if year_id in cache:
        return cache[year_id]
    page = session.get(f"https://spb.fhr.ru/tournaments/{slug}-year-{year_id}/", timeout=20).text
    groups = []
    for link in _SPB_GROUP_LINK_RE.findall(page):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link.replace("&amp;", "&")).query)
        raw = qs.get("params", [""])[0]
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(errors="ignore")
        m = re.search(r'GROUP_ID";s:\d+:"(\d+)"', decoded)
        if m and m.group(1) not in groups:
            groups.append(m.group(1))
    cache[year_id] = groups
    SPB_GROUPS_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return groups


def _spb_calendar(session, tournament_id, slug, year_id, group_id):
    import base64, urllib.parse
    group_part = f's:{len(group_id)}:"{group_id}"' if group_id else "N"
    php = (f'a:4:{{s:13:"TOURNAMENT_ID";s:{len(tournament_id)}:"{tournament_id}";s:8:"GROUP_ID";{group_part};'
           f's:7:"YEAR_ID";s:{len(year_id)}:"{year_id}";s:7:"is_ajax";b:1;}}')
    params = urllib.parse.quote(base64.b64encode(php.encode()).decode(), safe="")
    url = (f"https://spb.fhr.ru/fhr-ajax/{spb_fhr_client.AJAX_COMPONENT_PATH}/"
           f"?component=itprofit%3Acompetitions-calendar&template=.default&params={params}")
    referer = f"https://spb.fhr.ru/tournaments/{slug}-year-{year_id}/"
    text = session.get(url, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}, timeout=20).text
    games = []
    for row in _SPB_ROW_RE.findall(text):
        gid = spb_fhr_client.ROSSII_GAME_ID_RE.search(row)
        date = spb_fhr_client.ROSSII_DATE_RE.search(row)
        teams = spb_fhr_client.ROSSII_TEAM_RE.findall(row)
        if not (gid and date and len(teams) >= 2):
            continue
        score_m = _SPB_SCORE_RE.search(row)
        score = re.sub(r"<[^>]+>", "", score_m.group(1)).strip() if score_m else ""
        games.append({"game_id": gid.group(1), "date": date.group(1),
                      "team1": re.sub(r"<[^>]+>", "", teams[0]).strip(),
                      "team2": re.sub(r"<[^>]+>", "", teams[1]).strip(),
                      "score": score if re.search(r"\d", score) else ""})
    return games


def _spb_lookup_extra(session, age, tournament_type):
    tournament_id, slug, years = _spb_extra_source(age, tournament_type)
    year_id = years[age]
    lookup = {}
    for group_id in (_spb_group_ids(session, slug, year_id) or [None]):
        for gm in _spb_calendar(session, tournament_id, slug, year_id, group_id):
            key = build_match_key(gm["date"], gm["team1"], gm["team2"])
            lookup[key] = {"game_id": gm["game_id"], "score": gm["score"],
                           "game_url": f"https://spb.fhr.ru/games/{gm['game_id']}/"}
    return lookup


# ---------- протокол матча: вратари и штрафное время ----------
try:
    import spb_protocol
except Exception as _e:
    print(f"[WARN] spb_protocol.py недоступен ({_e}) — без вратарей и штрафов на баннере")
    spb_protocol = None
PROTOCOL_CACHE_FILE = Path(__file__).parent / "spb_protocol_cache.json"
PROTOCOL_RETRY_SECONDS = 180
_protocol_cache = None
_protocol_session = None
_PROTOCOL_SESSIONS = {}


def _protocol_summary(game_id, expected_goals, url=None):
    """Сводка протокола матча (из памяти; загрузка — один раз)."""
    global _protocol_cache, _protocol_session
    if spb_protocol is None or spb_fhr_client is None:
        return None
    if _protocol_cache is None:
        try:
            _protocol_cache = json.loads(PROTOCOL_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _protocol_cache = {}
    entry = _protocol_cache.get(str(game_id))
    if entry:
        have = sum(len(v) for v in ((entry.get("summary") or {}).get("goals") or {}).values())
        if have < expected_goals:
            entry["complete"] = False          # с тех пор забили ещё — протокол устарел, перечитать
    if entry and (entry.get("complete") or time.time() - entry.get("fetched", 0) < PROTOCOL_RETRY_SECONDS):
        return entry.get("summary")
    try:
        target = url or f"https://spb.fhr.ru/games/{game_id}/"      # протокол — с сайта самой игры
        if spb_fhr_client.KUBOK_ROSSII_DOMAIN in target:
            session = _PROTOCOL_SESSIONS.get("junior") or _PROTOCOL_SESSIONS.setdefault(
                "junior", spb_fhr_client.create_session_kubok_rossii())
        else:
            if _protocol_session is None:
                _protocol_session = spb_fhr_client.create_session()
            session = _protocol_session
        page = session.get(target, timeout=25).text
        summary = spb_protocol.summarize(page)
    except Exception as e:
        print(f"[WARN] Протокол матча {game_id} не получен: {e}")
        return entry.get("summary") if entry else None
    got = len(summary["goals"][1]) + len(summary["goals"][2])
    _protocol_cache[str(game_id)] = {"summary": summary, "fetched": time.time(),
                                     "complete": got >= expected_goals and bool(summary["goalies"][1])}
    PROTOCOL_CACHE_FILE.write_text(json.dumps(_protocol_cache, ensure_ascii=False), encoding="utf-8")
    return summary


def _protocol_extras(m, spb_lookup, score):
    """Вратари, штрафное время и (запасной вариант) голы — для строки баннера."""
    sc = results_banner.split_score(score) if results_banner else None
    sm = (spb_lookup or {}).get(build_match_key(plain_date(m["date"]), m["team1"], m["team2"]))
    if not sc or not sm:
        return {}
    summary = _protocol_summary(sm["game_id"], sc[0] + sc[1], sm.get("game_url"))
    if not summary:
        return {}
    import difflib
    def key(name):
        return re.sub(r"[^а-яa-z]", "", re.sub(r"\b(?:хк|жхк|19\d\d|20\d\d|\d\d)\b", "",
                                              name.lower().replace("ё", "е")))
    p1 = summary["teams"].get("1") or summary["teams"].get(1) or ""
    same = difflib.SequenceMatcher(None, key(p1), key(m["team1"])).ratio() >= \
        difflib.SequenceMatcher(None, key(p1), key(m["team2"])).ratio()
    a, b = ("1", "2") if same else ("2", "1")
    get = lambda d, k: d.get(k) if k in d else d.get(int(k), [])

    def goalie_line(entries):
        return ", ".join(f"{who} · {n} проп." for who, n in entries) if entries else ""
    out = {
        "goalie1": goalie_line(get(summary["goalies"], a)),
        "goalie2": goalie_line(get(summary["goalies"], b)),
        "goals1": list(get(summary["goals"], a)),
        "goals2": list(get(summary["goals"], b)),
    }
    if summary.get("penalty"):
        pen = summary["penalty"]
        out["penalty"] = (pen[0], pen[1]) if same else (pen[1], pen[0])
    return out


def _goals_for_banner(m, spb_lookup):
    """Авторы голов каждой команды для баннера: ["Попов 18'", ...] по минутам."""
    sm = (spb_lookup or {}).get(build_match_key(plain_date(m["date"]), m["team1"], m["team2"]))
    breakdown = (_RESULTS_DETAILS_CACHE.get(sm["game_id"]) or _LIVE_BREAKDOWN.get(sm["game_id"])) if sm else None
    if not breakdown:
        return [], []
    import difflib
    def key(name):
        return re.sub(r"[^а-яa-z]", "", re.sub(r"\b(?:хк|жхк|19\d\d|20\d\d|\d\d)\b", "",
                                              name.lower().replace("ё", "е")))
    k1, k2 = key(m["team1"]), key(m["team2"])
    out = ([], [])
    for period in breakdown:
        for tg in period.get("team_goals", []):
            kt = key(tg.get("name", ""))
            side = 0 if difflib.SequenceMatcher(None, kt, k1).ratio() >= \
                        difflib.SequenceMatcher(None, kt, k2).ratio() else 1
            for g in tg.get("goals", []):
                try:
                    minute = int(re.match(r"\d+", str(g.get("minute"))).group(0))
                except Exception:
                    minute = 0
                surname = (g.get("player") or "").split()[0] if g.get("player") else "?"
                out[side].append((minute, f"{surname} {g.get('minute')}'"))
    return [s for _, s in sorted(out[0])], [s for _, s in sorted(out[1])]


def _results_title(age, group, tournament_type):
    return results_banner.results_title(age, group, tournament_type) if results_banner else ""


def _results_image(state_key, banner_path, day_matches, spb_lookup, title, known_fp):
    """(картинка, отпечаток). Пока результатов нет — (None, None): обычный
    баннер. Перерисовывает, только если результаты изменились."""
    if not RESULTS_IMAGES_ENABLED:
        return None, None
    rows = []
    for m in day_matches:
        score = m.get("score")
        if not score and spb_lookup:
            sm = spb_lookup.get(build_match_key(plain_date(m["date"]), m["team1"], m["team2"]))
            score = (sm or {}).get("score")
        if not score:                              # идёт матч — live-счёт сразу на баннер
            score = _LIVE_SCORES.get(build_match_key(plain_date(m["date"]), m["team1"], m["team2"]))
        g1, g2 = _goals_for_banner(m, spb_lookup)
        extra = _protocol_extras(m, spb_lookup, score)
        p1, p2 = extra.get("goals1", []), extra.get("goals2", [])
        if (p1 or p2) and len(p1) + len(p2) >= len(g1) + len(g2):
            g1, g2 = p1, p2    # в протоколе есть передачи
        extra.pop("goals1", None), extra.pop("goals2", None)
        live = bool(score) and not m.get("score") and is_match_live(m["date"], m.get("time") or "")
        rows.append({"time": m.get("time"), "team1": m["team1"], "team2": m["team2"], "score": score,
                     "goals1": g1, "goals2": g2, "live": live or None, **extra})
    if not any(results_banner.split_score(r["score"]) for r in rows):
        return None, None
    fp = json.dumps(sorted((r["time"] or "", r["team1"], r["team2"], r["score"] or "",
                            r["goals1"], r["goals2"], r.get("goalie1", ""), r.get("goalie2", ""),
                            str(r.get("penalty")), bool(r.get("live"))) for r in rows), ensure_ascii=False)
    RESULTS_IMAGES_DIR.mkdir(exist_ok=True)
    out = RESULTS_IMAGES_DIR / (re.sub(r"[^0-9A-Za-z_.-]", "_", state_key) + ".png")
    if fp == known_fp and out.exists():
        return out, fp
    try:
        results_banner.render(banner_path, rows, day_matches[0]["date"], _group_of(state_key), title, out)
        return out, fp
    except Exception as e:
        print(f"[WARN] Не удалось нарисовать результаты для {state_key}: {e}")
        return None, known_fp


def _group_of(state_key):
    tid = state_key.split("_", 1)[0]
    for _age, group, t in CHAMPIONSHIP_CONFIG:
        if str(t) == tid:
            return group
    return None


def edit_media(message_id, image_path, caption, topic_id=None):
    """Заменяет картинку поста вместе с подписью."""
    media = json.dumps({"type": "photo", "media": "attach://photo",
                        "caption": caption, "parse_mode": "HTML"}, ensure_ascii=False)
    payload = {"chat_id": TG_CHAT_ID, "message_id": message_id, "media": media}
    if topic_id is not None:
        payload["reply_markup"] = _build_share_buttons(topic_id, message_id)
    with open(image_path, "rb") as f:
        data = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageMedia",
                             data=payload, files={"photo": f}, timeout=30).json()
    if not data.get("ok") and "message is not modified" not in str(data.get("description", "")):
        raise RuntimeError(f"Telegram editMessageMedia error: {data}")


# ===================== Праздничные ролики =====================
_FESTIVE_DAY = False          # текущий пост — праздничный: вместо картинки ролик


def _festive_mp4(image_path):
    """Ролик из готовой картинки: мигающая гирлянда и падающий снег по шапке."""
    import festive
    from PIL import Image as _Img
    src = Path(str(image_path))
    RESULTS_IMAGES_DIR.mkdir(exist_ok=True)
    out = RESULTS_IMAGES_DIR / f"{src.stem}_ny.mp4"
    festive.animate(_Img.open(src), str(out))
    return out


def _send_animation(topic_id, mp4, caption):
    with open(mp4, "rb") as fh:
        data = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendAnimation",
                             data={"chat_id": TG_CHAT_ID, "message_thread_id": topic_id, "caption": caption,
                                   "parse_mode": "HTML", "disable_notification": True},
                             files={"animation": fh}, timeout=60).json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendAnimation error: {data}")
    message_id = data["result"]["message_id"]
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageReplyMarkup",
                      data={"chat_id": TG_CHAT_ID, "message_id": message_id,
                            "reply_markup": _build_share_buttons(topic_id, message_id)}, timeout=30)
    except Exception:
        pass
    return message_id


_send_photo_still = send_photo
_edit_media_still = edit_media


def send_photo(topic_id, banner_path, caption):
    if _FESTIVE_DAY:
        try:
            return _send_animation(topic_id, _festive_mp4(banner_path), caption)
        except Exception as e:
            print(f"[WARN] Праздничный ролик не собрался ({e}) — отправляю картинку")
    return _send_photo_still(topic_id, banner_path, caption)


def edit_media(message_id, image_path, caption, topic_id=None):
    if _FESTIVE_DAY:
        try:
            mp4 = _festive_mp4(image_path)
            media = json.dumps({"type": "animation", "media": "attach://anim", "caption": caption,
                                "parse_mode": "HTML"}, ensure_ascii=False)
            payload = {"chat_id": TG_CHAT_ID, "message_id": message_id, "media": media}
            if topic_id is not None:
                payload["reply_markup"] = _build_share_buttons(topic_id, message_id)
            with open(mp4, "rb") as fh:
                data = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageMedia",
                                     data=payload, files={"anim": fh}, timeout=60).json()
            if data.get("ok") or "message is not modified" in str(data.get("description", "")):
                return
            raise RuntimeError(data)
        except Exception as e:
            print(f"[WARN] Праздничный ролик не обновился ({e}) — обновляю картинкой")
    return _edit_media_still(message_id, image_path, caption, topic_id)


def _update_day_post(existing, state, state_key, topic_id, banner_path, caption,
                     day_matches, spb_lookup, title, label, dry_run):
    global _results_edits_done
    existing["banner"], existing["title"] = str(banner_path), title   # для live_watcher.py
    img, img_fp = _results_image(state_key, banner_path, day_matches, spb_lookup, title,
                                 existing.get("image_fp"))
    image_changed = img_fp != existing.get("image_fp") or existing.get("style") != BANNER_STYLE
    caption_changed = existing.get("caption") != caption
    if not image_changed and not caption_changed:
        return
    if image_changed and _results_edits_done >= RESULTS_EDITS_PER_RUN:
        return    # лимит замен картинок за прогон — остальное на следующем прогоне
    print(f"\n[ОБНОВЛЕНИЕ{' + картинка' if image_changed else ''}] {label}")
    print(caption)
    if dry_run:
        print("  (dry-run — не отредактировано)")
        return
    if image_changed:
        _results_edits_done += 1
        edit_media(existing["message_id"], img or banner_path, caption, topic_id)
        existing["image_fp"] = img_fp
        existing["style"] = BANNER_STYLE
    else:
        edit_caption(existing["message_id"], caption, topic_id=topic_id)
    existing["caption"] = caption
    save_state(state)
    time.sleep(1)


# ===================== Запасной источник: spb.fhr.ru =====================
_FALLBACK_DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_FALLBACK_KEEP_UPPER = {"СКА", "ХКД", "ЖХК", "СШ", "СШОР", "МАХ", "ЦСКА", "ВВС"}


def _spb_display_name(name, age2):
    """'СКА-СЕРЕБРЯНЫЕ ЛЬВЫ' -> 'СКА Серебряные Львы 11';
    'КРАСНАЯ МАШИНА ЮНИОР - 2' -> 'Красная Машина Юниор 11 2' (как на fhspb.ru)."""
    n = re.sub(r'[\"«»]', " ", name or "")
    n = re.sub(r"(?<![А-ЯЁA-Zа-яёa-z])ХК(?![А-ЯЁA-Zа-яёa-z])", " ", n)
    n = re.sub(r"\b(?:19|20)\d{2}\b", " ", n)
    n = re.sub(r"№\s*(\d+)", r"№\1", n)          # "СШ № 1" -> "СШ №1": номер школы, не состав
    squad = re.search(r"\s-\s*(\d)\s*$|\s(\d)\s*$", n)
    if squad:
        n = n[:squad.start()]
        squad = squad.group(1) or squad.group(2)
    n = re.sub(r"(?<=\w)-(?=\w)", " ", n)
    words = []
    for w in n.split():
        if w in _FALLBACK_KEEP_UPPER or len(w) <= 3 and w.isupper():
            words.append(w)
        elif w.isupper() or w.islower():
            words.append(w.capitalize())
        else:
            words.append(w)
    return " ".join(words) + f" {age2}" + (f" {squad}" if squad else "")


def _spb_calendar_rows(session, tournament_id, slug, year_id, group_id):
    """Календарь Первенства / Кубка Федерации на spb.fhr.ru, со временем матча."""
    import base64, urllib.parse
    group_part = f's:{len(group_id)}:"{group_id}"' if group_id else "N"
    php = (f'a:4:{{s:13:"TOURNAMENT_ID";s:{len(tournament_id)}:"{tournament_id}";s:8:"GROUP_ID";{group_part};'
           f's:7:"YEAR_ID";s:{len(year_id)}:"{year_id}";s:7:"is_ajax";b:1;}}')
    params = urllib.parse.quote(base64.b64encode(php.encode()).decode(), safe="")
    url = (f"https://spb.fhr.ru/fhr-ajax/{spb_fhr_client.AJAX_COMPONENT_PATH}/"
           f"?component=itprofit%3Acompetitions-calendar&template=.default&params={params}")
    referer = f"https://spb.fhr.ru/tournaments/{slug}-year-{year_id}/"
    text = session.get(url, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}, timeout=20).text
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        gid = spb_fhr_client.ROSSII_GAME_ID_RE.search(row)
        date = spb_fhr_client.ROSSII_DATE_RE.search(row)
        tm = spb_fhr_client.ROSSII_TIME_RE.search(row)
        teams = spb_fhr_client.ROSSII_TEAM_RE.findall(row)
        if not (gid and date and len(teams) >= 2):
            continue
        sc = re.search(r'<div class="cell score[^"]*">\s*(.*?)\s*</div>', row, re.S)
        score = re.sub(r"<[^>]+>", "", sc.group(1)).strip() if sc else ""
        rows.append({"game_id": gid.group(1), "date": date.group(1), "time": tm.group(1) if tm else "",
                     "team1": re.sub(r"<[^>]+>", "", teams[0]).strip(),
                     "team2": re.sub(r"<[^>]+>", "", teams[1]).strip(),
                     "score": score if re.search(r"\d", score) else None,
                     "game_url": f"https://spb.fhr.ru/games/{gid.group(1)}/"})
    return rows


def _spb_fallback_matches(age, group, tournament_type, tournament_id):
    """Матчи на сегодня и завтра со spb.fhr.ru в формате parse_matches();
    None — для этого турнира запасного источника нет."""
    if spb_fhr_client is None:
        return None
    session = spb_fhr_client.create_session()
    src = _spb_extra_source(age, tournament_type)
    if src:
        sp_tid, slug, years = src
        gids = _spb_group_ids(session, slug, years[age]) or [None]
        if tournament_type == "ПЕРВЕНСТВО" and group and len(gids) > 1:
            idx = 0 if group in ("A", "А") else 1
            gids = [gids[idx]] if idx < len(gids) else gids
        raw = []
        for gid in gids:
            raw += _spb_calendar_rows(session, sp_tid, slug, years[age], gid)
    elif tournament_type == "КУБОК" and age in getattr(spb_fhr_client, "KUBOK_SPB_YEAR_IDS", {}):
        raw = spb_fhr_client.fetch_calendar_matches(session, spb_fhr_client.KUBOK_SPB_TOURNAMENT_ID,
                                                    spb_fhr_client.KUBOK_SPB_YEAR_IDS[age])
    else:
        return None

    today, age2 = datetime.now().date(), age[-2:]
    out = []
    for g in raw:
        try:
            d = datetime.strptime(g["date"], "%d.%m.%Y").date()
        except Exception:
            continue
        if not 0 <= (d - today).days <= 1:
            continue
        score = None
        sm = re.match(r"\s*(\d+)\s*:\s*(\d+)\s*(.*)$", g.get("score") or "")
        if sm:
            score = f"{sm.group(1)} : {sm.group(2)}{sm.group(3).strip()}"
        out.append({"date": f"{_FALLBACK_DAYS[d.weekday()]} {g['date']}", "time": g.get("time") or "",
                    "arena": "", "tournament_id": str(tournament_id), "match_id": f"spb{g['game_id']}",
                    "team1": _spb_display_name(g["team1"], age2), "team2": _spb_display_name(g["team2"], age2),
                    "score": score, "protocol_url": g["game_url"]})
    return out


def _cloud_skip(prefix):
    """В облаке (CLOUD_FOCUS=1) — обрабатывать только турниры с постами на вчера/сегодня/завтра.
    В 21:50–22:30 — все турниры, чтобы вышли посты на завтра. prefix — начало ключа поста
    (номер турнира fhspb.ru или ключ турнира junior.fhr.ru)."""
    if os.environ.get("CLOUD_FOCUS") != "1":
        return False
    now = datetime.now()
    if (21, 50) <= (now.hour, now.minute) <= (22, 30):
        return False
    global _CLOUD_ACTIVE
    if _CLOUD_ACTIVE is None:
        days = {(now + timedelta(days=d)).strftime("%d.%m.%Y") for d in (-1, 0, 1)}
        try:
            keys = json.loads(STATE_FILE.read_text(encoding="utf-8")).keys()
        except Exception:
            keys = []
        _CLOUD_ACTIVE = {k.split("_", 1)[0] for k in keys if any(d in k for d in days)}
    return str(prefix) not in _CLOUD_ACTIVE


_CLOUD_ACTIVE = None


def process_tournament(age: str, group: str, tournament_type: str, tournament_id: int,
                        state: dict, video_index: dict, dry_run: bool, official_keys: set = None,
                        month: int = None, spb_session=None, spb_details_cache: dict = None,
                        live_cache: dict = None) -> None:
    if _cloud_skip(str(tournament_id)):                    # облако: турнир без игр рядом — пропуск
        return
    topic_id = TOPIC_IDS[age]

    _from_spb = False
    try:
        matches = parse_matches(fetch_schedule_html(tournament_id, month=month))
    except Exception as fhspb_error:
        matches = _spb_fallback_matches(age, group, tournament_type, tournament_id)
        if matches is None:
            raise
        _from_spb = True
        print(f"[INFO] fhspb.ru недоступен — {age} г.р. {group or ''} {tournament_type}: "
              f"посты на сегодня/завтра по данным spb.fhr.ru ({len(matches)} матчей)")
    if tournament_type == "ПЕРВЕНСТВО":
        try:
            _mark_pervenstvo_overtime(age, group, matches)
        except Exception as e:
            print(f"[WARN] Пометки ОТ/ПБ для {age} {group or ''}: {e}")

    if not matches:
        return

    # Собираем ключи ВСЕХ матчей турнира (независимо от того, публикуем ли
    # мы пост прямо сейчас) — этот список используется VK-ботом, чтобы не
    # дублировать официальные матчи турнира в общий чат как "Товарищескую
    # игру". Собираем даже если баннер не найден — список должен быть полным.
    if official_keys is not None:
        for m in matches:
            key = build_match_key(plain_date(m["date"]), m["team1"], m["team2"])
            official_keys.add(key)
            _record_official_fixture(key, age, tournament_id, m["team1"], m["team2"], time=m.get("time"))

    # Обогащение данными spb.fhr.ru — доступно ТОЛЬКО для Кубка СПБ и
    # только для возрастов, чей внутренний YEAR_ID уже подтверждён (см.
    # spb_fhr_client.py). Строим справочник {ключ_матча: {game_id, game_url}}
    # один раз на турнир — используется дальше в format_caption для ссылки
    # на протокол и живых данных по ходу матча.
    spb_lookup = {}
    if (spb_session is not None and spb_fhr_client is not None
            and tournament_type == "КУБОК" and age in spb_fhr_client.KUBOK_SPB_YEAR_IDS):
        try:
            spb_matches = spb_fhr_client.fetch_calendar_matches(
                spb_session, spb_fhr_client.KUBOK_SPB_TOURNAMENT_ID,
                spb_fhr_client.KUBOK_SPB_YEAR_IDS[age],
            )
            for sm in spb_matches:
                key = build_match_key(sm["date"], sm["team1"], sm["team2"])
                spb_lookup[key] = {"game_id": sm["game_id"], "game_url": sm["game_url"],
                                    "score": sm["score"]}
        except Exception as e:
            print(f"[WARN] spb.fhr.ru недоступен для {age} г.р. КУБОК: {e}")
    if (not spb_lookup and spb_session is not None and spb_fhr_client is not None
            and _spb_extra_source(age, tournament_type) and _has_recent_games(matches)):
        try:
            spb_lookup = _spb_lookup_extra(spb_session, age, tournament_type)
        except Exception as e:
            print(f"[WARN] spb.fhr.ru недоступен для {age} г.р. {tournament_type}: {e}")

    banner_path = get_banner_path(age, group, tournament_type)
    base_banner_path = banner_path
    global _FESTIVE_DAY
    _FESTIVE_DAY = False
    if not banner_path.exists():
        print(f"[WARN] Баннер не найден: {banner_path} — пропускаю {age}/{group}/{tournament_type}")
        return

    # Группируем матчи по дате
    by_date = {}
    for m in matches:
        by_date.setdefault(m["date"], []).append(m)

    for date_label, day_matches in by_date.items():
        state_key = f"{tournament_id}_{date_label}"
        try:
            import festive
            # Праздничный день: ролик (гирлянда мигает, снег падает) из обычного баннера;
            # если ролики собрать нельзя — праздничная картинка
            _FESTIVE_DAY = festive.in_season(festive.date_of(date_label)) and festive.can_animate()
            banner_path = base_banner_path if _FESTIVE_DAY else festive.season_banner(base_banner_path, date_label)
        except Exception:
            _FESTIVE_DAY, banner_path = False, base_banner_path
        if _from_spb and state_key in state:
            continue    # запасной источник: опубликованные посты не трогаем, ждём fhspb.ru

        # Сохраняем всё необходимое для быстрого пересчёта этого дня БЕЗ
        # повторного похода на fhspb.ru — использует отдельный, часто
        # запускаемый процесс live_watcher.py, чтобы обновлять LIVE-матчи
        # намного чаще (раз в 30-60 секунд), не делая полный обход всех
        # турниров каждый раз.
        if live_cache is not None:
            live_cache[state_key] = {
                "date_label": date_label,
                "matches": day_matches,
                "spb_lookup": spb_lookup,
                "age": age[-2:],
                "domain": "spb.fhr.ru",
                "topic_id": topic_id,
            }

        caption = format_caption(date_label, day_matches, video_index, spb_lookup, spb_session,
                                  spb_details_cache, age=age[-2:])

        existing = state.get(state_key)

        if existing is None:
            # Публикуем пост только если день игры уже сегодня, завтра, либо
            # уже прошёл (например, результат появился с опозданием). Дни,
            # что дальше чем "завтра" — пока пропускаем; бот вернётся к ним
            # на одном из следующих прогонов, когда они станут ближе.
            try:
                match_date = parse_match_date(date_label)
                days_until = (match_date - datetime.now().date()).days
            except (ValueError, IndexError):
                days_until = 0  # если дату не удалось разобрать — не блокируем публикацию

            if days_until > 1:
                continue
            # Пост на ЗАВТРАШНИЕ игры — только вечером накануне, после
            # PUBLISH_EVE_HOUR (игры сегодня/прошедшие публикуются сразу).
            if days_until == 1 and datetime.now().hour < PUBLISH_EVE_HOUR:
                continue

            print(f"\n[НОВЫЙ ПОСТ] {age} г.р. {group or ''} {tournament_type} — {date_label}")
            print(caption)
            if not dry_run:
                img, img_fp = _results_image(state_key, banner_path, day_matches, spb_lookup,
                                             _results_title(age, group, tournament_type), None)
                message_id = send_photo(topic_id, img or banner_path, caption)
                state[state_key] = {
                    "message_id": message_id,
                    "caption": caption,
                    "image_fp": img_fp,
                    "style": BANNER_STYLE,
                }
                save_state(state)
                time.sleep(1)  # вежливая пауза между запросами к Telegram
            else:
                print("  (dry-run — не отправлено)")
        else:
            # День уже опубликован — сравниваем ИТОГОВЫЙ ТЕКСТ подписи, а не
            # отдельные поля (счёт/видео). Так под обновление автоматически
            # попадает и изменение данных, И изменение самого формата
            # отображения (например, если мы позже поменяем стиль постов) —
            # без этого старые посты застревали бы в старом виде навсегда.
            _update_day_post(existing, state, state_key, topic_id, banner_path, caption,
                             day_matches, spb_lookup, _results_title(age, group, tournament_type),
                             f"{age} г.р. {group or ''} {tournament_type} — {date_label}", dry_run)
    _FESTIVE_DAY = False


KUBOK_ROSSII_BANNER = BANNERS_DIR / "2011_kubokrossii.png"


JUNIOR_TOURNAMENTS = [
    # key — начало ключа постов; slug None — Кубок России через fetch_kubok_rossii_matches
    {"key": "kubokrossii", "name": "Кубок России", "label": "КУБОК РОССИИ", "age": "2011",
     "slug": None, "banner": "2011_kubokrossii.png"},
    {"key": "territoriya15", "name": "Территория хоккея U15", "label": "ТЕРРИТОРИЯ ХОККЕЯ U15", "age": "2012",
     "slug": "territoriya-khokkeya-u15-26308586", "banner": "2012_territoriya.png"},
    {"key": "territoriya14", "name": "Территория хоккея U14", "label": "ТЕРРИТОРИЯ ХОККЕЯ U14", "age": "2013",
     "slug": "territoriya-khokkeya-u14-26308525", "banner": "2013_territoriya.png"},
]


def process_kubok_rossii(state: dict, dry_run: bool, spb_rossii_session=None,
                          official_keys: set = None, spb_details_cache: dict = None,
                          video_index: dict = None, live_cache: dict = None) -> None:
    """Все турниры junior.fhr.ru из JUNIOR_TOURNAMENTS — по очереди, сбой одного не мешает другим."""
    if spb_fhr_client is None or spb_rossii_session is None:
        return
    for cfg in JUNIOR_TOURNAMENTS:
        try:
            _process_junior(cfg, state, dry_run, spb_rossii_session, official_keys,
                            spb_details_cache, video_index, live_cache)
        except Exception as e:
            print(f"[WARN] {cfg['name']}: {e}")


def _process_junior(cfg, state, dry_run, session, official_keys, spb_details_cache, video_index, live_cache):
    if _cloud_skip(cfg["key"]):
        return
    age, name, label = cfg["age"], cfg["name"], cfg["label"]
    topic_id = TOPIC_IDS[age]
    banner = BANNERS_DIR / cfg["banner"]
    try:
        if cfg["slug"] is None:
            junior_matches = spb_fhr_client.fetch_kubok_rossii_matches(session)
            order = list(spb_fhr_client.KUBOK_ROSSII_GROUP_IDS.keys())
        else:
            junior_matches, order = spb_fhr_client.fetch_junior_matches(
                session, cfg["slug"], cfg["slug"].rsplit("-", 1)[1])
    except Exception as e:
        print(f"[WARN] {name} недоступен: {e}")
        return
    if not junior_matches:
        return
    if cfg["slug"] is None:
        # Справочник участников Кубка России — VK-бот ищет по нему видео из групп Кубка России
        teams = sorted({t for rm in junior_matches for t in (rm["team1"], rm["team2"])})
        KUBOK_ROSSII_TEAMS_FILE.write_text(json.dumps(teams, ensure_ascii=False, indent=2), encoding="utf-8")

    matches, spb_lookup = [], {}
    tag = "kubok_rossii" if cfg["key"] == "kubokrossii" else cfg["key"]
    for rm in junior_matches:
        m = {"date": rm["date"], "time": rm["time"], "arena": "", "team1": rm["team1"], "team2": rm["team2"],
             "score": rm["score"], "protocol_url": rm["game_url"], "group_label": rm.get("group")}
        matches.append(m)
        key = build_match_key(plain_date(m["date"]), m["team1"], m["team2"])
        if official_keys is not None:
            official_keys.add(key)
            _record_official_fixture(key, age, tag, time=m.get("time"))
        spb_lookup[key] = {"game_id": rm["game_id"], "game_url": rm["game_url"], "score": rm["score"]}

    if not banner.exists():
        print(f"[WARN] Баннер не найден: {banner} — пропускаю {name}")
        return

    by_date = {}
    for m in matches:
        by_date.setdefault(m["date"], []).append(m)
    group_order = {g: i for i, g in enumerate(order)}
    for day_matches in by_date.values():
        day_matches.sort(key=lambda m: (group_order.get(m.get("group_label"), 999), m.get("time") or ""))

    for date_label, day_matches in by_date.items():
        state_key = f"{cfg['key']}_{date_label}"
        if live_cache is not None:
            live_cache[state_key] = {"date_label": date_label, "matches": day_matches, "spb_lookup": spb_lookup,
                                     "age": age[-2:], "domain": spb_fhr_client.KUBOK_ROSSII_DOMAIN,
                                     "topic_id": topic_id}
        caption = format_caption(date_label, day_matches, video_index or {}, spb_lookup, session,
                                 spb_details_cache, domain=spb_fhr_client.KUBOK_ROSSII_DOMAIN, age=age[-2:])
        existing = state.get(state_key)
        if existing is None:
            try:
                days_until = (parse_match_date(date_label) - datetime.now().date()).days
            except (ValueError, IndexError):
                days_until = 0
            if days_until > 1:
                continue
            if days_until == 1 and datetime.now().hour < PUBLISH_EVE_HOUR:
                continue
            print(f"\n[НОВЫЙ ПОСТ] {age} г.р. {label} — {date_label}")
            print(caption)
            if not dry_run:
                img, img_fp = _results_image(state_key, banner, day_matches, spb_lookup, f"{name} {age} г.р.", None)
                message_id = send_photo(topic_id, img or banner, caption)
                state[state_key] = {"message_id": message_id, "caption": caption, "image_fp": img_fp,
                                    "style": globals().get("BANNER_STYLE", "")}
                save_state(state)
                time.sleep(1)
            else:
                print("  (dry-run — не отправлено)")
        else:
            _update_day_post(existing, state, state_key, topic_id, banner, caption, day_matches, spb_lookup,
                             f"{name} {age} г.р.", f"{age} г.р. {label} — {date_label}", dry_run)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Реальная отправка в Telegram")
    parser.add_argument("--dry-run", action="store_true", help="Только показать, без отправки")
    parser.add_argument("--test-last-season", action="store_true",
                         help="Тестовый прогон на TournamentID прошлого сезона (6367)")
    args = parser.parse_args()

    if not args.live and not args.dry_run:
        print("Укажите --dry-run (тест) или --live (реальная отправка)")
        sys.exit(1)

    if not _acquire_lock():
        print("[INFO] Предыдущий прогон, судя по всему, ещё выполняется — пропускаем этот запуск.")
        return

    try:
        _run(args)
    finally:
        _release_lock()


def _run(args) -> None:
    dry_run = not args.live
    state = load_state()
    video_index = load_video_index()
    try:                                       # YouTube-каналы: не чаще раза в 10 минут
        import youtube_watch
        youtube_watch.refresh()
    except Exception as e:
        print(f"[WARN] YouTube: {e}")
    try:                                       # RUTUBE-каналы: так же, не чаще раза в 10 минут
        import rutube_watch
        rutube_watch.refresh()
    except Exception as e:
        print(f"[WARN] RUTUBE: {e}")
    official_keys = set()
    spb_details_cache = load_spb_details_cache()
    globals()["_RESULTS_DETAILS_CACHE"] = spb_details_cache   # голы для баннера с результатами
    # Снимок всех матчей дня (только там, где доступно обогащение с
    # spb.fhr.ru/junior.fhr.ru — Кубок СПБ и Кубок России) — сохраняется в
    # LIVE_SNAPSHOT_FILE в конце прогона. Отдельный, часто запускаемый
    # процесс live_watcher.py использует этот снимок, чтобы быстро (раз в
    # 30-60 секунд) обновлять именно LIVE-матчи, не делая полный обход
    # всех турниров каждый раз — Первенство сюда не попадает, там нет
    # источника живых данных по голам.
    live_cache = {}

    # Одна сессия spb.fhr.ru на весь прогон — переиспользуется для всех
    # возрастов Кубка СПБ (не нужно заново проходить "рукопожатие" сессии
    # на каждый турнир). Если сайт недоступен — просто продолжаем без
    # обогащения, это не критично для основной функциональности.
    spb_session = None
    if spb_fhr_client is not None:
        try:
            spb_session = spb_fhr_client.create_session()
        except Exception as e:
            print(f"[WARN] Не удалось создать сессию spb.fhr.ru, продолжаем без обогащения: {e}")

    # Отдельная сессия для junior.fhr.ru (Кубок России) — ДРУГОЙ домен,
    # сессия/PHPSESSID со spb.fhr.ru здесь не подходит.
    spb_rossii_session = None
    if spb_fhr_client is not None:
        try:
            spb_rossii_session = spb_fhr_client.create_session_kubok_rossii()
        except Exception as e:
            print(f"[WARN] Не удалось создать сессию junior.fhr.ru (Кубок России): {e}")

    if args.test_last_season:
        # Тестовый прогон на реальных прошлогодних данных одного турнира,
        # чтобы проверить всю логику от парсинга до формирования поста.
        print("=== ТЕСТОВЫЙ ПРОГОН НА ДАННЫХ ПРОШЛОГО СЕЗОНА (TournamentID=6367) ===")
        process_tournament("2011", "Б", "ПЕРВЕНСТВО", 6367, state, video_index, dry_run=True)
        return

    for age, tournament_id in CUP_CONFIG.items():
        try:
            process_tournament(age, None, "КУБОК", tournament_id, state, video_index, dry_run,
                                official_keys=official_keys, spb_session=spb_session,
                                spb_details_cache=spb_details_cache, live_cache=live_cache)
        except Exception as e:
            print(f"[ERROR] {age}/КУБОК: {e}")
        time.sleep(4)  # пауза между запросами к fhspb.ru — не грузить сайт подряд

    # Кубок России (2011 г.р.) — публикуется в ту же тему 2011 г.р., что и
    # Кубок СПБ/Первенство, отдельными постами на день.
    try:
        process_kubok_rossii(state, dry_run, spb_rossii_session=spb_rossii_session,
                              official_keys=official_keys, spb_details_cache=spb_details_cache,
                              video_index=video_index, live_cache=live_cache)
    except Exception as e:
        print(f"[ERROR] Кубок России: {e}")

    for age, group, tournament_id in CHAMPIONSHIP_CONFIG:
        try:
            process_tournament(age, group, "ПЕРВЕНСТВО", tournament_id, state, video_index, dry_run,
                                official_keys=official_keys, spb_session=spb_session,
                                spb_details_cache=spb_details_cache, live_cache=live_cache)
        except Exception as e:
            print(f"[ERROR] {age}/{group}/ПЕРВЕНСТВО: {e}")
        time.sleep(4)

    # Сохраняем список ключей всех официальных матчей турнира — VK-бот
    # сверяется с ним, чтобы не дублировать эти матчи в общий чат.
    _save_official_data(official_keys)

    save_spb_details_cache(spb_details_cache)
    print(f"Кэш деталей spb.fhr.ru: {len(spb_details_cache)} завершённых матчей")

    LIVE_SNAPSHOT_FILE.write_text(
        json.dumps(live_cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Снимок для live_watcher.py сохранён: {len(live_cache)} постов дня")

    print("\nГотово.")


if __name__ == "__main__":
    main()
