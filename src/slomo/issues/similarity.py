"""Near-match detection between issues. Suggestion-only — never auto-merges."""

from __future__ import annotations

from difflib import SequenceMatcher

from slomo.issues.fingerprint import normalize_frames, normalize_message
from slomo.issues.index import IssueIndex
from slomo.issues.models import Issue

THRESHOLD = 0.82


def similarity(a_frames: list[str], b_frames: list[str], a_msg: str, b_msg: str) -> float:
    frame_score = SequenceMatcher(None, a_frames, b_frames).ratio()
    msg_score = SequenceMatcher(None, a_msg, b_msg).ratio()
    return 0.7 * frame_score + 0.3 * msg_score


def similar_issues(
    issue: Issue,
    index: IssueIndex,
    threshold: float = THRESHOLD,
    limit: int = 5,
) -> list[tuple[Issue, float]]:
    """Issues of the same exception type whose stack/message shape is close."""
    incidents = index.incidents_for_issue(issue.id, limit=1)
    if not incidents:
        return []
    ref = incidents[0]
    ref_frames = normalize_frames(ref.frames)
    ref_msg = normalize_message(ref.message)

    scored: list[tuple[Issue, float]] = []
    for other in index.list_issues():
        if other.fingerprint == issue.fingerprint or other.exc_type != issue.exc_type:
            continue
        other_incidents = index.incidents_for_issue(other.id, limit=1)
        if not other_incidents:
            continue
        score = similarity(
            ref_frames,
            normalize_frames(other_incidents[0].frames),
            ref_msg,
            normalize_message(other_incidents[0].message),
        )
        if score >= threshold:
            scored.append((other, score))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]
