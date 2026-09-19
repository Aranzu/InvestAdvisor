"""
Módulo de analítica financiera mensual y scanner de oportunidades.
"""
from .monthly_analytics import get_monthly_performance_report
from .opportunities import scan_opportunities, load_candidates_watchlist

__all__ = [
    "get_monthly_performance_report",
    "scan_opportunities",
    "load_candidates_watchlist",
]
