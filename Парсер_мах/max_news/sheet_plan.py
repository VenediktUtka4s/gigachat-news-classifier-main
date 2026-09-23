"""Plan additions and repairs without changing existing populated cells."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date

import emoji
from .models import Post

FIRST_DATA_ROW = 6
OWNED = (0, 1, 4, 6, 7, 9, 12)


class SheetWriteError(RuntimeError):
    pass


def text_key(text: str) -> str:
    # Preserve case, punctuation and links. Similar news need not be identical.
    # Mirror the classifier's emoji removal so cleaned M still matches MAX.
    text = emoji.replace_emoji(text, replace=lambda chars, data:
        chars[0] if chars.endswith('\u20e3') or chars[0] in '©®™' else ' ')
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf'
                   and c not in '\ufe0f\ufe0e')
    return ' '.join(unicodedata.normalize('NFC', text).split())


def post_values(post: Post, collected_on: date) -> dict[int, str | int]:
    epoch = date(1899, 12, 30)
    return {0: 'Максим', 1: (collected_on - epoch).days,
            4: (post.publication_date - epoch).days, 6: post.source,
            7: post.direct_url, 9: post.title, 12: post.text}


@dataclass
class WritePlan:
    changes: list[tuple[int, dict[int, str | int]]]
    new_count: int = 0
    text_duplicates: int = 0


def plan_posts(rows: list[list], posts: list[Post], collected_on: date) -> WritePlan:
    working = [list(row) + [''] * max(0, 21 - len(row)) for row in rows]
    by_url = {}
    for index, row in enumerate(working):
        if str(row[7]).strip():
            by_url.setdefault(str(row[7]).strip(), index)
    known_texts = {key for row in working if isinstance(row[12], str)
                   if (key := text_key(row[12]))}
    # FORMULA reads preserve even formulas displaying empty strings.
    occupied = [i for i, row in enumerate(working) if any(v != '' for v in row)]
    next_index = max(occupied, default=-1) + 1
    unique = {}
    for post in posts:
        unique.setdefault(post.direct_url, post)
    # Repair known identities before comparing new URLs with repaired texts.
    ordered = sorted(unique.values(), key=lambda post: post.direct_url not in by_url)
    plan = WritePlan([])
    for post in ordered:
        wanted = post_values(post, collected_on)
        index = by_url.get(post.direct_url)
        if index is None:
            key = text_key(post.text)
            if key and key in known_texts:
                plan.text_duplicates += 1
                continue
            index = next_index
            next_index += 1
            while len(working) <= index:
                working.append([''] * 21)
            changes = wanted
            plan.new_count += 1
            by_url[post.direct_url] = index
        else:
            changes = {col: value for col, value in wanted.items()
                       if working[index][col] == '' and value != ''}
        if changes:
            plan.changes.append((index + FIRST_DATA_ROW, changes))
            for col, value in changes.items():
                working[index][col] = value
        if isinstance(working[index][12], str):
            key = text_key(working[index][12])
            if key:
                known_texts.add(key)
    return plan
