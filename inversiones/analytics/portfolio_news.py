"""Portfolio-level news feed built from the existing per-ticker news collector."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from data_fetchers.news import get_position_news
from data_loader import load_user_positions_from_folder


def get_portfolio_news(
    positions: Optional[List[Dict[str, Any]]] = None,
    max_workers: int = 6,
    max_articles_per_ticker: int = 3,
) -> Dict[str, Any]:
    """Collect recent, de-duplicated news for every current holding.

    This is informational only: it does not affect portfolio recommendations.
    """
    positions = positions if positions is not None else load_user_positions_from_folder()
    unique_positions = {}
    for position in positions:
        ticker = str(position.get("ticker") or "").strip().upper()
        if ticker:
            unique_positions[ticker] = position

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(unique_positions)))) as executor:
        futures = {
            executor.submit(get_position_news, ticker, str(pos.get("name") or "")): ticker
            for ticker, pos in unique_positions.items()
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                data = future.result()
            except Exception:
                continue
            for article in data.get("articles", [])[:max_articles_per_ticker]:
                title = str(article.get("title") or "").strip()
                if not title:
                    continue
                results.append({
                    "ticker": ticker,
                    "title": title,
                    "link": article.get("link") or "",
                    "publisher": article.get("publisher") or "Fuente financiera",
                    "published": article.get("published") or "Reciente",
                    "sentiment": article.get("sentiment_tag") or "NEUTRAL",
                    "sentiment_score": article.get("sentiment_score", 0.0),
                })

    deduped: List[Dict[str, Any]] = []
    seen_titles = set()
    for article in results:
        key = article["title"].casefold()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(article)

    sentiment_counts = {"POSITIVO": 0, "NEUTRAL": 0, "NEGATIVO": 0}
    for article in deduped:
        label = article["sentiment"]
        sentiment_counts[label if label in sentiment_counts else "NEUTRAL"] += 1

    return {
        "articles": deduped,
        "portfolio_tickers": sorted(unique_positions),
        "covered_tickers": sorted({article["ticker"] for article in deduped}),
        "sentiment_counts": sentiment_counts,
        "data_sources": ["Yahoo Finance", "Google News RSS"],
        "disclaimer": "Titulares y sentimiento informativos; no son asesoría financiera ni modifican recomendaciones automáticamente.",
    }
