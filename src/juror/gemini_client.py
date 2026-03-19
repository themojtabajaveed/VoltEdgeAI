from google import genai
import json

def classify_announcement(text: str) -> dict:
    """
    Classifies a stock announcement as Positive, Negative, or Neutral.
    Returns a dictionary with keys: label, confidence (0-1), and reason.
    """
    client = genai.Client()

    prompt = (
        f"Classify this as Positive, Negative, or Neutral for the stock price today: '{text}' "
        "Reply in JSON with keys: label, confidence (0-1), and reason."
    )

    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=prompt
    )

    # Assuming the response text is valid JSON, or wrapped in a json code block
    raw_text = response.text.strip()
    
    # Strip markdown formatting if present
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        # Fallback if the model didn't return perfect JSON
        return {
            "label": "Neutral", 
            "confidence": 0.0, 
            "reason": f"Failed to parse JSON response: {raw_text}"
        }
