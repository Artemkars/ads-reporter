"""
run_from_sheets.py — запуск отчёта из Google Sheets "Панель управления".

Как это работает:
  1. Пользователь заполняет строку на листе "Панель" в Google Sheets:
     Кабинет | Дата от | Дата до | Курс USD/₸ | НДС %
     amk     | 2026-08-01 | 2026-08-07 | 490 | 12

  2. Запускает этот скрипт:
     python run_from_sheets.py

  3. Скрипт:
     - Читает параметры из "Панель"
     - Тянет данные из Meta API
     - Записывает красивый блок отчёта на лист клиента
     - Обновляет статус в "Панель"

  4. Всё. Данные в Google Sheets.

Переменные окружения (.env):
  META_ACCESS_TOKEN       — токен Meta API
  GOOGLE_CREDENTIALS_JSON — JSON-ключ сервисного аккаунта (строкой)
  GOOGLE_SPREADSHEET_ID   — ID таблицы Google Sheets
  AMK_ACT_ID              — ID рекламного кабинета
"""
import io
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# UTF-8 для Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from report.calculator import calculate_report
from report.sheets_writer import (
    build_service,
    ensure_panel_exists,
    read_panel,
    update_panel_status,
    write_report_block,
)
from sources.meta import MetaAdsSource


def setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"sheets_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.info("Логирование -> %s", log_file)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)
    load_dotenv()

    # --- Проверяем переменные окружения ---
    access_token = os.getenv("META_ACCESS_TOKEN")
    google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")

    missing = []
    if not access_token:
        missing.append("META_ACCESS_TOKEN")
    if not google_creds:
        missing.append("GOOGLE_CREDENTIALS_JSON")
    if not spreadsheet_id:
        missing.append("GOOGLE_SPREADSHEET_ID")
    if missing:
        log.error("Не заданы переменные окружения: %s", ", ".join(missing))
        log.error("Проверьте файл .env")
        sys.exit(1)

    # --- Загружаем конфиг ---
    try:
        config = load_config()
    except FileNotFoundError:
        log.error("config.yaml не найден")
        sys.exit(1)

    clients_config = config.get("clients", {})

    # --- Подключаемся к Sheets ---
    log.info("Подключаемся к Google Sheets...")
    service = build_service(google_creds)

    # --- Создаём Панель если её нет ---
    ensure_panel_exists(service, spreadsheet_id)

    # --- Читаем запрос из Панели ---
    log.info("Читаем параметры из листа 'Панель'...")
    params = read_panel(service, spreadsheet_id)

    if params is None:
        log.error(
            "Лист 'Панель' пустой! Заполните строку 3: кабинет, даты, курс, НДС."
        )
        log.info("Откройте таблицу: https://docs.google.com/spreadsheets/d/%s/edit", spreadsheet_id)
        sys.exit(1)

    client_key = params["client_key"]
    date_from_str = params["date_from"]
    date_to_str = params["date_to"]
    rate = params["rate"]
    vat = params["vat"]

    log.info(
        "Запрос: кабинет=%s | период=%s -> %s | курс=%.0f | НДС=%.0f%%",
        client_key,
        date_from_str or "last_7d",
        date_to_str or "last_7d",
        rate or 0,
        vat or 0,
    )

    # --- Валидация ---
    if client_key not in clients_config:
        msg = f"Кабинет '{client_key}' не найден в config.yaml. Доступные: {', '.join(clients_config.keys())}"
        log.error(msg)
        update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
        sys.exit(1)

    if rate is None or vat is None:
        msg = "Не заполнены курс или НДС в Панели"
        log.error(msg)
        update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
        sys.exit(1)

    # Парсим даты
    date_from = None
    date_to = None
    if date_from_str:
        try:
            date_from = date.fromisoformat(date_from_str)
        except ValueError:
            msg = f"Неверный формат 'Дата от': {date_from_str} (нужно ГГГГ-ММ-ДД)"
            log.error(msg)
            update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
            sys.exit(1)
    if date_to_str:
        try:
            date_to = date.fromisoformat(date_to_str)
        except ValueError:
            msg = f"Неверный формат 'Дата до': {date_to_str} (нужно ГГГГ-ММ-ДД)"
            log.error(msg)
            update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
            sys.exit(1)

    # --- Обновляем статус ---
    update_panel_status(service, spreadsheet_id, "Загрузка данных...")

    # --- Инициализируем Meta API ---
    client_cfg = clients_config[client_key]
    client_name = client_cfg.get("name", client_key.upper())
    act_id_env = client_cfg.get("act_id_env", "")
    act_id = os.getenv(act_id_env)
    lead_action_types = client_cfg.get("lead_action_types", ["lead"])

    if not act_id:
        msg = f"Переменная {act_id_env} не задана в .env"
        log.error(msg)
        update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
        sys.exit(1)

    meta_cfg = config.get("meta_api", {})
    source = MetaAdsSource(
        access_token=access_token,
        api_version=meta_cfg.get("version", "v19.0"),
        timeout=meta_cfg.get("timeout_seconds", 30),
        max_retries=meta_cfg.get("max_retries", 3),
        retry_delay=meta_cfg.get("retry_delay_seconds", 5),
    )

    # --- Тянем данные ---
    try:
        campaigns = source.fetch(
            client_id=client_key,
            act_id=act_id,
            lead_action_types=lead_action_types,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        msg = f"Ошибка Meta API: {exc}"
        log.error(msg)
        update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
        sys.exit(1)

    if not campaigns:
        msg = "Нет данных за указанный период"
        log.warning(msg)
        update_panel_status(service, spreadsheet_id, msg)
        sys.exit(0)

    # --- Считаем ---
    update_panel_status(service, spreadsheet_id, "Расчёт и запись...")

    client_report = calculate_report(
        client_id=client_key,
        client_name=client_name,
        campaigns=campaigns,
        rate_usd_kzt=rate,
        vat_pct=vat,
    )

    if client_report is None:
        msg = "Отчёт пустой"
        log.warning(msg)
        update_panel_status(service, spreadsheet_id, msg)
        sys.exit(0)

    # --- Пишем в Sheets ---
    try:
        url = write_report_block(service, spreadsheet_id, client_report)
    except Exception as exc:
        msg = f"Ошибка записи в Sheets: {exc}"
        log.error(msg)
        update_panel_status(service, spreadsheet_id, f"Ошибка: {msg}")
        sys.exit(1)

    # --- Готово ---
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    period_label = client_report.date_label
    status = f"Готово | {period_label} | {now}"
    update_panel_status(service, spreadsheet_id, status)

    log.info("")
    log.info("=== ГОТОВО ===")
    log.info("Клиент: %s", client_name)
    log.info("Период: %s", period_label)
    log.info("Кампаний: %d | Лидов: %d | Расход: %.0f KZT",
             len(client_report.rows), client_report.total.leads, client_report.total.spend_kzt)
    log.info("Таблица: %s", url)


if __name__ == "__main__":
    main()
