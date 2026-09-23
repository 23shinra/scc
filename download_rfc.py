#!/usr/bin/env python3
"""Скачивает файлы раздела «Цены и тарифы» РФЦ в папки год/месяц/тип."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.client import InvalidURL
from pathlib import Path

BASE = "https://rfc.kz"
PAGE = f"{BASE}/ru/single-purchaser-of-electricity/prices-and-rates/"
USER_AGENT = "Mozilla/5.0 (compatible; rfc-prices-downloader/1.0)"
START_YEAR = 2020
MONTHS = [f"{month:02d}" for month in range(1, 13)]
PAUSE_SECONDS = 0.4
ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "downloads"
DEFAULT_DB = ROOT / "data" / "rfc_files.db"

FOLDERS = {
    "notes": "Примечания",
    "decode": "Расшифровка",
    "res": "Тариф ВИЭ",
    "base": "Базовые цены",
    "invest": "Инвест тариф",
    "intergov": "Межправ тариф",
    "fact": "Факт потребления",
    "other": "Прочее",
}

DOC_RE = re.compile(
    r'<a href="([^"]+)" class="corporative-docs".*?<div class="name">([^<]*)</div>',
    re.DOTALL,
)
HASH_NAME_RE = re.compile(r"^[a-z0-9]{16,}\.[a-z0-9]{2,5}$", re.IGNORECASE)


def current_year() -> int:
    return datetime.now().year


def available_years() -> list[int]:
    return list(range(START_YEAR, current_year() + 1))


def normalize(text: str) -> str:
    return text.casefold().replace("_", " ").replace("ё", "е")


def classify(text: str) -> str:
    folded = normalize(text)
    if "примечан" in folded or "ескерту" in folded:
        return "notes"
    if "расшифров" in folded or "дешифр" in folded:
        return "decode"
    if "межправ" in folded:
        return "intergov"
    if "инвест" in folded:
        return "invest"
    if "базов" in folded or "баға" in folded:
        return "base"
    if "тариф" in folded and any(token in folded for token in ("виэ", "возобновл", "жэк", "подд")):
        return "res"
    if "факт" in folded or "нақты" in folded:
        return "fact"
    return "other"


def sanitize(name: str) -> str:
    cleaned = html.unescape(name).replace("\u00a0", " ").strip()
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:180]


def choose_filename(url_name: str, title: str) -> str:
    original = sanitize(urllib.parse.unquote(url_name))
    if original and not HASH_NAME_RE.match(original):
        return original
    extension = Path(original).suffix if original else ""
    stem = sanitize(title) or "document"
    if extension and not stem.casefold().endswith(extension.casefold()):
        return f"{stem}{extension}"
    return stem or original or "document"


def quote_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def list_url(year: int, month: str, page: int | None = None) -> str:
    query = {"year": str(year), "month": month}
    if page and page > 1:
        query["PAGEN_1"] = str(page)
    return PAGE + "?" + urllib.parse.urlencode(query)


def parse_docs(page_html: str) -> list[tuple[str, str]]:
    documents: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in DOC_RE.finditer(page_html):
        href = html.unescape(match.group(1)).strip()
        title = html.unescape(match.group(2)).strip()
        if not href or href in seen:
            continue
        seen.add(href)
        documents.append((href, title))
    return documents


def page_numbers(page_html: str) -> set[int]:
    return {int(number) for number in re.findall(r"PAGEN_1=(\d+)", page_html)}


def iter_documents(year: int, month: str) -> list[tuple[str, str]]:
    first_html = fetch_text(list_url(year, month))
    time.sleep(PAUSE_SECONDS)

    pending = [1, *sorted(page_numbers(first_html))]
    visited: set[int] = set()
    documents: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    while pending:
        page = pending.pop(0)
        if page in visited:
            continue
        visited.add(page)
        page_html = first_html if page == 1 else fetch_text(list_url(year, month, page))
        if page != 1:
            time.sleep(PAUSE_SECONDS)
        for number in page_numbers(page_html):
            if number not in visited:
                pending.append(number)
        for href, title in parse_docs(page_html):
            url = quote_url(urllib.parse.urljoin(BASE, href))
            if url in seen_urls:
                continue
            seen_urls.add(url)
            documents.append((url, title))
    return documents


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            url TEXT PRIMARY KEY,
            year INTEGER NOT NULL,
            month TEXT NOT NULL,
            kind TEXT NOT NULL,
            folder TEXT NOT NULL,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            downloaded_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            mode TEXT NOT NULL,
            saved INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.commit()
    return connection


def migrate_manifest(connection: sqlite3.Connection, output_root: Path) -> int:
    manifest_path = output_root / ".manifest.json"
    if not manifest_path.exists():
        return 0

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    migrated = 0
    now = datetime.now().isoformat(timespec="seconds")
    for url, relative in data.items():
        relative_path = str(relative)
        file_path = output_root / relative_path
        if not file_path.exists() or file_path.stat().st_size <= 0:
            continue
        parts = Path(relative_path).parts
        year = int(parts[0]) if len(parts) >= 1 and str(parts[0]).isdigit() else 0
        month = parts[1] if len(parts) >= 2 else ""
        folder = parts[2] if len(parts) >= 3 else FOLDERS["other"]
        kind = next((key for key, name in FOLDERS.items() if name == folder), "other")
        connection.execute(
            """
            INSERT INTO files (
                url, year, month, kind, folder, title, filename,
                relative_path, size_bytes, downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                relative_path = excluded.relative_path,
                size_bytes = excluded.size_bytes,
                folder = excluded.folder,
                kind = excluded.kind
            """,
            (
                str(url),
                year,
                month,
                kind,
                folder,
                file_path.stem,
                file_path.name,
                relative_path,
                file_path.stat().st_size,
                now,
            ),
        )
        migrated += 1
    connection.commit()
    backup = manifest_path.with_suffix(".json.bak")
    manifest_path.replace(backup)
    return migrated


def get_existing(connection: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM files WHERE url = ?", (url,)).fetchone()


def upsert_file(
    connection: sqlite3.Connection,
    *,
    url: str,
    year: int,
    month: str,
    kind: str,
    title: str,
    dest: Path,
    output_root: Path,
) -> str:
    relative = dest.relative_to(output_root).as_posix()
    connection.execute(
        """
        INSERT INTO files (
            url, year, month, kind, folder, title, filename,
            relative_path, size_bytes, downloaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            year = excluded.year,
            month = excluded.month,
            kind = excluded.kind,
            folder = excluded.folder,
            title = excluded.title,
            filename = excluded.filename,
            relative_path = excluded.relative_path,
            size_bytes = excluded.size_bytes,
            downloaded_at = excluded.downloaded_at
        """,
        (
            url,
            year,
            month,
            kind,
            FOLDERS[kind],
            title,
            dest.name,
            relative,
            dest.stat().st_size,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    connection.commit()
    return relative


def allocate_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / filename
    stem = candidate.stem
    suffix = candidate.suffix
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{number}{suffix}"
        number += 1
    return candidate


def move_into(source: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / source.name
    if dest.resolve() == source.resolve():
        return source
    if dest.exists():
        dest = allocate_path(folder, source.name)
    source.rename(dest)
    old_folder = source.parent
    if old_folder.exists() and not any(old_folder.iterdir()):
        old_folder.rmdir()
    return dest


def download_file(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                raise RuntimeError(f"вместо файла пришла HTML-страница: {url}")
            payload = response.read()
        if not payload:
            raise RuntimeError(f"пустой ответ: {url}")
        partial.write_bytes(payload)
        partial.replace(dest)
    finally:
        if partial.exists():
            partial.unlink()


def download_month(
    year: int,
    month: str,
    output_root: Path,
    connection: sqlite3.Connection,
) -> tuple[int, int, int]:
    documents = iter_documents(year, month)
    if not documents:
        print(f"{year}/{month}: файлов нет")
        return 0, 0, 0

    saved = 0
    skipped = 0
    errors = 0
    for url, title in documents:
        url_name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        kind = classify(f"{title} {urllib.parse.unquote(url_name)}")
        folder = output_root / str(year) / month / FOLDERS[kind]
        filename = choose_filename(url_name, title)

        existing = get_existing(connection, url)
        if existing:
            existing_path = output_root / existing["relative_path"]
            if existing_path.exists() and existing_path.stat().st_size > 0:
                if existing_path.parent.resolve() != folder.resolve():
                    moved = move_into(existing_path, folder)
                    relative = upsert_file(
                        connection,
                        url=url,
                        year=year,
                        month=month,
                        kind=kind,
                        title=title,
                        dest=moved,
                        output_root=output_root,
                    )
                    print(f"перенес  {relative}")
                else:
                    print(f"уже есть  {existing['relative_path']}")
                skipped += 1
                continue

        dest = allocate_path(folder, filename)
        try:
            download_file(url, dest)
        except (urllib.error.URLError, InvalidURL, TimeoutError, RuntimeError, OSError) as error:
            errors += 1
            print(f"ошибка   {year}/{month} {filename}: {error}")
            continue

        relative = upsert_file(
            connection,
            url=url,
            year=year,
            month=month,
            kind=kind,
            title=title,
            dest=dest,
            output_root=output_root,
        )
        saved += 1
        print(f"скачан   {relative}")
        time.sleep(PAUSE_SECONDS)

    return saved, skipped, errors


def daily_years_months() -> tuple[list[int], list[str]]:
    """Текущий и прошлый год — новые файлы часто появляются с задержкой."""
    year = current_year()
    years = [year]
    if year - 1 >= START_YEAR:
        years.insert(0, year - 1)
    return years, MONTHS


def parse_args() -> argparse.Namespace:
    years = available_years()
    parser = argparse.ArgumentParser(
        description="Скачивает цены и тарифы РФЦ в папки год/месяц/тип.",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Проверить текущий и прошлый год на новые файлы (для автозапуска)",
    )
    parser.add_argument("--year", type=int, choices=years, help="Один год, например 2026")
    parser.add_argument("--month", choices=MONTHS, help="Один месяц, например 05")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Папка для файлов (по умолчанию ./downloads)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite-база учёта скачанных файлов (по умолчанию ./data/rfc_files.db)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.daily:
        years, months = daily_years_months()
        mode = "daily"
    else:
        years = [args.year] if args.year else available_years()
        months = [args.month] if args.month else MONTHS
        mode = "manual"

    output_root = args.output.resolve()
    db_path = args.db.resolve()
    connection = connect_db(db_path)
    migrated = migrate_manifest(connection, output_root)
    if migrated:
        print(f"в базу перенесено записей из старого манифеста: {migrated}")

    started_at = datetime.now().isoformat(timespec="seconds")
    cursor = connection.execute(
        "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
        (started_at, mode),
    )
    run_id = cursor.lastrowid
    connection.commit()

    saved_total = 0
    skipped_total = 0
    errors_total = 0
    for year in years:
        for month in months:
            print(f"--- {year}/{month} ---")
            saved, skipped, errors = download_month(year, month, output_root, connection)
            saved_total += saved
            skipped_total += skipped
            errors_total += errors

    finished_at = datetime.now().isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE runs
        SET finished_at = ?, saved = ?, skipped = ?, errors = ?
        WHERE id = ?
        """,
        (finished_at, saved_total, skipped_total, errors_total, run_id),
    )
    connection.commit()
    connection.close()
    print(
        f"готово: скачано {saved_total}, пропущено {skipped_total}, "
        f"ошибок {errors_total}; база {db_path}"
    )


if __name__ == "__main__":
    main()
