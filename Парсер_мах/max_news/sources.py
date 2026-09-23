from __future__ import annotations

from .models import Source


def parse_max_sources(rows: list[list[str]]) -> list[Source]:
    if len(rows) < 2:
        raise RuntimeError("На листе «Источники» не найдена таблица данных")

    headers = [cell.strip() for cell in rows[0]]
    try:
        source_column = headers.index("Название")
        url_column = headers.index("Основная ссылка")
        messenger_column = headers.index("Платформа")
        status_column = headers.index("Активность")
    except ValueError as error:
        raise RuntimeError(f"Не найден обязательный столбец: {error}") from error

    result: list[Source] = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        if padded[messenger_column].strip().lower() != "max":
            continue
        if padded[status_column].strip().lower() not in {"активен", "активный", "да"}:
            continue
        name = padded[source_column].strip()
        url = padded[url_column].strip()
        if name and url.startswith("https://max.ru/"):
            result.append(Source(name=name, url=url))
    return result
