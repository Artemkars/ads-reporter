"""
report/sheets_writer.py

Запись отчёта в Google Sheets через Google Sheets API v4.

Два режима работы:
1. Инициализация "Панели управления" — лист-запросчик, где пользователь
   указывает кабинет, даты, курс, НДС
2. Запись отчёта — красивый блок с шапкой периода, таблицей кампаний и итогом

Авторизация — сервисный аккаунт (JSON-ключ из переменной окружения GOOGLE_CREDENTIALS_JSON).
"""
import json
import logging
from datetime import datetime
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .calculator import ClientReport

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PANEL_SHEET_NAME = "Панель"

# Заголовки панели управления
PANEL_HEADERS = [
    "Кабинет",
    "Дата от (ГГГГ-ММ-ДД)",
    "Дата до (ГГГГ-ММ-ДД)",
    "Курс USD/₸",
    "НДС + АК %",
    "Статус",
]

# Заголовки таблицы отчёта
REPORT_HEADERS = [
    "Название (Кампания / Группа / Объявление)",
    "Расход USD",
    "Расход ₸",
    "Лиды",
    "Цена лида",
    "Статус",
]


# ===========================================================================
# Публичные функции
# ===========================================================================

def build_service(credentials_json: str):
    """Создаёт авторизованный клиент Sheets API."""
    info = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_panel_exists(service, spreadsheet_id: str) -> None:
    """
    Создаёт лист 'Панель' с инструкцией если его нет.
    Если он уже есть — ничего не делает.
    """
    if _sheet_exists(service, spreadsheet_id, PANEL_SHEET_NAME):
        return

    logger.info("Создаём лист '%s'...", PANEL_SHEET_NAME)

    # Создаём лист
    _create_sheet(service, spreadsheet_id, PANEL_SHEET_NAME, frozen_rows=2)

    # Пишем инструкцию и заголовки
    rows = [
        [
            "Заполните строку 3 и запустите скрипт: python run_from_sheets.py",
            "", "", "", "", ""
        ],
        PANEL_HEADERS,
        ["amk", "", "", "490", "12", ""],  # пример-заготовка
    ]

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{PANEL_SHEET_NAME}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    # Форматирование
    sheet_id = _get_sheet_id(service, spreadsheet_id, PANEL_SHEET_NAME)
    requests = [
        # Строка 1 — инструкция (серый фон, italic)
        _format_row_request(sheet_id, 0, 1, bold=False, italic=True,
                            bg=(0.95, 0.95, 0.95), fg=(0.4, 0.4, 0.4)),
        # Строка 2 — заголовки (тёмно-синий)
        _format_row_request(sheet_id, 1, 2, bold=True,
                            bg=(0.12, 0.22, 0.39), fg=(1, 1, 1)),
        # Ширина столбцов
        _col_width_request(sheet_id, 0, 200),   # Кабинет
        _col_width_request(sheet_id, 1, 180),   # Дата от
        _col_width_request(sheet_id, 2, 180),   # Дата до
        _col_width_request(sheet_id, 3, 120),   # Курс
        _col_width_request(sheet_id, 4, 120),   # НДС
        _col_width_request(sheet_id, 5, 250),   # Статус
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()

    logger.info("Панель управления создана")


def read_panel(service, spreadsheet_id: str) -> Optional[dict]:
    """
    Читает строку 3 из листа 'Панель' (первый запрос пользователя).

    Returns:
        dict с ключами: client_key, date_from, date_to, rate, vat
        или None если данных нет.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{PANEL_SHEET_NAME}'!A3:E3",
    ).execute()

    values = result.get("values", [])
    if not values or not values[0]:
        return None

    row = values[0]
    # Дополняем до 5 элементов
    while len(row) < 5:
        row.append("")

    client_key = (row[0] or "").strip().lower()
    date_from = (row[1] or "").strip()
    date_to = (row[2] or "").strip()
    rate_str = (row[3] or "").strip().replace(",", ".").replace(" ", "")
    vat_str = (row[4] or "").strip().replace(",", ".").replace(" ", "")

    if not client_key:
        return None

    return {
        "client_key": client_key,
        "date_from": date_from if date_from else None,
        "date_to": date_to if date_to else None,
        "rate": float(rate_str) if rate_str else None,
        "vat": float(vat_str) if vat_str else None,
    }


def update_panel_status(service, spreadsheet_id: str, status: str) -> None:
    """Обновляет колонку 'Статус' в строке 3 панели."""
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{PANEL_SHEET_NAME}'!F3",
        valueInputOption="USER_ENTERED",
        body={"values": [[status]]},
    ).execute()


def write_report_block(
    service,
    spreadsheet_id: str,
    client_report: ClientReport,
) -> str:
    """
    Записывает отчёт красивым блоком на лист клиента.

    Каждый запуск добавляет новый блок:
    ┌──────────────────────────────────────────────────┐
    │ Период: 01.07 – 07.07.2026 | Курс: 490 | НДС 12% │
    ├──────────────────────────────────────────────────┤
    │ Кампания | Статус | Расход $ | Расход ₸ | ...    │
    │ ...данные...                                      │
    │ ИТОГО    |        | 3227.34  | 1771164  | ...    │
    └──────────────────────────────────────────────────┘
    (пустая строка)

    Returns:
        URL таблицы.
    """
    sheet_name = f"{client_report.client_name} {client_report.date_label}"

    # Создаём лист если не существует
    if not _sheet_exists(service, spreadsheet_id, sheet_name):
        _create_sheet(service, spreadsheet_id, sheet_name, frozen_rows=0)
        logger.info("Создан лист '%s'", sheet_name)

    # Определяем следующую свободную строку
    next_row = _get_next_empty_row(service, spreadsheet_id, sheet_name)

    # Формируем блок строк
    rows = _build_report_block(client_report)

    # Записываем блок
    start_cell = f"'{sheet_name}'!A{next_row}"
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=start_cell,
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    # Форматирование блока
    sheet_id = _get_sheet_id(service, spreadsheet_id, sheet_name)
    data_rows_count = max(1, len(client_report.rows))
    _format_report_block(service, spreadsheet_id, sheet_id,
                         next_row, data_rows_count)

    # Автоширина колонок
    _auto_col_widths(service, spreadsheet_id, sheet_id)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    logger.info(
        "[%s] Отчёт записан в Sheets: строки %d–%d",
        client_report.client_name, next_row, next_row + len(rows) - 1,
    )
    return url


# ===========================================================================
# Формирование блока данных
# ===========================================================================

def _build_report_block(client_report: ClientReport) -> list[list]:
    """Формирует список строк для одного блока отчёта."""
    rows = []

    # Строка 1: шапка периода
    rows.append([
        f"Период: {client_report.date_label}",
        "",
        f"Курс: {client_report.rate_usd_kzt:,.0f} ₸",
        f"НДС + АК: {client_report.vat_pct}%",
        "",
        f"Источник: Meta Ads",
    ])

    # Строка 2: заголовки таблицы
    rows.append(REPORT_HEADERS)

    # Строки данных
    if not client_report.rows:
        rows.append(["Нет данных за указанный период", "", "", "", "", ""])
    else:
        for row in client_report.rows:
            prefix = ""
            if row.level == "adset":
                prefix = "  └─ "
            elif row.level == "ad":
                prefix = "      └─ "
                
            rows.append([
                prefix + row.name,
                row.spend_usd,
                row.spend_kzt,
                row.leads,
                row.cost_per_lead if row.cost_per_lead else 0,
                row.status,
            ])

    # Итоговая строка
    t = client_report.total
    rows.append([
        "ИТОГО",
        t.spend_usd,
        t.spend_kzt,
        t.leads,
        t.cost_per_lead if t.cost_per_lead else 0,
        "",
    ])

    # Пустая строка-разделитель
    rows.append([""])

    return rows


# ===========================================================================
# Форматирование
# ===========================================================================

def _format_report_block(
    service, spreadsheet_id: str, sheet_id: int,
    start_row: int, data_rows_count: int
) -> None:
    """Применяет форматирование к блоку отчёта."""
    # start_row — 1-indexed (номер строки в Sheets)
    # В API batchUpdate используется 0-indexed
    r = start_row - 1  # 0-indexed

    header_row = r          # шапка периода
    cols_row = r + 1        # заголовки колонок
    data_start = r + 2      # первая строка данных
    total_row = data_start + data_rows_count  # ИТОГО

    requests = [
        # Шапка периода — голубой фон, жирный, крупный шрифт
        _format_row_request(sheet_id, header_row, header_row + 1,
                            bold=True, bg=(0.82, 0.88, 0.95), fg=(0.12, 0.22, 0.39), font_size=14),
        # Заголовки колонок — тёмно-синий
        _format_row_request(sheet_id, cols_row, cols_row + 1,
                            bold=True, bg=(0.12, 0.22, 0.39), fg=(1, 1, 1)),
        # Итоговая строка — жирный, светло-голубой фон
        _format_row_request(sheet_id, total_row, total_row + 1,
                            bold=True, bg=(0.84, 0.89, 0.94), fg=(0.12, 0.22, 0.39)),
        # Рамки вокруг всего блока (данные + итого)
        {
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": cols_row,
                    "endRowIndex": total_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "top": _border_style(),
                "bottom": _border_style(),
                "left": _border_style(),
                "right": _border_style(),
                "innerHorizontal": _border_style(style="DOTTED"),
                "innerVertical": _border_style(style="DOTTED"),
            }
        },
        # Числовой формат для столбцов B-C (расходы) и E (цена лида)
        _number_format_request(sheet_id, data_start, total_row + 1, 1, 2, "#,##0.00"),
        _number_format_request(sheet_id, data_start, total_row + 1, 2, 3, "#,##0 ₸"),
        _number_format_request(sheet_id, data_start, total_row + 1, 4, 5, "#,##0 ₸"),
    ]

    # Добавляем условное форматирование статусов
    requests.extend(_conditional_format_status(sheet_id, data_start, total_row))

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception as fmt_err:
        logger.warning("Ошибка форматирования: %s", fmt_err)


def _auto_col_widths(service, spreadsheet_id: str, sheet_id: int) -> None:
    """Задаёт фиксированную ширину столбцов отчёта."""
    requests = [
        _col_width_request(sheet_id, 0, 420),   # Кампания
        _col_width_request(sheet_id, 1, 110),   # Расход $
        _col_width_request(sheet_id, 2, 150),   # Расход ₸
        _col_width_request(sheet_id, 3, 80),    # Лиды
        _col_width_request(sheet_id, 4, 130),   # Цена лида
        _col_width_request(sheet_id, 5, 120),   # Статус
    ]
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception:
        pass


# ===========================================================================
# Утилиты для Sheets API
# ===========================================================================

def _sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> bool:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return sheet_name in [s["properties"]["title"] for s in meta.get("sheets", [])]


def _create_sheet(service, spreadsheet_id: str, sheet_name: str,
                  frozen_rows: int = 0) -> None:
    body = {
        "requests": [{
            "addSheet": {
                "properties": {
                    "title": sheet_name,
                    "gridProperties": {"frozenRowCount": frozen_rows},
                }
            }
        }]
    }
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()


def _get_sheet_id(service, spreadsheet_id: str, sheet_name: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Лист '{sheet_name}' не найден")


def _get_next_empty_row(service, spreadsheet_id: str, sheet_name: str) -> int:
    """Находит первую пустую строку на листе."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:A",
    ).execute()
    values = result.get("values", [])
    return len(values) + 1


def _format_row_request(
    sheet_id: int, start_row: int, end_row: int,
    bold: bool = False, italic: bool = False,
    bg: tuple = None, fg: tuple = None, font_size: int = 10,
) -> dict:
    text_format = {"bold": bold, "italic": italic, "fontSize": font_size}
    if fg:
        text_format["foregroundColorStyle"] = {
            "rgbColor": {"red": fg[0], "green": fg[1], "blue": fg[2]}
        }

    fmt = {"textFormat": text_format}
    if bg:
        fmt["backgroundColor"] = {"red": bg[0], "green": bg[1], "blue": bg[2]}

    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
            },
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    }


def _col_width_request(sheet_id: int, col_index: int, width: int) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_index,
                "endIndex": col_index + 1,
            },
            "properties": {"pixelSize": width},
            "fields": "pixelSize",
        }
    }


def _number_format_request(
    sheet_id: int, start_row: int, end_row: int,
    start_col: int, end_col: int, pattern: str,
) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {
                "userEnteredFormat": {
                    "numberFormat": {"type": "NUMBER", "pattern": pattern}
                }
            },
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _border_style(style: str = "SOLID") -> dict:
    return {
        "style": style,
        "color": {"red": 0.74, "green": 0.76, "blue": 0.78},
    }

def _conditional_format_status(sheet_id: int, start_row: int, end_row: int) -> list[dict]:
    def rule(condition_type: str, condition_values: list[str], bg: tuple, fg: tuple):
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": end_row,
                        "startColumnIndex": 5,
                        "endColumnIndex": 6,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": condition_type,
                            "values": [{"userEnteredValue": v} for v in condition_values]
                        },
                        "format": {
                            "backgroundColor": {"red": bg[0], "green": bg[1], "blue": bg[2]},
                            "textFormat": {"foregroundColor": {"red": fg[0], "green": fg[1], "blue": fg[2]}, "bold": True}
                        }
                    }
                },
                "index": 0
            }
        }
    return [
        rule("TEXT_EQ", ["Активна"], (0.85, 0.93, 0.83), (0.1, 0.5, 0.1)),
        rule("TEXT_EQ", ["Остановлена"], (0.9, 0.9, 0.9), (0.4, 0.4, 0.4)),
        rule("TEXT_EQ", ["Отключена"], (0.9, 0.9, 0.9), (0.4, 0.4, 0.4)),
    ]
