"""Data models for the lead-generation bot."""

from models.order import Order
from models.organization import Organization
from models.tg_lead import TgLead

__all__ = ["Order", "Organization", "TgLead"]
