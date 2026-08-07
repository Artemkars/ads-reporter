"""
sources/base.py

Абстрактный базовый класс для источников рекламных данных.
Позволяет добавлять Google Ads / TikTok Ads как отдельные модули
без изменения логики расчётов и формирования отчётов.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class CampaignRow:
    """
    Единый формат строки данных по кампании.
    Используется независимо от источника (Meta / Google / TikTok).
    """
    campaign_name: str
    status: str                     # "ACTIVE" / "PAUSED" / "DELETED" и т.д.
    spend_usd: float                # расход в USD
    leads: int                      # количество лидов (уже отфильтровано по конфигу)
    date_from: date
    date_to: date
    client_id: str                  # ключ клиента из config.yaml (например, "amk")
    source: str = "meta"            # метка источника для логов и будущих расширений
    raw_actions: dict = field(default_factory=dict)  # все action_type → value (для отладки)


class DataSource(ABC):
    """
    Абстрактный источник рекламных данных.

    Для добавления нового источника (Google Ads, TikTok Ads) достаточно:
    1. Создать новый файл sources/google.py (или tiktok.py)
    2. Унаследоваться от DataSource
    3. Реализовать метод fetch()
    4. Зарегистрировать источник в run_report.py

    Логика calculator.py и excel_writer.py остаётся без изменений.
    """

    @abstractmethod
    def fetch(
        self,
        client_id: str,
        act_id: str,
        lead_action_types: list[str],
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[CampaignRow]:
        """
        Забирает данные по кампаниям для одного рекламного кабинета.

        Args:
            client_id: ключ клиента (например, "amk")
            act_id: ID рекламного кабинета (например, "act_1202497118763581")
            lead_action_types: список типов действий Meta, считаемых лидами
            date_from: начало периода (None = последние 7 дней)
            date_to: конец периода (None = последние 7 дней)

        Returns:
            Список CampaignRow. Пустой список если данных нет.

        Raises:
            Не должен бросать исключения — ошибки логируются внутри,
            возвращается пустой список.
        """
        ...
