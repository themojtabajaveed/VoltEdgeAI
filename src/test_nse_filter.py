from sources.nse_announcements import fetch_latest_announcements

def main():
    print("Testing NSE announcement filter...")
    anns = fetch_latest_announcements(limit=10)
    
    print(f"Total returned: {len(anns)}")
    
    for a in anns:
        text = a["text"] if a["text"] else "No text"
        print(f"{a['symbol']}: {text[:100]}...")

if __name__ == "__main__":
    main()
