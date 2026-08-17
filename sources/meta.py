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

INSIGHTS_FIELDS = "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,objective,optimization_goal,reach,clicks,date_start,date_stop"

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
            active_camp_ids = self._get_active_campaign_ids(act_id, date_from, date_to)
            if not active_camp_ids:
                logger.info("[%s] Нет активных кампаний с расходами/лидами за этот период.", client_id.upper())
                return []
                
            raw_insights = []
            for camp_id in active_camp_ids:
                insights = self._fetch_ad_insights(camp_id, date_from, date_to)
                raw_insights.extend(insights)
        except Exception as exc:
            if hasattr(exc, "response") and exc.response is not None:
                logger.error("[%s] Критическая ошибка API Meta: %s\nBODY: %s", client_id.upper(), exc, exc.response.text)
            else:
                logger.error("[%s] Критическая ошибка API Meta: %s", client_id.upper(), exc)
            return []

        if not raw_insights:
            return []

        # Запрашиваем статусы всех кампаний, групп и объявлений
        statuses = self._fetch_all_statuses(act_id)

        # Построение дерева
        
        def new_ad_node():
            return {"name": "", "spend": 0.0, "results": 0, "reach": 0, "status": "UNKNOWN"}
            
        def new_adset_node():
            return {"name": "", "spend": 0.0, "results": 0, "reach": 0, "status": "UNKNOWN", "ads": defaultdict(new_ad_node)}
            
        def new_camp_node():
            return {"name": "", "spend": 0.0, "results": 0, "reach": 0, "status": "UNKNOWN", "adsets": defaultdict(new_adset_node)}

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
            actions = insight.get("actions", [])
            objective = insight.get("objective", "")
            opt_goal = insight.get("optimization_goal", "")
            results = 0
            reach = 0
            
            lead_actions = sum(int(act.get("value", 0)) for act in actions if act.get("action_type") in lead_action_types)
            msg_actions = sum(int(act.get("value", 0)) for act in actions if act.get("action_type") == "onsite_conversion.messaging_conversation_started_7d")
            eng_actions = sum(int(act.get("value", 0)) for act in actions if act.get("action_type") == "post_engagement")

            if objective in ("OUTCOME_LEADS", "LEAD_GENERATION", "CONVERSIONS", "OUTCOME_SALES"):
                results = lead_actions
            elif objective in ("MESSAGES", "MESSAGING"):
                results = msg_actions
            elif objective in ("OUTCOME_TRAFFIC", "LINK_CLICKS"):
                results = int(insight.get("clicks", 0))
            elif objective in ("OUTCOME_AWARENESS", "REACH", "BRAND_AWARENESS"):
                reach = int(insight.get("reach", 0))
                results = 0
            elif objective in ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT"):
                # Если оптимизация на переписки, считаем переписки
                if opt_goal in ("REPLIES", "CONVERSATIONS"):
                    results = msg_actions
                # Иначе это вовлеченность в пост/профиль -> охваты
                else:
                    reach = int(insight.get("reach", 0))
                    results = 0
            else:
                if lead_actions > 0:
                    results = lead_actions
                elif msg_actions > 0:
                    results = msg_actions
                else:
                    results = int(insight.get("clicks", 0))
                    
            # Если нет ни расходов, ни результатов, ни охватов - пропускаем
            if spend == 0 and results == 0 and reach == 0:
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
            camp_node["objective"] = objective
            camp_node["opt_goal"] = opt_goal
            if msg_actions > 0:
                camp_node["has_messages"] = True
            
            # Обновляем Группу
            adset_node = camp_node["adsets"][a_id]
            adset_node["name"] = insight.get("adset_name", "Без названия")
            adset_node["status"] = statuses.get(a_id, "UNKNOWN")
            adset_node["spend"] += spend
            adset_node["results"] += results
            adset_node["reach"] += reach
            
            # Обновляем Объявление
            ad_node = adset_node["ads"][ad_id]
            ad_node["name"] = insight.get("ad_name", "Без названия")
            ad_node["status"] = statuses.get(ad_id, "UNKNOWN")
            ad_node["spend"] += spend
            ad_node["results"] += results
            ad_node["reach"] += reach
        
        # Пересчет итогов для кампаний
        for c_id, c_data in tree.items():
            c_data["spend"] = sum(a["spend"] for a in c_data["adsets"].values())
            c_data["results"] = sum(a["results"] for a in c_data["adsets"].values())
            c_data["reach"] = sum(a["reach"] for a in c_data["adsets"].values())
            
        if min_date > max_date:
            min_date = date_from or (date.today() - timedelta(days=7))
            max_date = max_date or date.today()

        # Разворачиваем в плоский список
        rows = []
        for c_id, c_data in tree.items():
            obj = c_data.get("objective", "")
            opt = c_data.get("opt_goal", "")
            has_msg = c_data.get("has_messages", False)
            
            is_lead = obj in ("OUTCOME_LEADS", "LEAD_GENERATION", "CONVERSIONS", "OUTCOME_SALES", "MESSAGES", "MESSAGING") or (obj in ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT") and opt in ("REPLIES", "CONVERSATIONS"))
            is_aw = obj in ("OUTCOME_AWARENESS", "REACH", "BRAND_AWARENESS") or (obj in ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT") and opt not in ("REPLIES", "CONVERSATIONS"))
            
            rows.append(CampaignRow(
                level="campaign",
                name=c_data["name"],
                status=c_data["status"],
                spend_usd=c_data["spend"],
                results=c_data["results"],
                date_from=min_date,
                date_to=max_date,
                client_id=client_id,
                is_lead_campaign=is_lead,
                reach=c_data["reach"],
                is_awareness_campaign=is_aw
            ))
            
            for a_id, a_data in c_data["adsets"].items():
                rows.append(CampaignRow(
                    level="adset",
                    name=a_data["name"],
                    status=a_data["status"],
                    spend_usd=a_data["spend"],
                    results=a_data["results"],
                    date_from=min_date,
                    date_to=max_date,
                    client_id=client_id,
                    is_lead_campaign=is_lead,
                    reach=a_data["reach"],
                    is_awareness_campaign=is_aw
                ))
                
                for ad_id, ad_data in a_data["ads"].items():
                    rows.append(CampaignRow(
                        level="ad",
                        name=ad_data["name"],
                        status=ad_data["status"],
                        spend_usd=ad_data["spend"],
                        results=ad_data["results"],
                        date_from=min_date,
                        date_to=max_date,
                        client_id=client_id,
                        is_lead_campaign=is_lead,
                        reach=ad_data["reach"],
                        is_awareness_campaign=is_aw
                    ))

        logger.info(
            "[%s] Итого сформировано строк: %d (кампаний: %d)",
            client_id.upper(), len(rows), len(tree)
        )
        return rows


    def _get_active_campaign_ids(self, act_id: str, date_from: Optional[date], date_to: Optional[date]) -> list[str]:
        """Получает список ID кампаний, у которых были показы/расходы в данный период."""
        params = {
            "level": "campaign",
            "fields": "campaign_id",
            "limit": 500,
            "access_token": self.access_token,
        }
        if date_from and date_to:
            params["time_range"] = f'{{"since":"{date_from.isoformat()}","until":"{date_to.isoformat()}"}}'
        else:
            params["date_preset"] = "last_7d"
            
        url = f"{self.base_url}/{self.api_version}/{act_id}/insights"
        camp_ids = []
        while url:
            data = self._get_with_retry(url, params)
            for item in data.get("data", []):
                if item.get("campaign_id"):
                    camp_ids.append(item["campaign_id"])
                    
            next_url = data.get("paging", {}).get("next")
            if next_url:
                url = next_url
                params = {}
            else:
                url = None
                
        return list(set(camp_ids))

    def _fetch_ad_insights(
        self, node_id: str, date_from: Optional[date], date_to: Optional[date]
    ) -> list[dict]:
        """Забирает инсайты на уровне объявлений с пагинацией для конкретного узла."""
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

        url = f"{self.base_url}/{self.api_version}/{node_id}/insights"
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

    def _fetch_all_statuses(self, act_id: str) -> dict[str, str]:
        """Запрашивает статусы всех объектов кабинета (кампании, группы, объявления)."""
        statuses = {}
        
        for edge in ["campaigns", "adsets", "ads"]:
            url = f"{self.base_url}/{self.api_version}/{act_id}/{edge}"
            params = {
                "fields": "id,status,effective_status",
                "limit": 500,
                "access_token": self.access_token,
            }
            while url:
                try:
                    data = self._get_with_retry(url, params)
                    for item in data.get("data", []):
                        obj_id = item.get("id")
                        if obj_id:
                            st = item.get("effective_status", item.get("status", "UNKNOWN"))
                            statuses[obj_id] = st
                            
                    next_url = data.get("paging", {}).get("next")
                    if next_url:
                        url = next_url
                        params = {}
                    else:
                        url = None
                except Exception as e:
                    logger.warning("Ошибка при пакетном запросе статусов %s: %s", edge, e)
                    break
                    
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
