"""Organization data model for 2GIS search results."""

from dataclasses import dataclass


@dataclass
class Organization:
    """Represents a business organization from 2GIS.

    All fields except id and name have defaults so partial data
    from the 2GIS API never crashes the application.
    """

    id: str
    name: str
    phone: str = ""
    email: str = ""
    website: str = ""
    address: str = ""
    rating: float = 0.0
    socials: str = ""
