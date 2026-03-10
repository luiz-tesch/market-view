"""
Usa Groq (LLaMA 3.3 70B — gratuito em console.groq.com) para estimar
probabilidade de um mercado e comparar com o preço atual (identificar edge)
"""
import json
from groq import Groq
from src import config
from src.analysis.news import fetch_news, format_news_for_prompt


client = Groq(api_key=config.GROQ_API_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"  # gratuito, excelente para análise


def analyze_market(market: dict, current_price: float) -> dict:
    """
    Analisa um mercado e retorna:
    - estimated_prob: probabilidade estimada pelo modelo (0-1)
    - edge: diferença entre estimativa e preço atual
    - reasoning: explicação
    - recommendation: 'BUY', 'SKIP', ou 'SELL'
    """
    question = market.get("question", "")
    end_date = market.get("end_date_iso", "desconhecida")

    # Busca notícias relevantes
    articles = fetch_news(query=question)
    news_text = format_news_for_prompt(articles)

    prompt = f"""You are a prediction market analyst. Analyze this market and estimate the real probability of the event occurring.

MARKET: {question}
RESOLUTION DATE: {end_date}
CURRENT PRICE (market implied probability): {current_price:.1%}

RECENT RELATED NEWS:
{news_text}

Respond ONLY with valid JSON, no extra text or markdown:
{{
  "estimated_prob": <number between 0 and 1>,
  "confidence": <"low"|"medium"|"high">,
  "reasoning": "<2-3 sentence explanation>",
  "key_factors": ["<factor1>", "<factor2>"]
}}"""

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.2,
        response_format={"type": "json_object"},  # força JSON puro
    )

    raw = response.choices[0].message.content.strip()
    analysis = json.loads(raw)

    estimated_prob = float(analysis["estimated_prob"])
    edge = estimated_prob - current_price

    # Recomendação baseada no edge e confiança
    confidence = analysis.get("confidence", "low")
    min_edge = config.MIN_EDGE
    if confidence == "low":
        min_edge *= 2  # exige edge maior quando confiança é baixa

    if edge >= min_edge:
        recommendation = "BUY"
    elif edge <= -min_edge:
        recommendation = "SELL"
    else:
        recommendation = "SKIP"

    return {
        "question": question,
        "current_price": current_price,
        "estimated_prob": estimated_prob,
        "edge": edge,
        "confidence": confidence,
        "recommendation": recommendation,
        "reasoning": analysis.get("reasoning", ""),
        "key_factors": analysis.get("key_factors", []),
    }