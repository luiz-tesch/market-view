"""
Usa Google Gemini 2.0 Flash para estimar probabilidade de um mercado.
Substituiu: Groq LLaMA 3.3 70B
"""
from __future__ import annotations
import json, re, requests
from src import config
from src.analysis.news import fetch_news, format_news_for_prompt

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{config.GEMINI_MODEL}:generateContent"
)

def _call_gemini(prompt: str) -> str:
    if not config.GEMINI_API_KEY:
        raise EnvironmentError(
            "GEMINI_API_KEY não configurada. Obtenha em https://aistudio.google.com/app/apikey"
        )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        GEMINI_URL,
        params={"key": config.GEMINI_API_KEY},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise ValueError("Gemini retornou resposta vazia")
    return candidates[0]["content"]["parts"][0]["text"].strip()

def _parse_json_safe(raw: str) -> dict:
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    return json.loads(raw)

def analyze_market(market: dict, current_price: float) -> dict:
    question = market.get("question", "")
    end_date = market.get("end_date_iso") or market.get("endDateIso", "desconhecida")
    articles  = fetch_news(query=question)
    news_text = format_news_for_prompt(articles)

    prompt = f"""You are a prediction market analyst. Estimate the real probability of the event below.

MARKET: {question}
RESOLUTION DATE: {end_date}
CURRENT MARKET PRICE (implied probability): {current_price:.1%}

RECENT RELATED NEWS:
{news_text}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "estimated_prob": <number 0-1>,
  "confidence": <"low"|"medium"|"high">,
  "reasoning": "<2-3 sentence explanation>",
  "key_factors": ["<factor1>", "<factor2>"]
}}"""

    raw      = _call_gemini(prompt)
    analysis = _parse_json_safe(raw)

    estimated_prob = float(analysis["estimated_prob"])
    edge           = estimated_prob - current_price
    confidence     = analysis.get("confidence", "low")

    min_edge = config.MIN_EDGE * (2 if confidence == "low" else 1)

    if edge >= min_edge:
        recommendation = "BUY"
    elif edge <= -min_edge:
        recommendation = "SELL"
    else:
        recommendation = "SKIP"

    return {
        "question":       question,
        "current_price":  current_price,
        "estimated_prob": estimated_prob,
        "edge":           edge,
        "confidence":     confidence,
        "recommendation": recommendation,
        "reasoning":      analysis.get("reasoning", ""),
        "key_factors":    analysis.get("key_factors", []),
    }