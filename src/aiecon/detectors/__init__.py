"""Waste detectors (PLAN.md §9). Each detector emits evidence-backed ``Finding`` records."""

from aiecon.detectors.common import DetectorInputs, load_inputs
from aiecon.detectors.fallback_waste import detect_unused_fallbacks
from aiecon.detectors.repeated_context import detect_repeated_context
from aiecon.detectors.retry_waste import detect_discarded_attempts, detect_failed_runs
from aiecon.detectors.summary import FindingsSummary, summarize

__all__ = [
    "DetectorInputs",
    "FindingsSummary",
    "detect_discarded_attempts",
    "detect_failed_runs",
    "detect_repeated_context",
    "detect_unused_fallbacks",
    "load_inputs",
    "summarize",
]
