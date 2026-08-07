"""
sources/meta.py

Источник данных: Meta Marketing API (Graph API v19.0).
Реализует абстрактный DataSource для рекламных кабинетов Meta Ads.

Изменения:
- Поддерживает уровень вложенности: Кампания -> Группа -> Объявление.
- Запрашивает /insights?level=ad, чтобы получить только те объекты, по которым были расходы/лиды.
- Пакетно запрашивает статусы объектов.
"""
import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

import requests

from .base import DataSource, CampaignRow

logger = logging.getLogger(__name__)

INSIGHTS_FIELDS = "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,date_start,date_stop"

class MetaAdsSource(DataSource):
    """Реализация DataSource для Meta Marketing API с поддержкой иерархии."""

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

    def fetch(
        self,
        client_id: str,
        act_id: str,
        lead_action_types: list[str],
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[CampaignRow]:
        """
        Забирает данные по всем объявлениям кабинета за указанный период (Insights).
        """
        logger.info(
            "[%s] Начинаем сбор данных из Meta (Insights level=ad) | кабинет: %s",
            client_id.upper(), act_id
        )

        try:
            raw_insights = self._fetch_ad_insights(act_id, date_from, date_to)
        except Exception as exc:
            logger.error("[%s] Критическая ошибка API Meta: %s", client_id.upper(), exc)
            return []

        if not raw_insights:
            return []

        # Сбор уникальных ID для запроса статусов
        object_ids = set()
        for insight in raw_insights:
            if "campaign_id" in insight: object_ids.add(insight["campaign_id"])
            if "adset_id" in insight: object_ids.add(insight["adset_id"])
            if "ad_id" in insight: object_ids.add(insight["ad_id"])

        statuses = self._fetch_statuses(list(object_ids))

        # Построение дерева
        # tree[campaign_id] = { name, spend, leads, status, adsets: { adset_id: { ... ads: { ad_id: {...} } } } }
        
        def new_ad_node():
            return {"name": "", "spend": 0.0, "leads": 0, "status": "UNKNOWN"}
            
        def new_adset_node():
            return {"name": "", "spend": 0.0, "leads": 0, "status": "UNKNOWN", "ads": defaultdict(new_ad_node)}
            
        def new_camp_node():
            return {"name": "", "spend": 0.0, "leads": 0, "status": "UNKNOWN", "adsets": defaultdict(new_adset_node)}

        tree = defaultdict(new_camp_node)
        
        d_from_str = date_from.isoformat() if date_from else (date.today() - timedelta(days=7)).isoformat()
        d_to_str = date_to.isoformat() if date_to else date.today().isoformat()
        
        min_date = date.today()
        max_date = date(2000, 1, 1)

        for insight in raw_insights:
            c_id = insight.get("campaign_id")
            a_id = insight.get("adset_id")
            ad_id = insight.get("ad_id")
            if not c_id or not a_id or not ad_id:
                continue
                
            spend = float(insight.get("spend", 0) or 0)
            
            # Подсчет лидов
            leads = 0
            for action in insight.get("actions", []):
                atype = action.get("action_type", "")
                if atype in lead_action_types:
                    leads += int(action.get("value", 0) or 0)
                    
            # Если нет ни расходов, ни лидов - пропускаем (обычно Insights API и так не отдаст, но на всякий случай)
            if spend == 0 and leads == 0:
                continue

            try:
                curr_d_from = date.fromisoformat(insight.get("date_start", d_from_str))
                curr_d_to = date.fromisoformat(insight.get("date_stop", d_to_str))
                min_date = min(min_date, curr_d_from)
                max_date = max(max_date, curr_d_to)
            except Exception:
                pass

            # Обновляем Кампанию
            camp_node = tree[c_id]
            camp_node["name"] = insight.get("campaign_name", "Без названия")
            camp_node["status"] = statuses.get(c_id, "UNKNOWN")
            camp_node["spend"] += spend
            camp_node["leads"] += leads
            
            # Обновляем Группу
            adset_node = camp_node["adsets"][a_id]
            adset_node["name"] = insight.get("adset_name", "Без названия")
            adset_node["status"] = statuses.get(a_id, "UNKNOWN")
            adset_node["spend"] += spend
            adset_node["leads"] += leads
            
            # Обновляем Объявление
            ad_node = adset_node["ads"][ad_id]
            ad_node["name"] = insight.get("ad_name", "Без названия")
            ad_node["status"] = statuses.get(ad_id, "UNKNOWN")
            ad_node["spend"] += spend
            ad_node["leads"] += leads
            
        if min_date > max_date:
            min_date = date_from or (date.today() - timedelta(days=7))
            max_date = date_to or date.today()

        # Разворачиваем в плоский список
        rows = []
        for c_id, c_data in tree.items():
            if c_data["spend"] == 0 and c_data["leads"] == 0:
                continue
                
            rows.append(CampaignRow(
                level="campaign",
                name=c_data["name"],
                status=c_data["status"],
                spend_usd=c_data["spend"],
                leads=c_data["leads"],
                date_from=min_date,
                date_to=max_date,
                client_id=client_id,
            ))
            
            for a_id, a_data in c_data["adsets"].items():
                rows.append(CampaignRow(
                    level="adset",
                    name=a_data["name"],
                    status=a_data["status"],
                    spend_usd=a_data["spend"],
                    leads=a_data["leads"],
                    date_from=min_date,
                    date_to=max_date,
                    client_id=client_id,
                ))
                
                for ad_id, ad_data in a_data["ads"].items():
                    rows.append(CampaignRow(
                        level="ad",
                        name=ad_data["name"],
                        status=ad_data["status"],
                        spend_usd=ad_data["spend"],
                        leads=ad_data["leads"],
                        date_from=min_date,
                        date_to=max_date,
                        client_id=client_id,
                    ))

        logger.info(
            "[%s] Итого сформировано строк: %d (кампаний: %d)",
            client_id.upper(), len(rows), len(tree)
        )
        return rows


    def _fetch_ad_insights(
        self, act_id: str, date_from: Optional[date], date_to: Optional[date]
    ) -> list[dict]:
        """Забирает инсайты на уровне объявлений с пагинацией."""
        params = {
            "level": "ad",
            "fields": INSIGHTS_FIELDS,
            "limit": self.campaigns_per_page,
            "access_token": self.access_token,
        }

        if date_from and date_to:
            params["time_range"] = f'{{"since":"{date_from.isoformat()}","until":"{date_to.isoformat()}"}}'
        else:
            params["date_preset"] = "last_7d"

        url = f"{self.base_url}/{self.api_version}/{act_id}/insights"
        all_insights = []
        page = 1

        while url:
            logger.debug("Запрашиваем insights (page %d): %s", page, url)
            data = self._get_with_retry(url, params)
            insights = data.get("data", [])
            all_insights.extend(insights)
            
            next_url = data.get("paging", {}).get("next")
            if next_url:
                url = next_url
                params = {} 
            else:
                url = None
            page += 1

        return all_insights

    def _fetch_statuses(self, object_ids: list[str]) -> dict[str, str]:
        """Пакетно запрашивает статусы объектов по ID."""
        statuses = {}
        chunk_size = 50
        
        for i in range(0, len(object_ids), chunk_size):
            chunk = object_ids[i:i + chunk_size]
            url = f"{self.base_url}/{self.api_version}/"
            params = {
                "ids": ",".join(chunk),
                "fields": "status",
                "access_token": self.access_token,
            }
            try:
                data = self._get_with_retry(url, params)
                for obj_id, obj_data in data.items():
                    if isinstance(obj_data, dict):
                        # Для Campaign, AdSet и Ad поле статуса может называться status, 
                        # иногда эффективный статус, но обычно status есть.
                        # Также иногда Meta отдает status как configured_status
                        st = obj_data.get("status", obj_data.get("configured_status", "UNKNOWN"))
                        statuses[obj_id] = st
            except Exception as e:
                logger.warning("Ошибка при пакетном запросе статусов: %s", e)
                
        return statuses

    def _get_with_retry(self, url: str, params: dict) -> dict:
        """GET-запрос с retry при rate-limit (429) и серверных ошибках (5xx)."""
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    wait = self.retry_delay * attempt
                    logger.warning("Rate-limit от Meta API. Ждём %d сек...", wait)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    wait = self.retry_delay * attempt
                    logger.warning("Серверная ошибка Meta API (%d). Ждём %d сек... Ответ: %s", resp.status_code, wait, resp.text)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"Meta API вернул ошибку: [{err.get('code')}] {err.get('message')}")

                return data

            except requests.exceptions.Timeout:
                last_error = f"Таймаут (попытка {attempt})"
                time.sleep(self.retry_delay)
            except requests.exceptions.ConnectionError as e:
                last_error = f"Ошибка соединения: {e}"
                time.sleep(self.retry_delay)

        raise RuntimeError(f"Не удалось получить данные. Последняя ошибка: {last_error}")
