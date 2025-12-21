"""Standalone debug script to reproduce this project's YTMusic search + confidence scoring.

Usage:
  python debug_ytmusic_scoring.py --query "Jane Zhang, 王赫野 是你 - Live" --track "是你 - Live" --artist "Jane Zhang, 王赫野" --limit 25

Notes:
- This script intentionally mirrors the scoring logic in backend/services/youtube.py.
- It prints the entire scored list (not just the top 3), sorted by score desc.
- On Windows, the default console encoding can be cp1252, which can't print CJK.
  We reconfigure stdout/stderr to UTF-8 to avoid UnicodeEncodeError.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List

from ytmusicapi import YTMusic


# Best-effort: force UTF-8 output (Windows consoles often default to cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


CONFIDENCE_THRESHOLD = 0.65  # must match backend/services/youtube.py


def calculate_similarity(str1: str, str2: str) -> float:
    """Calculate similarity between two strings using SequenceMatcher (similar to Levenshtein)."""
    str1 = (str1 or "").lower().strip()
    str2 = (str2 or "").lower().strip()
    return SequenceMatcher(None, str1, str2).ratio()


def calculate_match_score(youtube_title: str, youtube_channel: str, track_name: str, artist: str) -> float:
    """Calculate overall match score between YTMusic result and the intended track."""
    yt_title = (youtube_title or "").lower()
    yt_channel = (youtube_channel or "").lower()
    track = (track_name or "").lower()
    art = (artist or "").lower()

    # Remove common suffixes from YouTube titles
    for suffix in [
        "official audio",
        "official video",
        "official music video",
        "lyrics",
        "lyric video",
        "audio",
        "hd",
        "4k",
        "official",
        "(official)",
        "[official]",
        "music video",
    ]:
        yt_title = yt_title.replace(suffix, "").strip()

    artist_parts = [a.strip() for a in art.split(",")]
    main_artist = artist_parts[0] if artist_parts else art

    # Critical: does the main artist appear in title or channel?
    artist_in_title = main_artist in yt_title
    artist_in_channel = main_artist in yt_channel
    artist_found = artist_in_title or artist_in_channel

    title_sim = calculate_similarity(yt_title, track)

    track_in_title = track in yt_title
    if track_in_title:
        title_sim = max(title_sim, 0.8)

    artist_sim = max(
        calculate_similarity(yt_channel, main_artist),
        calculate_similarity(yt_title, main_artist),
    )

    if not artist_found and artist_sim < 0.5:
        score = title_sim * 0.4
    else:
        score = (title_sim * 0.5) + (artist_sim * 0.35) + (0.15 if artist_found else 0)

    return min(score, 1.0)


def normalize_artists_list(artists_list: Any) -> str:
    if not artists_list:
        return ""
    if isinstance(artists_list, list):
        names: List[str] = []
        for a in artists_list:
            if isinstance(a, dict):
                names.append(a.get("name", "") or "")
            else:
                names.append(str(a))
        return ", ".join([n for n in names if n])
    return str(artists_list)


@dataclass
class ScoredCandidate:
    idx: int
    video_id: str
    title: str
    channel: str
    duration: str
    score: float
    url: str


def ytmusic_search(query: str, limit: int) -> List[Dict[str, Any]]:
    ytmusic = YTMusic()
    return ytmusic.search(query, filter="songs", limit=limit)


def score_results(results: List[Dict[str, Any]], track_name: str, artist: str) -> List[ScoredCandidate]:
    scored: List[ScoredCandidate] = []
    for i, res in enumerate(results, start=1):
        video_id = res.get("videoId") or ""
        if not video_id:
            continue

        title = res.get("title", "") or ""
        channel = normalize_artists_list(res.get("artists"))
        duration = res.get("duration", "") or ""

        score = calculate_match_score(title, channel, track_name, artist)
        scored.append(
            ScoredCandidate(
                idx=i,
                video_id=video_id,
                title=title,
                channel=channel,
                duration=duration,
                score=round(score, 3),
                url=f"https://music.youtube.com/watch?v={video_id}",
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug YTMusic search scoring")
    parser.add_argument("--query", required=True, help="The YTMusic search query")
    parser.add_argument("--track", required=True, help="The intended track name used for scoring")
    parser.add_argument("--artist", required=True, help="The intended artist string used for scoring")
    parser.add_argument("--limit", type=int, default=25, help="Number of YTMusic results to fetch")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also print the raw YTMusic search payload for each result (verbose)",
    )

    args = parser.parse_args()

    print(f"Query: {args.query}")
    print(f"Scoring against: track='{args.track}' artist='{args.artist}'")
    print(f"Threshold: {CONFIDENCE_THRESHOLD}")
    print(f"Fetching top {args.limit} YTMusic song results...\n")

    results = ytmusic_search(args.query, args.limit)

    if args.raw:
        print("=== RAW RESULTS ===")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print("=== END RAW RESULTS ===\n")

    scored = score_results(results, args.track, args.artist)

    if not scored:
        print("No scored results (no videoId entries).")
        return 2

    print("idx\tscore\tduration\ttitle\t|\tchannel\t|\turl")
    for c in scored:
        flag = "" if c.score >= CONFIDENCE_THRESHOLD else "*"
        print(f"{c.idx}\t{c.score:.3f}{flag}\t{c.duration}\t{c.title}\t|\t{c.channel}\t|\t{c.url}")

    best = scored[0]
    print("\nBest:")
    print(
        f"  score={best.score:.3f} (>= threshold? {best.score >= CONFIDENCE_THRESHOLD})\n"
        f"  title={best.title}\n"
        f"  channel={best.channel}\n"
        f"  url={best.url}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

