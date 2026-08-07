import os
import requests

def main():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("Error: META_ACCESS_TOKEN is not set.")
        return

    print("Fetching ad accounts from Meta Graph API...")
    url = f"https://graph.facebook.com/v19.0/me/adaccounts?fields=name,account_id&limit=100&access_token={token}"
    
    response = requests.get(url)
    data = response.json()
    
    if "data" in data:
        accounts = data["data"]
        print(f"\nFound {len(accounts)} ad accounts:\n")
        print("-" * 50)
        for acc in accounts:
            name = acc.get("name", "Unknown Name")
            act_id = acc.get("account_id", "Unknown ID")
            print(f"{name}  ->  act_{act_id}")
        print("-" * 50)
        
        # Paging handle if more than 100
        while "paging" in data and "next" in data["paging"]:
            response = requests.get(data["paging"]["next"])
            data = response.json()
            if "data" in data:
                for acc in data["data"]:
                    name = acc.get("name", "Unknown Name")
                    act_id = acc.get("account_id", "Unknown ID")
                    print(f"{name}  ->  act_{act_id}")
    else:
        print("Failed to fetch ad accounts or no accounts found.")
        print("Response:", data)

if __name__ == "__main__":
    main()
