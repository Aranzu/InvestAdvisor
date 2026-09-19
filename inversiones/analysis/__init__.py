"""
Motor de Análisis y Ponderación Cuantitativa y Cualitativa.
"""
from .scoring import calculate_composite_scores
from .recommendation import generate_recommendation
from .llm_explainer import generate_detailed_explanation

__all__ = [
    "calculate_composite_scores",
    "generate_recommendation",
    "generate_detailed_explanation",
]
