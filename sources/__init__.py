"""
sources/__init__.py
Экспортирует базовый класс и конкретные реализации источников данных.
"""
from .base import DataSource, CampaignRow
from .meta import MetaAdsSource

__all__ = ["DataSource", "CampaignRow", "MetaAdsSource"]
