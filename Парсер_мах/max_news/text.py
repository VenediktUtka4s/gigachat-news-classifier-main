from __future__ import annotations

import re


_SENTENCE_END = re.compile(r"[.!?…]+(?:[\"'»”’\)\]]+)?(?=\s|$)")


def first_sentence(text: str) -> str:
    """Возвращает первое предложение; при отсутствии знака — первую строку."""
    cleaned = text.strip()
    if not cleaned:
        return ""

    match = _SENTENCE_END.search(cleaned)
    first_line_end = cleaned.find("\n")
    if match and (first_line_end == -1 or match.end() <= first_line_end):
        return cleaned[: match.end()].strip()
    if first_line_end != -1:
        return cleaned[:first_line_end].strip()
    return cleaned

