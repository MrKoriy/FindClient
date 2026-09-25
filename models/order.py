"""Freelance order model shared by all order sources."""

from dataclasses import dataclass


@dataclass
class Order:
    source: str
    id: str
    title: str
    url: str
    description: str = ""
    budget: str = ""
    published: str = ""

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.id}"
