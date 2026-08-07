"""
Утилита: показывает все action_type по всем кампаниям кабинета.
Запуск: python tools/inspect_actions.py
"""
import io, sys, os, json, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("META_ACCESS_TOKEN")
ACT_ID = os.getenv("AMK_ACT_ID", "act_1202497118763581")
BASE = "https://graph.facebook.com/v19.0"

def fetch_all(url, params):
    results = []
    while url:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}
    return results

campaigns = fetch_all(
    f"{BASE}/{ACT_ID}/campaigns",
    {
        "fields": "name,status,insights{spend,actions}",
        "date_preset": "maximum",
        "limit": 100,
        "access_token": TOKEN,
    }
)

print(f"\nКабинет: {ACT_ID}")
print(f"Всего кампаний: {len(campaigns)}\n")
print("=" * 90)

# Словарь: action_type -> список кампаний где встречается
action_map = {}

for camp in campaigns:
    name = camp.get("name", "?")
    status = camp.get("status", "?")
    insights = camp.get("insights", {}).get("data", [])

    if not insights:
        print(f"[НЕТ ДАННЫХ]  {name[:70]}")
        continue

    ins = insights[0]
    spend = float(ins.get("spend", 0) or 0)
    actions = ins.get("actions", [])

    action_types = {a["action_type"]: int(a.get("value", 0)) for a in actions}

    print(f"\n[{status:8s}]  {name[:70]}")
    print(f"             Расход: ${spend:.2f}  |  Actions:")
    for atype, val in sorted(action_types.items()):
        marker = "  <-- ЛИДЫ?" if any(k in atype for k in ["lead", "whatsapp", "contact", "message", "click_to"]) else ""
        print(f"             {val:5d}  {atype}{marker}")
        if atype not in action_map:
            action_map[atype] = []
        action_map[atype].append(name[:40])

print("\n" + "=" * 90)
print("\nВСЕ action_type в кабинете (уникальные):")
for atype in sorted(action_map.keys()):
    camps_count = len(action_map[atype])
    marker = "  <-- ВЕРОЯТНО ЛИДЫ" if any(k in atype for k in ["lead", "whatsapp", "contact", "message", "click_to"]) else ""
    print(f"  {atype}{marker}  ({camps_count} кампаний)")
