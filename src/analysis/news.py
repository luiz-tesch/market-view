"""
Busca notícias relevantes via NewsAPI
"""
import requests
from src import config


def fetch_news(query: str, max_articles: int = 5) -> list[dict]:
    """
    Busca artigos recentes sobre o tema da pergunta do mercado.
    Retorna lista de {title, description, url, publishedAt}
    """
    if not config.NEWS_API_KEY:
        return []  # NewsAPI é opcional

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": max_articles,
        "apiKey": config.NEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {
                "title": a["title"],
                "description": a.get("description", ""),
                "url": a["url"],
                "published": a["publishedAt"],
            }
            for a in articles
        ]
    except Exception as e:
        print(f"⚠️  NewsAPI erro: {e}")
        return []


def format_news_for_prompt(articles: list[dict]) -> str:
    """Formata artigos em texto para incluir no prompt do Claude."""
    if not articles:
        return "Nenhuma notícia recente encontrada."
    lines = []
    for a in articles:
        lines.append(f"- [{a['published'][:10]}] {a['title']}: {a['description']}")
    return "\n".join(lines)