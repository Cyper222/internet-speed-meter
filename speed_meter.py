#!/usr/bin/env python3
"""Measure download speed using ten sequential HTTP requests."""

import argparse
from dataclasses import dataclass
from http.client import HTTPException
import math
import sys
from time import perf_counter
from typing import Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
BYTES_PER_MB = 1_000_000


@dataclass(frozen=True)
class Measurement:
    size_bytes: int
    elapsed_seconds: float


def http_url(value: str) -> str:
    """Accept only complete HTTP(S) URLs."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        # Accessing port also validates its format and range.
        parsed.port
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        value.encode("ascii")
    except (ValueError, UnicodeError):
        raise argparse.ArgumentTypeError(
            "укажите полный HTTP/HTTPS URL; не-ASCII символы нужно закодировать"
        ) from None
    return value


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        timeout = float("nan")
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("таймаут должен быть положительным числом")
    return timeout


def measure_once(url: str, timeout: float) -> Measurement:
    """Time a full download, counting bytes without keeping the file in memory."""
    request = Request(url, headers={
        "User-Agent": "internet-speed-meter/1.0",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "Accept-Encoding": "identity",
    })
    size_bytes = 0
    started = perf_counter()
    with urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            size_bytes += len(chunk)
        elapsed_seconds = perf_counter() - started

        # Reading in chunks may silently reach EOF on a truncated response.
        length = response.headers.get("Content-Length")
        if length is not None and not response.headers.get("Transfer-Encoding"):
            if size_bytes != int(length):
                raise OSError(
                    f"неполный ответ: ожидалось {length} байт, получено {size_bytes}"
                )

    if size_bytes == 0:
        raise OSError("сервер вернул пустой ответ; выберите URL файла")
    if elapsed_seconds <= 0:
        raise OSError("не удалось измерить длительность запроса")
    return Measurement(size_bytes, elapsed_seconds)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Измерить скорость скачивания: 10 последовательных запросов."
    )
    parser.add_argument("url", type=http_url, help="прямая ссылка на большой файл")
    parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0, metavar="SECONDS",
        help="таймаут сетевой операции в секундах (по умолчанию: 30)",
    )
    args = parser.parse_args(argv)

    total_bytes = 0
    total_seconds = 0.0
    for number in range(1, REQUEST_COUNT + 1):
        try:
            result = measure_once(args.url, args.timeout)
        except KeyboardInterrupt:
            print("\nИзмерение прервано.", file=sys.stderr)
            return 130
        except (HTTPError, URLError, OSError, HTTPException, ValueError) as error:
            print(f"Ошибка запроса {number}/{REQUEST_COUNT}: {error}", file=sys.stderr)
            return 1
        total_bytes += result.size_bytes
        total_seconds += result.elapsed_seconds
        print(
            f"{number:2}/{REQUEST_COUNT}: {result.size_bytes} байт, "
            f"{result.elapsed_seconds:.3f} с", flush=True,
        )

    speed = total_bytes / total_seconds / BYTES_PER_MB
    print(f"\nЗавершено запросов: {REQUEST_COUNT}")
    print(f"Среднее время запроса: {total_seconds / REQUEST_COUNT:.3f} с")
    print(f"Скачано: {total_bytes} байт ({total_bytes / BYTES_PER_MB:.3f} МБ)")
    print(f"Скорость: {speed:.3f} МБ/с ({speed * 8:.3f} Мбит/с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
