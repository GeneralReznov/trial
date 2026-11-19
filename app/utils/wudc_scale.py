"""Compact WUDC speaker-scale helpers used by live debate reports.

The previous application evaluated debates on the 50-100 WUDC speaker scale.
Keeping the scale in a small, dependency-free module lets the current
FastAPI experience expose the same meaningful tournament context while its
regular session scoring remains on the app's 0-100 report scale.
"""

from __future__ import annotations

from typing import Any


WUDC_SCALE: dict[str, Any] = {
    "min": 50,
    "max": 100,
    "description": "World Universities Debating Championship speaker scale",
    "tiers": [
        {
            "key": "elite",
            "min": 89,
            "max": 100,
            "name": "Elite championship level",
            "description": "Brilliant arguments, exceptional engagement and virtually unassailable comparative analysis.",
        },
        {
            "key": "competitive",
            "min": 76,
            "max": 88,
            "name": "Tournament ready",
            "description": "Strong arguments, good strategy and a competitive performance.",
        },
        {
            "key": "developing",
            "min": 60,
            "max": 75,
            "name": "Developing skills",
            "description": "Core skills are present and will improve with more refinement and practice.",
        },
        {
            "key": "needs_work",
            "min": 50,
            "max": 59,
            "name": "Needs development",
            "description": "Build clearer arguments, evidence and strategic awareness before competition.",
        },
    ],
    "criteria": [
        {
            "key": "argument_quality",
            "name": "Argument quality",
            "description": "Clarity, relevance and logical structure of arguments.",
        },
        {
            "key": "evidence_use",
            "name": "Evidence use",
            "description": "Specificity, credibility and integration of examples or data.",
        },
        {
            "key": "reasoning",
            "name": "Reasoning",
            "description": "Causal links, impact analysis and conclusions that follow from premises.",
        },
        {
            "key": "strategic_positioning",
            "name": "Strategic positioning",
            "description": "Side-appropriate framing, clash engagement and prioritisation.",
        },
    ],
}


def wudc_tier(score: float) -> dict[str, Any]:
    """Return the WUDC tier for a 50-100 speaker score."""
    value = max(WUDC_SCALE["min"], min(WUDC_SCALE["max"], int(round(score))))
    for tier in WUDC_SCALE["tiers"]:
        if tier["min"] <= value <= tier["max"]:
            return {**tier, "score": value}
    return {**WUDC_SCALE["tiers"][-1], "score": value}


def wudc_score(overall_score: float) -> int:
    """Map the app's 0-100 debate report score to the speaker scale."""
    return max(50, min(100, int(round(50 + float(overall_score) / 2))))


__all__ = ["WUDC_SCALE", "wudc_score", "wudc_tier"]