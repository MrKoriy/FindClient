"""Organization data model for map search results (2GIS, Yandex Maps)."""

from dataclasses import dataclass


@dataclass
class Organization:
    """Represents a business organization from a map source.

    All fields except id and name have defaults so partial data
    from a source never crashes the application.
    """

    id: str
    name: str
    phone: str = ""
    email: str = ""
    website: str = ""
    address: str = ""
    rating: float = 0.0
    socials: str = ""
    source: str = "2gis"
    city: str = ""
    category: str = ""
    reviews: int = 0
    branches: int = 0
    url: str = ""
    score: int = 0

    @property
    def has_website(self) -> bool:
        return bool(self.website.strip())
