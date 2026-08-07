"""
sources/google.py

Источник данных Google Ads.
"""
import logging
from datetime import date, timedelta
from typing import Optional
from collections import defaultdict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from sources.base import DataSource, CampaignRow

logger = logging.getLogger(__name__)

class GoogleAdsSource(DataSource):
    """Источник данных Google Ads с иерархией Кампания -> Группа -> Объявление."""
    
    def __init__(
        self,
        developer_token: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        login_customer_id: str
    ):
        credentials = {
            "developer_token": developer_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "login_customer_id": login_customer_id,
            "use_proto_plus": True
        }
        try:
            self.client = GoogleAdsClient.load_from_dict(credentials)
            self.ga_service = self.client.get_service("GoogleAdsService")
        except Exception as e:
            logger.error("Ошибка инициализации Google Ads Client: %s", e)
            self.client = None

    def fetch(
        self,
        client_id: str,
        act_id: str,  # google_customer_id e.g. "1234567890"
        lead_action_types: list[str], # Not strictly used in GA, we use conversions
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[CampaignRow]:
        if not self.client:
            logger.warning("[%s] Google Ads Client не инициализирован.", client_id.upper())
            return []
            
        if not act_id:
            return []
            
        customer_id = str(act_id).replace("-", "")

        d_from = date_from or (date.today() - timedelta(days=7))
        d_to = date_to or (date.today() - timedelta(days=1))
        date_query = f"segments.date >= '{d_from.isoformat()}' AND segments.date <= '{d_to.isoformat()}'"

        # Мы делаем один большой запрос по ad_group_ad, фильтруя по показам > 0 (или расходам)
        # Это даст нам сразу всю иерархию для активных объектов
        query = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.status,
              campaign.bidding_strategy_type,
              ad_group.id,
              ad_group.name,
              ad_group.status,
              ad_group_ad.ad.id,
              ad_group_ad.ad.name,
              ad_group_ad.status,
              metrics.cost_micros,
              metrics.conversions,
              metrics.clicks,
              metrics.impressions
            FROM ad_group_ad
            WHERE {date_query}
              AND metrics.impressions > 0
        """

        try:
            # stream
            request = self.client.get_type("SearchGoogleAdsStreamRequest")
            request.customer_id = customer_id
            request.query = query
            stream = self.ga_service.search_stream(request)
            
            # Построение дерева
            def new_ad_node():
                return {"name": "", "spend": 0.0, "results": 0, "status": "UNKNOWN"}
            def new_adset_node():
                return {"name": "", "spend": 0.0, "results": 0, "status": "UNKNOWN", "ads": defaultdict(new_ad_node)}
            def new_camp_node():
                return {"name": "", "spend": 0.0, "results": 0, "status": "UNKNOWN", "adsets": defaultdict(new_adset_node)}
                
            tree = defaultdict(new_camp_node)
            
            for batch in stream:
                for row in batch.results:
                    camp = row.campaign
                    ad_group = row.ad_group
                    ad_group_ad = row.ad_group_ad
                    metrics = row.metrics
                    
                    camp_id = str(camp.id)
                    adset_id = str(ad_group.id)
                    ad_id = str(ad_group_ad.ad.id)
                    
                    spend = metrics.cost_micros / 1000000.0
                    
                    # Определение "Результатов" на основе стратегии
                    bidding = camp.bidding_strategy_type.name if camp.bidding_strategy_type else ""
                    if bidding in ("TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"):
                        results = int(metrics.conversions)
                    elif bidding in ("MAXIMIZE_CLICKS", "MANUAL_CPC"):
                        results = int(metrics.clicks)
                    elif bidding in ("TARGET_IMPRESSION_SHARE", "MANUAL_CPM", "MANUAL_CPV"):
                        results = int(metrics.impressions)
                    else:
                        # Fallback: Если есть конверсии - они, иначе клики
                        results = int(metrics.conversions) if metrics.conversions > 0 else int(metrics.clicks)
                        
                    if spend == 0 and results == 0:
                        continue
                        
                    c_node = tree[camp_id]
                    c_node["name"] = camp.name
                    c_node["status"] = camp.status.name
                    
                    a_node = c_node["adsets"][adset_id]
                    a_node["name"] = ad_group.name
                    a_node["status"] = ad_group.status.name
                    
                    ad_node = a_node["ads"][ad_id]
                    # Ad names can be empty in some formats (like responsive search ads), provide a fallback
                    ad_node["name"] = ad_group_ad.ad.name or f"Ad {ad_id}"
                    ad_node["status"] = ad_group_ad.status.name
                    ad_node["spend"] += spend
                    ad_node["results"] += results
                    
            # Агрегация сумм снизу вверх
            for c_id, c_node in tree.items():
                for a_id, a_node in c_node["adsets"].items():
                    a_node["spend"] = sum(ad["spend"] for ad in a_node["ads"].values())
                    a_node["results"] = sum(ad["results"] for ad in a_node["ads"].values())
                
                c_node["spend"] = sum(a["spend"] for a in c_node["adsets"].values())
                c_node["results"] = sum(a["results"] for a in c_node["adsets"].values())
                
            # Flatten
            result_rows = []
            for c_id, c_data in tree.items():
                result_rows.append(CampaignRow(
                    level="campaign",
                    name=c_data["name"],
                    status=self._format_status(c_data["status"]),
                    spend_usd=c_data["spend"],
                    results=c_data["results"],
                    date_from=d_from,
                    date_to=d_to,
                    client_id=client_id,
                    source="google"
                ))
                for a_id, a_data in c_data["adsets"].items():
                    result_rows.append(CampaignRow(
                        level="adset",
                        name=a_data["name"],
                        status=self._format_status(a_data["status"]),
                        spend_usd=a_data["spend"],
                        results=a_data["results"],
                        date_from=d_from,
                        date_to=d_to,
                        client_id=client_id,
                        source="google"
                    ))
                    for ad_id, ad_data in a_data["ads"].items():
                        result_rows.append(CampaignRow(
                            level="ad",
                            name=ad_data["name"],
                            status=self._format_status(ad_data["status"]),
                            spend_usd=ad_data["spend"],
                            results=ad_data["results"],
                            date_from=d_from,
                            date_to=d_to,
                            client_id=client_id,
                            source="google"
                        ))
            return result_rows

        except GoogleAdsException as ex:
            logger.error("[%s] Google Ads API Exception: %s", client_id.upper(), ex)
            return []
        except Exception as e:
            logger.error("[%s] Ошибка Google Ads: %s", client_id.upper(), e)
            return []

    def _format_status(self, status: str) -> str:
        # GA statuses like ENABLED, PAUSED, REMOVED
        mapping = {
            "ENABLED": "Активен",
            "PAUSED": "Приостановлен",
            "REMOVED": "Удален",
            "UNKNOWN": "UNKNOWN"
        }
        return mapping.get(status.upper(), status)
