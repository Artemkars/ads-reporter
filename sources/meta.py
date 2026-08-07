"""
sources/meta.py

Источник данных: Meta Marketing API (Graph API v19.0).
Реализует абстрактный DataSource для рекламных кабинетов Meta Ads.

Поддерживает:
- Пагинацию (cursor-based) — забирает все кампании, не только первые 100
- Retry при rate-limit (HTTP 429) и временных ошибках (5xx)
- Произвольный диапазон дат или date_preset=last_7d
- Фильтрацию actions по типам лидов из конфига
"""
import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests

from .base import DataSource, CampaignRow

logger = logging.getLogger(__name__)

# Поля кампании, которые запрашиваем у API
CAMPAIGN_FIELDS = "name,status,insights{spend,actions,date_start,date_stop}"


class MetaAdsSource(DataSource):
    """Реализация DataSource для Meta Marketing API."""

    def __init__(
        self,
        access_token: str,
        api_version: str = "v19.0",
        base_url: str = "https://graph.facebook.com",
        timeout: int = 30,
        max_retries: int = 3,
        retry_delay: int = 5,
        campaigns_per_page: int = 100,
    ):
        self.access_token = access_token
        self.api_version = api_version
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.campaigns_per_page = campaigns_per_page

    # ------------------------------------------------------------------
    # Публичный интерфейс (реализация абстрактного метода)
    # ------------------------------------------------------------------

    def fetch(
        self,
        client_id: str,
        act_id: str,
        lead_action_types: list[str],
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[CampaignRow]:
        """
        Забирает все кампании кабинета за указанный период.
        При ошибках — логирует и возвращает пустой список.
        """
        logger.info(
            "[%s] Начинаем сбор данных из Meta | кабинет: %s | период: %s → %s",
            client_id.upper(),
            act_id,
            date_from or "last_7d",
            date_to or "last_7d",
        )

        try:
            raw_campaigns = self._fetch_all_campaigns(act_id, date_from, date_to)
        except Exception as exc:
            logger.error(
                "[%s] Критическая ошибка при обращении к API Meta: %s", client_id.upper(), exc
            )
            return []

        rows = []
        for camp in raw_campaigns:
            row = self._parse_campaign(camp, client_id, lead_action_types, date_from, date_to)
            if row is not None:
                rows.append(row)

        logger.info(
            "[%s] Получено кампаний: %d (с данными: %d)",
            client_id.upper(),
            len(raw_campaigns),
            len(rows),
        )
        return rows

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _fetch_all_campaigns(
        self,
        act_id: str,
        date_from: Optional[date],
        date_to: Optional[date],
    ) -> list[dict]:
        """Забирает все кампании с пагинацией (cursor-based)."""
        params = self._build_params(date_from, date_to)
        url = f"{self.base_url}/{self.api_version}/{act_id}/campaigns"

        all_campaigns = []
        page = 1

        while url:
            logger.debug("Запрашиваем страницу %d кампаний: %s", page, url)
            data = self._get_with_retry(url, params)
            campaigns = data.get("data", [])
            all_campaigns.extend(campaigns)
            logger.debug("Страница %d: получено %d кампаний", page, len(campaigns))

            # Следующая страница (cursor-based pagination)
            next_url = data.get("paging", {}).get("next")
            if next_url:
                url = next_url
                params = {}  # параметры уже встроены в next URL
            else:
                url = None
            page += 1

        return all_campaigns

    def _build_params(
        self, date_from: Optional[date], date_to: Optional[date]
    ) -> dict:
        """Формирует параметры запроса к API."""
        params = {
            "fields": CAMPAIGN_FIELDS,
            "limit": self.campaigns_per_page,
            "access_token": self.access_token,
        }

        if date_from and date_to:
            # Явный диапазон дат
            params["time_range"] = (
                f'{{"since":"{date_from.isoformat()}","until":"{date_to.isoformat()}"}}'
            )
        else:
            # По умолчанию — последние 7 дней
            params["date_preset"] = "last_7d"

        return params

    def _get_with_retry(self, url: str, params: dict) -> dict:
        """
        GET-запрос с retry при rate-limit (429) и серверных ошибках (5xx).
        Бросает исключение если все попытки исчерпаны.
        """
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)

                if resp.status_code == 429:
                    wait = self.retry_delay * attempt
                    logger.warning(
                        "Rate-limit (429) от Meta API. Ждём %d сек (попытка %d/%d)...",
                        wait, attempt, self.max_retries
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    wait = self.retry_delay * attempt
                    logger.warning(
                        "Серверная ошибка Meta API (%d). Ждём %d сек (попытка %d/%d)...",
                        resp.status_code, wait, attempt, self.max_retries
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # Проверяем ошибку в теле ответа (Meta возвращает 200 + error{})
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(
                        f"Meta API вернул ошибку: [{err.get('code')}] {err.get('message')}"
                    )

                return data

            except requests.exceptions.Timeout:
                last_error = f"Таймаут запроса (попытка {attempt}/{self.max_retries})"
                logger.warning(last_error)
                time.sleep(self.retry_delay)

            except requests.exceptions.ConnectionError as e:
                last_error = f"Ошибка соединения: {e} (попытка {attempt}/{self.max_retries})"
                logger.warning(last_error)
                time.sleep(self.retry_delay)

        raise RuntimeError(
            f"Не удалось получить данные после {self.max_retries} попыток. Последняя ошибка: {last_error}"
        )

    def _parse_campaign(
        self,
        camp: dict,
        client_id: str,
        lead_action_types: list[str],
        date_from: Optional[date],
        date_to: Optional[date],
    ) -> Optional[CampaignRow]:
        """
        Разбирает один объект кампании из ответа API.
        Возвращает CampaignRow или None если нет данных о расходах.
        """
        name = camp.get("name", "Без названия")
        status = camp.get("status", "UNKNOWN")

        insights_data = camp.get("insights", {}).get("data", [])
        if not insights_data:
            # Кампания без insights (нет расходов за период) — включаем с нулями
            logger.debug(
                "[%s] Кампания без insights (нет расходов за период): %s",
                client_id.upper(), name
            )
            effective_date_from = date_from or (date.today() - timedelta(days=7))
            effective_date_to = date_to or date.today()
            return CampaignRow(
                campaign_name=name,
                status=status,
                spend_usd=0.0,
                leads=0,
                date_from=effective_date_from,
                date_to=effective_date_to,
                client_id=client_id,
                source="meta",
                raw_actions={},
            )

        insight = insights_data[0]
        spend_usd = float(insight.get("spend", 0) or 0)

        # Собираем все actions в словарь action_type → value
        raw_actions = {}
        for action in insight.get("actions", []):
            action_type = action.get("action_type", "")
            value = int(action.get("value", 0) or 0)
            raw_actions[action_type] = raw_actions.get(action_type, 0) + value

        # Считаем только те action_type, которые заданы для клиента в конфиге
        leads = sum(raw_actions.get(atype, 0) for atype in lead_action_types)

        # Даты из ответа API
        try:
            d_from = date.fromisoformat(insight.get("date_start", ""))
            d_to = date.fromisoformat(insight.get("date_stop", ""))
        except (ValueError, TypeError):
            d_from = date_from or (date.today() - timedelta(days=7))
            d_to = date_to or date.today()

        return CampaignRow(
            campaign_name=name,
            status=status,
            spend_usd=spend_usd,
            leads=leads,
            date_from=d_from,
            date_to=d_to,
            client_id=client_id,
            source="meta",
            raw_actions=raw_actions,
        )
