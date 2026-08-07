"""
run_report.py -- точка входа для еженедельного рекламного отчета.

Использование:
    python run_report.py --rate 490 --vat 12
    python run_report.py --rate 490 --vat 12 --date-from 2026-07-28 --date-to 2026-08-03
    python run_report.py --rate 490 --vat 12 --sheets-id 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
    python run_report.py --rate 490 --vat 12 --no-excel
    python run_report.py --rate 490 --vat 12 --client amk
    python run_report.py --rate 490 --vat 12 --list-clients

Аргументы:
    --rate        Курс USD/KZT (обязательный)
    --vat         Процент НДС + АК (обязательный)
    --date-from   Начало периода YYYY-MM-DD (опционально, по умолчанию: last_7d)
    --date-to     Конец периода YYYY-MM-DD  (опционально, по умолчанию: last_7d)
    --sheets-id   ID таблицы Google Sheets для выгрузки (опционально)
    --no-excel    Не создавать .xlsx файл (удобно при автозапуске через CI)
    --client      Обработать только одного клиента (по ключу из config.yaml)
    --output-dir  Директория для Excel-файлов (по умолчанию: output/)
    --list-clients Показать список клиентов из конфига и выйти

Переменные окружения:
    META_ACCESS_TOKEN       -- токен Meta Marketing API
    GOOGLE_CREDENTIALS_JSON -- JSON-ключ сервисного аккаунта Google (строкой)
    GOOGLE_SPREADSHEET_ID   -- ID таблицы Sheets (альтернатива --sheets-id)
    *_ACT_ID                -- ID рекламных кабинетов (AMK_ACT_ID и т.д.)
    REPORT_RATE_USD_KZT     -- курс (альтернатива --rate, для автозапуска)
    REPORT_VAT_PCT          -- НДС % (альтернатива --vat, для автозапуска)
"""
import argparse
import io
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Принудительно переключаем stdout/stderr в UTF-8 на Windows
# (по умолчанию PowerShell/CMD использует cp1252, что ломает кириллицу в логах)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from report.calculator import calculate_report
from report.excel_writer import write_excel_report
from report.sheets_writer import build_service, write_report_block
from sources.meta import MetaAdsSource


# ---------------------------------------------------------------------------
# Настройка логирования
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Настраивает вывод в консоль и в файл logs/report_YYYY-MM-DD.log."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    log_filename = log_dir / f"report_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    date_format = "%H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_filename, encoding="utf-8"),
    ]

    logging.basicConfig(level=logging.INFO, format=log_format,
                        datefmt=date_format, handlers=handlers)
    logging.info("Логирование запущено → %s", log_filename)


# ---------------------------------------------------------------------------
# CLI-аргументы
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Еженедельный рекламный отчет Meta Ads -> Excel / Google Sheets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python run_report.py --rate 490 --vat 12
  python run_report.py --rate 490 --vat 12 --date-from 2026-07-28 --date-to 2026-08-03
  python run_report.py --rate 490 --vat 12 --sheets-id 1BxiM...upms
  python run_report.py --rate 490 --vat 12 --no-excel
  python run_report.py --list-clients
""",
    )
    parser.add_argument(
        "--rate", type=float,
        help="Курс USD/KZT на неделю (например: 490). Также читается из REPORT_RATE_USD_KZT."
    )
    parser.add_argument(
        "--vat", type=float,
        help="Процент НДС + АК (например: 12). Также читается из REPORT_VAT_PCT."
    )
    parser.add_argument(
        "--date-from", dest="date_from",
        help="Начало периода YYYY-MM-DD (по умолчанию: последние 7 дней)"
    )
    parser.add_argument(
        "--date-to", dest="date_to",
        help="Конец периода YYYY-MM-DD (по умолчанию: последние 7 дней)"
    )
    parser.add_argument(
        "--sheets-id", dest="sheets_id",
        help="ID таблицы Google Sheets. Также читается из GOOGLE_SPREADSHEET_ID."
    )
    parser.add_argument(
        "--no-excel", dest="no_excel", action="store_true",
        help="Не создавать .xlsx файл (удобно при автозапуске через GitHub Actions)"
    )
    parser.add_argument(
        "--client",
        help="Обработать только одного клиента (ключ из config.yaml, например: amk)"
    )
    parser.add_argument(
        "--output-dir", dest="output_dir", default="output",
        help="Директория для сохранения Excel-файлов (по умолчанию: output/)"
    )
    parser.add_argument(
        "--list-clients", action="store_true",
        help="Показать список клиентов из config.yaml и выйти"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    # Загружаем .env
    load_dotenv()

    # Парсим аргументы
    args = parse_args()

    # Загружаем конфиг
    try:
        config = load_config()
    except FileNotFoundError:
        log.error("Файл config.yaml не найден. Запустите скрипт из директории проекта.")
        sys.exit(1)

    clients_config = config.get("clients", {})

    # --list-clients
    if args.list_clients:
        print("\nКлиенты в config.yaml:")
        for key, val in clients_config.items():
            act_id = os.getenv(val.get("act_id_env", ""), "не задан")
            print(f"  {key:10s} -> {val.get('name', key):10s}  |  act_id: {act_id}")
        sys.exit(0)

    # --rate и --vat: аргументы или переменные окружения
    rate = args.rate if args.rate is not None else (float(os.getenv("REPORT_RATE_USD_KZT", "0")) or None)
    if args.vat is not None:
        vat = args.vat
    elif os.getenv("REPORT_VAT_PCT") is not None:
        vat = float(os.getenv("REPORT_VAT_PCT"))
    else:
        vat = None

    if rate is None or vat is None:
        print("\nОшибка: необходимо указать --rate и --vat (или REPORT_RATE_USD_KZT / REPORT_VAT_PCT)\n")
        print("Пример:")
        print("  python run_report.py --rate 490 --vat 12\n")
        sys.exit(1)

    # Google Sheets ID: аргумент или переменная окружения
    sheets_id = args.sheets_id or os.getenv("GOOGLE_SPREADSHEET_ID")
    google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if sheets_id and not google_creds:
        log.error(
            "GOOGLE_CREDENTIALS_JSON не задан -- без него нельзя писать в Sheets. "
            "Добавьте JSON-ключ сервисного аккаунта в переменные окружения."
        )
        sys.exit(1)

    # Парсим даты
    date_from: date | None = None
    date_to: date | None = None
    if args.date_from:
        try:
            date_from = date.fromisoformat(args.date_from)
        except ValueError:
            log.error("Неверный формат --date-from: %s (ожидается YYYY-MM-DD)", args.date_from)
            sys.exit(1)
    if args.date_to:
        try:
            date_to = date.fromisoformat(args.date_to)
        except ValueError:
            log.error("Неверный формат --date-to: %s (ожидается YYYY-MM-DD)", args.date_to)
            sys.exit(1)

    # Токен из переменных окружения
    access_token = os.getenv("META_ACCESS_TOKEN")
    if not access_token:
        log.error(
            "Переменная окружения META_ACCESS_TOKEN не задана. "
            "Создайте файл .env (см. .env.example)."
        )
        sys.exit(1)

    # Инициализируем источник данных Meta
    meta_cfg = config.get("meta_api", {})
    source = MetaAdsSource(
        access_token=access_token,
        api_version=meta_cfg.get("version", "v19.0"),
        base_url=meta_cfg.get("base_url", "https://graph.facebook.com"),
        timeout=meta_cfg.get("timeout_seconds", 30),
        max_retries=meta_cfg.get("max_retries", 3),
        retry_delay=meta_cfg.get("retry_delay_seconds", 5),
        campaigns_per_page=meta_cfg.get("campaigns_per_page", 100),
    )

    # Выбираем клиентов для обработки
    if args.client:
        if args.client not in clients_config:
            log.error(
                "Клиент '%s' не найден в config.yaml. Доступные: %s",
                args.client,
                ", ".join(clients_config.keys()),
            )
            sys.exit(1)
        clients_to_process = {args.client: clients_config[args.client]}
    else:
        clients_to_process = clients_config

    log.info(
        "=== Запуск отчета | Клиентов: %d | Курс: %.2f KZT | НДС: %.1f%% | Период: %s -> %s | Sheets: %s ===",
        len(clients_to_process),
        rate,
        vat,
        date_from or "last_7d",
        date_to or "last_7d",
        sheets_id or "нет",
    )

    # ---------------------------------------------------------------------------
    # Обработка каждого клиента (ошибка одного не останавливает остальных)
    # ---------------------------------------------------------------------------
    results = {"success": [], "error": [], "empty": []}

    for client_key, client_cfg in clients_to_process.items():
        client_name = client_cfg.get("name", client_key.upper())
        act_id_env = client_cfg.get("act_id_env", "")
        act_id = client_cfg.get("act_id") or (os.getenv(act_id_env) if act_id_env else None)
        lead_action_types = client_cfg.get("lead_action_types", ["lead"])

        if not act_id:
            log.warning(
                "[%s] Переменная окружения '%s' не задана — клиент пропущен.",
                client_name, act_id_env
            )
            results["error"].append(client_name)
            continue

        log.info("[%s] ─── Обработка клиента ─────────────────────────", client_name)

        try:
            # 1. Сбор данных из Meta API
            campaigns = source.fetch(
                client_id=client_key,
                act_id=act_id,
                lead_action_types=lead_action_types,
                date_from=date_from,
                date_to=date_to,
            )

            if not campaigns:
                d_from_str = date_from.strftime('%Y-%m-%d') if date_from else "last_7d"
                d_to_str = date_to.strftime('%Y-%m-%d') if date_to else "last_7d"
                log.warning("[%s] Нет данных за период %s - %s", client_name, d_from_str, d_to_str)

            # 2. Расчет показателей
            d_from_label = date_from.strftime('%d.%m.%Y') if date_from else ""
            d_to_label = date_to.strftime('%d.%m.%Y') if date_to else ""

            client_report = calculate_report(
                client_id=client_key,
                client_name=client_name,
                campaigns=campaigns,
                rate_usd_kzt=rate,
                vat_pct=vat,
                date_from=d_from_label,
                date_to=d_to_label,
            )



            # 3a. Excel (если не отключен флагом --no-excel)
            if not args.no_excel:
                filepath = write_excel_report(client_report, output_dir=args.output_dir)
                results["success"].append((client_name, filepath))
            else:
                results["success"].append((client_name, "(Excel пропущен)"))

            # 3b. Google Sheets (если задан ID таблицы)
            if sheets_id and google_creds:
                try:
                    sheets_service = build_service(google_creds)
                    write_report_block(
                        service=sheets_service,
                        spreadsheet_id=sheets_id,
                        client_report=client_report,
                        client_name=client_name,
                        date_from=d_from_label,
                        date_to=d_to_label,
                    )
                    log.info("[%s] Успешно загружено в Google Sheets", client_name)
                except Exception as e:
                    log.error("[%s] Ошибка записи в Google Sheets: %s", client_name, e)

        except Exception as exc:
            log.error(
                "[%s] Непредвиденная ошибка: %s",
                client_name, exc, exc_info=True
            )
            results["error"].append(client_name)

    # ---------------------------------------------------------------------------
    # Итоговая сводка
    # ---------------------------------------------------------------------------
    log.info("")
    log.info("=== ИТОГИ ЗАПУСКА ===")
    if results["success"]:
        log.info("[OK] Успешно сформированы отчеты:")
        for name, path in results["success"]:
            log.info("     %s -> %s", name, path)
    if results["empty"]:
        log.info("[--] Нет данных за период (файлы не созданы): %s", ", ".join(results["empty"]))
    if results["error"]:
        log.warning("[!!] Ошибки (клиенты пропущены): %s", ", ".join(results["error"]))

    total_ok = len(results["success"])
    total_all = len(clients_to_process)
    log.info("---------------------")
    log.info("Завершено: %d/%d клиентов успешно", total_ok, total_all)

    if results["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
