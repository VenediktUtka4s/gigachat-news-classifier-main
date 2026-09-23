from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Post:
    source: str
    source_url: str
    publication_date: date
    publication_time: time | None
    direct_url: str
    title: str
    text: str
