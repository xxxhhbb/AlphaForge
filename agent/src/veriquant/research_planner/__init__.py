"""Deterministic research-intent planning without eager package cycles."""

from .models_v2 import IntentResolution, ResearchIntentV2
from .semantic import DeterministicResearchIntentCompiler


def __getattr__(name: str):
    if name in {
        "DeterministicResearchQuestionPlanner",
        "QuestionAssessmentRequest",
        "QuestionAssessmentResult",
    }:
        from .service import (
            DeterministicResearchQuestionPlanner,
            QuestionAssessmentRequest,
            QuestionAssessmentResult,
        )

        return {
            "DeterministicResearchQuestionPlanner": DeterministicResearchQuestionPlanner,
            "QuestionAssessmentRequest": QuestionAssessmentRequest,
            "QuestionAssessmentResult": QuestionAssessmentResult,
        }[name]
    raise AttributeError(name)


__all__ = [
    "DeterministicResearchIntentCompiler",
    "DeterministicResearchQuestionPlanner",
    "IntentResolution",
    "QuestionAssessmentRequest",
    "QuestionAssessmentResult",
    "ResearchIntentV2",
]
