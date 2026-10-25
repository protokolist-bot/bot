"""
delete_old_transcripts.py
=========================
Одноразовый скрипт: удаляет ВСЕ транскрипты, накопившиеся в аккаунте AssemblyAI.
Запуск на сервере из папки бота:
    venv/bin/python delete_old_transcripts.py
"""

import httpx

from config import ASSEMBLYAI_API_KEY

API_BASE = "https://api.assemblyai.com/v2"
HEADERS = {"authorization": ASSEMBLYAI_API_KEY}


def main() -> None:
    if not ASSEMBLYAI_API_KEY:
        print("ASSEMBLYAI_API_KEY не задан в .env")
        return

    url = f"{API_BASE}/transcript?limit=200"
    deleted, failed = 0, 0

    # Листаем страницы списка (от новых к старым), пока они есть.
    for _ in range(50):
        data = httpx.get(url, headers=HEADERS, timeout=30).json()
        for item in data.get("transcripts", []):
            tid = item["id"]
            resp = httpx.delete(f"{API_BASE}/transcript/{tid}", headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                deleted += 1
                print(f"Удалён: {tid}")
            else:
                failed += 1
                print(f"Не удалось удалить {tid}: {resp.status_code}")
        url = (data.get("page_details") or {}).get("prev_url")
        if not url:
            break

    print(f"\nГотово. Удалено: {deleted}, ошибок: {failed}")


if __name__ == "__main__":
    main()
