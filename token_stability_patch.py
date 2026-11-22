#!/usr/bin/env python3
"""
Патч стабильного refresh VK-токена.

Что делает:
1. Файловый lock вокруг refresh (защита от гонки)
2. При error 5 — несколько попыток refresh с паузой
3. После успешного refresh сразу обновляет глобальный токен
4. Не падает SystemExit сразу — пробует service_key и шлёт один алерт
5. Сохраняет новый refresh_token, если VK его вернул

Запуск из папки проекта:
    python3 token_stability_patch.py
    python3 token_stability_patch.py --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRY = False


def backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak_token")
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


NEW_TOKEN_FUNCS = r'''
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

'''


def patch_bot() -> None:
    path = ROOT / "vk_to_tg_hockey_bot.py"
    if not path.exists():
        print("✗ vk_to_tg_hockey_bot.py не найден")
        return
    print("\n== vk_to_tg_hockey_bot.py ==")
    backup(path)
    text = path.read_text(encoding="utf-8")

    start_marker = "def load_tokens() -> dict:"
    end_marker = "def _vk_service_key():"
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 or end < 0:
        print("  ⚠ не нашёл границы token-функций")
        return

    text = text[:start] + NEW_TOKEN_FUNCS + "\n\n" + text[end:]
    print("  ✓ заменены load/save/refresh с lock и retry")

    old_err5 = '''        if code == 5:  # User authorization failed
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
                return data, token'''

    new_err5 = '''        if code == 5:  # User authorization failed
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
                return data, token'''

    text = replace_once(text, old_err5, new_err5, "error 5 → retry + service_key fallback")

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

    patch_bot()

    print("\n=== Готово ===")
    if not DRY:
        print("Дальше:")
        print("  python3 -m py_compile vk_to_tg_hockey_bot.py")
        print("  ./restart_vkbot.sh")
        print("Бэкап: vk_to_tg_hockey_bot.py.bak_token")


if __name__ == "__main__":
    main()
