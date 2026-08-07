"""
report/calculator.py

Финансовые расчёты отчёта. Не зависит от источника данных.

Формулы:
    spend_kzt = spend_usd * rate * (1 + vat_pct / 100)
    cost_per_lead = spend_kzt / leads  (0 если лидов нет)

Итоговая строка:
    сумма расходов USD, сумма расходов KZT, сумма лидов, средняя цена лида
"""
from dataclasses import dataclass
from typing import Optional

from sources.base import CampaignRow


@dataclass
class ReportRow:
    """Строка отчёта с рассчитанными финансовыми показателями."""
    name: str
    status: str
    spend_usd: float
    spend_kzt: float         # spend_usd * rate * (1 + vat_pct/100)
    results: int
    cost_per_result: float   # spend_kzt / results (0 если results == 0)
    level: str = "campaign"  # 'campaign', 'adset', 'ad'
    is_total: bool = False   # True для итоговой строки блока


@dataclass
class ClientReport:
    """Полный отчёт по одному клиенту."""
    client_id: str
    client_name: str
    rows: list[ReportRow]         # все кампании
    total: ReportRow              # итоговая строка
    rate_usd_kzt: float
    vat_pct: float
    date_label: str               # "01.07.2026 – 07.07.2026" — для заголовка отчёта
    source_name: str = "Meta Ads" # "Meta Ads" or "Google Ads"


def calculate_report(
    client_id: str,
    client_name: str,
    campaigns: list[CampaignRow],
    rate_usd_kzt: float,
    vat_pct: float,
    date_from: str = None,
    date_to: str = None,
    source_name: str = "Meta Ads",
) -> ClientReport:
    """
    Рассчитывает финансовые показатели по всем кампаниям клиента.

    Args:
        client_id: ключ клиента ("amk")
        client_name: отображаемое имя ("AMK")
        campaigns: список CampaignRow из DataSource
        rate_usd_kzt: курс USD/₸ на неделю (задаётся пользователем)
        vat_pct: процент НДС + АК (например, 12.0)

        date_from, date_to: Строки дат для заголовка.

    Returns:
        ClientReport с данными (или пустым списком строк, если нет кампаний).
    """
    multiplier = rate_usd_kzt * (1 + vat_pct / 100)

    rows: list[ReportRow] = []
    for camp in campaigns:
        spend_kzt = round(camp.spend_usd * multiplier, 2)
        cost_per_result = round(spend_kzt / camp.results, 2) if camp.results > 0 else 0.0

        rows.append(ReportRow(
            name=camp.name,
            status=_format_status(camp.status),
            spend_usd=round(camp.spend_usd, 2),
            spend_kzt=spend_kzt,
            results=camp.results,
            cost_per_result=cost_per_result,
            level=camp.level,
        ))

    # Итоговая строка (считаем только по уровню campaign, чтобы не дублировать)
    campaign_rows = [r for r in rows if r.level == 'campaign']
    total_spend_usd = round(sum(r.spend_usd for r in campaign_rows), 2)
    total_spend_kzt = round(sum(r.spend_kzt for r in campaign_rows), 2)
    total_results = sum(r.results for r in campaign_rows)
    avg_cost_per_result = (
        round(total_spend_kzt / total_results, 2) if total_results > 0 else 0.0
    )

    total = ReportRow(
        name="ИТОГО",
        status="",
        spend_usd=total_spend_usd,
        spend_kzt=total_spend_kzt,
        results=total_results,
        cost_per_result=avg_cost_per_result,
        level="total",
        is_total=True,
    )

    # Формируем метку периода
    date_label = f"{date_from} – {date_to}" if date_from and date_to else "За всё время"

    return ClientReport(
        client_id=client_id,
        client_name=client_name,
        rows=rows,
        total=total,
        rate_usd_kzt=rate_usd_kzt,
        vat_pct=vat_pct,
        date_label=date_label,
        source_name=source_name,
    )


def _format_status(status: str) -> str:
    """Переводит статус Meta на понятный язык."""
    mapping = {
        "ACTIVE": "Активна",
        "PAUSED": "Остановлена",
        "DELETED": "Удалена",
        "ARCHIVED": "В архиве",
        "DISABLED": "Отключена",
        "CAMPAIGN_PAUSED": "Остановлена (Кампания)",
        "ADSET_PAUSED": "Остановлена (Группа)",
        "PENDING_REVIEW": "На модерации",
        "DISAPPROVED": "Отклонена",
        "WITH_ISSUES": "С ошибками",
        "IN_PROCESS": "В обработке",
        "PREAPPROVED": "Одобрена",
    }
    return mapping.get(status.upper(), status)
