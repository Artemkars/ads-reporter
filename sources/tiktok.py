import json
import logging
from datetime import date, timedelta
from typing import Optional
import requests
from sources.base import CampaignRow, DataSource

logger = logging.getLogger(__name__)

class TikTokAdsSource(DataSource):
    """
    Источник данных из TikTok Business API v1.3.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://business-api.tiktok.com/open_api/v1.3"
        self.headers = {
            "Access-Token": self.access_token,
            "Content-Type": "application/json"
        }

    def fetch(
        self,
        client_id: str,
        act_id: str,
        lead_action_types: list[str],
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[CampaignRow]:
        if not self.access_token:
            logger.warning("[%s] TIKTOK_ACCESS_TOKEN не задан, пропускаем TikTok.", client_id.upper())
            return []

        if not act_id:
            logger.warning("[%s] tiktok_advertiser_id не задан в config, пропускаем TikTok.", client_id.upper())
            return []
            
        d_from = date_from or (date.today() - timedelta(days=7))
        d_to = date_to or date.today()
        d_from_str = d_from.strftime('%Y-%m-%d')
        d_to_str = d_to.strftime('%Y-%m-%d')

        logger.info("[%s] Парсинг TikTok Ads %s - %s", client_id.upper(), d_from_str, d_to_str)
        
        # 1. Fetch hierarchy (Campaigns, AdGroups, Ads)
        tree = {} # camp_id -> {..., 'adsets': {adset_id -> {..., 'ads': {ad_id -> {...}}}}}
        
        # 1.1 Campaigns
        c_url = f"{self.base_url}/campaign/get/"
        c_params = {"advertiser_id": act_id, "page_size": 1000}
        c_resp = requests.get(c_url, headers=self.headers, params=c_params).json()
        if c_resp.get("code") != 0:
            logger.error("[%s] TikTok Campaigns Error: %s", client_id.upper(), c_resp)
            return []
            
        for c in c_resp.get("data", {}).get("list", []):
            c_id = str(c.get("campaign_id"))
            objective = c.get("objective_type", "")
            tree[c_id] = {
                "name": c.get("campaign_name", f"Campaign {c_id}"),
                "status": c.get("operation_status", "UNKNOWN"),
                "is_lead_campaign": objective in ("LEAD_GENERATION", "CONVERSIONS"),
                "spend": 0.0,
                "results": 0,
                "adsets": {}
            }
            
        # 1.2 AdGroups
        a_url = f"{self.base_url}/adgroup/get/"
        a_params = {"advertiser_id": act_id, "page_size": 1000}
        a_resp = requests.get(a_url, headers=self.headers, params=a_params).json()
        if a_resp.get("code") == 0:
            for a in a_resp.get("data", {}).get("list", []):
                c_id = str(a.get("campaign_id"))
                a_id = str(a.get("adgroup_id"))
                if c_id in tree:
                    tree[c_id]["adsets"][a_id] = {
                        "name": a.get("adgroup_name", f"AdGroup {a_id}"),
                        "status": a.get("operation_status", "UNKNOWN"),
                        "spend": 0.0,
                        "results": 0,
                        "ads": {}
                    }
                    
        # 1.3 Ads
        ad_url = f"{self.base_url}/ad/get/"
        ad_params = {"advertiser_id": act_id, "page_size": 1000}
        ad_resp = requests.get(ad_url, headers=self.headers, params=ad_params).json()
        if ad_resp.get("code") == 0:
            for ad in ad_resp.get("data", {}).get("list", []):
                c_id = str(ad.get("campaign_id"))
                a_id = str(ad.get("adgroup_id"))
                ad_id = str(ad.get("ad_id"))
                if c_id in tree and a_id in tree[c_id]["adsets"]:
                    tree[c_id]["adsets"][a_id]["ads"][ad_id] = {
                        "name": ad.get("ad_name", f"Ad {ad_id}"),
                        "status": ad.get("operation_status", "UNKNOWN"),
                        "spend": 0.0,
                        "results": 0
                    }

        # 2. Fetch Report (Metrics)
        r_url = f"{self.base_url}/report/integrated/get/"
        r_params = {
            "advertiser_id": act_id,
            "report_type": "BASIC",
            "data_level": "AUCTION_AD",
            "dimensions": json.dumps(["campaign_id", "adgroup_id", "ad_id"]),
            "metrics": json.dumps(["spend", "conversion"]),
            "start_date": d_from_str,
            "end_date": d_to_str,
            "page_size": 1000
        }
        r_resp = requests.get(r_url, headers=self.headers, params=r_params).json()
        if r_resp.get("code") != 0:
            logger.error("[%s] TikTok Report Error: %s", client_id.upper(), r_resp)
        else:
            metrics_list = r_resp.get("data", {}).get("list", [])
            for row in metrics_list:
                m = row.get("metrics", {})
                d = row.get("dimensions", {})
                c_id = str(d.get("campaign_id"))
                a_id = str(d.get("adgroup_id"))
                ad_id = str(d.get("ad_id"))
                
                spend = float(m.get("spend", 0.0) or 0.0)
                results = int(m.get("conversion", 0) or 0)
                
                if spend == 0 and results == 0:
                    continue
                    
                if c_id in tree:
                    tree[c_id]["spend"] += spend
                    tree[c_id]["results"] += results
                    if a_id in tree[c_id]["adsets"]:
                        tree[c_id]["adsets"][a_id]["spend"] += spend
                        tree[c_id]["adsets"][a_id]["results"] += results
                        if ad_id in tree[c_id]["adsets"][a_id]["ads"]:
                            tree[c_id]["adsets"][a_id]["ads"][ad_id]["spend"] += spend
                            tree[c_id]["adsets"][a_id]["ads"][ad_id]["results"] += results

        # 3. Flatten and filter empty
        result_rows = []
        for c_id, c_data in tree.items():
            if c_data["spend"] == 0 and c_data["results"] == 0:
                continue
                
            is_lead = c_data.get("is_lead_campaign", False)
            result_rows.append(CampaignRow(
                level="campaign",
                name=c_data["name"],
                status=c_data["status"],
                spend_usd=c_data["spend"],
                results=c_data["results"],
                date_from=d_from,
                date_to=d_to,
                client_id=client_id,
                source="tiktok",
                is_lead_campaign=is_lead
            ))
            
            for a_id, a_data in c_data["adsets"].items():
                if a_data["spend"] == 0 and a_data["results"] == 0:
                    continue
                result_rows.append(CampaignRow(
                    level="adset",
                    name=a_data["name"],
                    status=a_data["status"],
                    spend_usd=a_data["spend"],
                    results=a_data["results"],
                    date_from=d_from,
                    date_to=d_to,
                    client_id=client_id,
                    source="tiktok",
                    is_lead_campaign=is_lead
                ))
                
                for ad_id, ad_data in a_data["ads"].items():
                    if ad_data["spend"] == 0 and ad_data["results"] == 0:
                        continue
                    result_rows.append(CampaignRow(
                        level="ad",
                        name=ad_data["name"],
                        status=ad_data["status"],
                        spend_usd=ad_data["spend"],
                        results=ad_data["results"],
                        date_from=d_from,
                        date_to=d_to,
                        client_id=client_id,
                        source="tiktok",
                        is_lead_campaign=is_lead
                    ))
                    
        return result_rows
