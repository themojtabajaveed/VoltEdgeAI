from src.juror.gemini_client import classify_announcement

def main():
    announcement = "ABC Ltd wins a 500 crore order from Government of India."
    result = classify_announcement(announcement)
    print(result)

if __name__ == "__main__":
    main()
