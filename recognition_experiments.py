"""Experiment flag helpers for recognition A/B runs.

Flags are intentionally inert until a recognizer path checks them. This lets
benchmark scripts record comparable metadata before individual optimizations
are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


KNOWN_FLAGS = {
    "temporal_candidate_cache",
    "stable_state_scoring",
    "fsm_event_constraints",
    "cell_temporal_voting",
    "single_frame_shadow_refine",
    "sequence_repair_clear_v1",
    "sequence_repair_shape_target_v1",
    "sequence_candidate_gap_fill_v1",
    "sequence_candidate_rerank_v1",
}


@dataclass(frozen=True)
class ExperimentFlags:
    enabled: frozenset[str]

    def is_enabled(self, name: str) -> bool:
        return name in self.enabled

    def as_list(self) -> list[str]:
        return sorted(self.enabled)

    def as_metadata(self) -> dict:
        return {
            "enabled": self.as_list(),
            "known": sorted(KNOWN_FLAGS),
        }


def parse_flags(values: Iterable[str] | str | None) -> ExperimentFlags:
    if isinstance(values, ExperimentFlags):
        return values
    if values is None:
        return ExperimentFlags(frozenset())
    if isinstance(values, str):
        raw_values = values.replace(";", ",").split(",")
    else:
        raw_values = []
        for value in values:
            raw_values.extend(str(value).replace(";", ",").split(","))
    flags = {item.strip() for item in raw_values if item and item.strip()}
    unknown = sorted(flags - KNOWN_FLAGS)
    if unknown:
        raise ValueError(f"Unknown experiment flag(s): {', '.join(unknown)}")
    return ExperimentFlags(frozenset(flags))
