import json
from src.sources.nse_announcements import fetch_latest_announcements

def main():
    print("Fetching announcements...")
    anns = fetch_latest_announcements(limit=5)
    
    print(f"Fetched {len(anns)} announcements.")
    
    if anns:
        print("\nFirst announcement:")
        # Pretty-print the dictionary for readability
        print(json.dumps(anns[0], indent=2))

if __name__ == "__main__":
    main()
