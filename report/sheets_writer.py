"""
report/sheets_writer.py

Запись отчёта в Google Sheets через Google Sheets API v4.
Авторизация — сервисный аккаунт (JSON-ключ из переменной окружения GOOGLE_CREDENTIALS_JSON).

Структура листа в Sheets:
  - Один лист на клиента (создаётся автоматически если не существует)
  - Строка-заголовок (пишется один раз при создании листа)
  - Каждый запуск добавляет строки в конец (append), не перезаписывает
  - Столбцы: Дата запуска | Период | Курс | НДС | Кампания | Статус |
             Расход $ | Расход ₸ | Лиды | Цена лида ₸ | ИТОГО?

Таким образом в одной Sheets-таблице накапливается история всех недель.
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .calculator import ClientReport

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Заголовки таблицы
HEADER = [
    "Дата запуска",
    "Период",
    "Курс USD/₸",
    "НДС %",
    "Кампания",
    "Статус",
    "Расход, $",
    "Расход, ₸ (с НДС)",
    "Лиды",
    "Цена лида, ₸",
    "Итоговая строка",
]


def write_sheets_report(
    client_report: ClientReport,
    spreadsheet_id: str,
    credentials_json: str,
) -> str:
    """
    Записывает отчёт клиента в Google Sheets (append в конец листа).

    Args:
        client_report: рассчитанный отчёт
        spreadsheet_id: ID таблицы Google Sheets
            (часть URL: docs.google.com/spreadsheets/d/ЭТОТ_ID/edit)
        credentials_json: JSON-строка с ключом сервисного аккаунта
            (значение переменной окружения GOOGLE_CREDENTIALS_JSON)

    Returns:
        URL таблицы.
    """
    try:
        service = _build_service(credentials_json)
        sheet_name = client_report.client_name  # "AMK", "JetQ" и т.д.

        # Убедимся что лист существует, создадим если нет
        _ensure_sheet_exists(service, spreadsheet_id, sheet_name)

        # Проверяем — если лист пустой, пишем заголовок
        _ensure_header(service, spreadsheet_id, sheet_name)

        # Формируем строки данных
        rows = _build_rows(client_report)

        # Append в конец листа
        body = {"values": rows}
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute()
        )

        updated_range = result.get("updates", {}).get("updatedRange", "?")
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        logger.info(
            "[%s] Google Sheets обновлён: %s | Диапазон: %s",
            client_report.client_name,
            url,
            updated_range,
        )
        return url

    except HttpError as exc:
        logger.error(
            "[%s] Ошибка Google Sheets API: %s", client_report.client_name, exc
        )
        raise
    except Exception as exc:
        logger.error(
            "[%s] Ошибка при записи в Sheets: %s", client_report.client_name, exc
        )
        raise


# ---------------------------------------------------------------------------
# Внутренние функции
# ---------------------------------------------------------------------------

def _build_service(credentials_json: str):
    """Создаёт авторизованный клиент Sheets API."""
    info = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> None:
    """Создаёт лист с нужным именем если он не существует."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if sheet_name in existing:
        return

    logger.info("Создаём лист '%s' в таблице...", sheet_name)
    body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": sheet_name,
                        "gridProperties": {"frozenRowCount": 1},
                    }
                }
            }
        ]
    }
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()


def _ensure_header(service, spreadsheet_id: str, sheet_name: str) -> None:
    """Пишет строку заголовка если лист пустой."""
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:A1",
        )
        .execute()
    )

    existing = result.get("values", [])
    if existing:
        return  # Заголовок уже есть

    logger.info("Пишем заголовок в лист '%s'...", sheet_name)
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADER]},
    ).execute()

    # Делаем заголовок жирным
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_id = next(
            s["properties"]["sheetId"]
            for s in meta["sheets"]
            if s["properties"]["title"] == sheet_name
        )
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "backgroundColor": {
                                        "red": 0.122,
                                        "green": 0.220,
                                        "blue": 0.392,
                                    },
                                    "foregroundColor": {
                                        "red": 1.0,
                                        "green": 1.0,
                                        "blue": 1.0,
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,backgroundColor,foregroundColor)",
                        }
                    }
                ]
            },
        ).execute()
    except Exception as fmt_err:
        logger.warning("Не удалось применить форматирование заголовка: %s", fmt_err)


def _build_rows(client_report: ClientReport) -> list[list]:
    """Формирует список строк для записи в Sheets."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    period = client_report.date_label
    rate = client_report.rate_usd_kzt
    vat = client_report.vat_pct

    rows = []

    # Строки кампаний
    for row in client_report.rows:
        rows.append([
            now,
            period,
            rate,
            vat,
            row.campaign_name,
            row.status,
            row.spend_usd,
            row.spend_kzt,
            row.leads,
            row.cost_per_lead if row.cost_per_lead else 0,
            "",  # не итоговая
        ])

    # Итоговая строка
    t = client_report.total
    rows.append([
        now,
        period,
        rate,
        vat,
        "ИТОГО",
        "",
        t.spend_usd,
        t.spend_kzt,
        t.leads,
        t.cost_per_lead if t.cost_per_lead else 0,
        "ДА",  # маркер итоговой строки
    ])

    return rows
