"""Standalone debug script to inspect YTMusic search ranking + improved confidence scoring.

This script:
- Fetches Spotify track metadata by track id (name, artists, duration_ms)
- Uses that metadata to build a YTMusic search query (or accepts --query override)
- Fetches YTMusic song results
- Scores EVERY result using a multi-signal scoring model:
  - title similarity
  - artist similarity (matches ANY Spotify artist, not just the first)
  - duration similarity (Spotify duration_ms vs YT duration)
  - rank prior (trust YTMusic ordering as a prior)
  - small heuristic nudges (live/cover/remix)

Usage:
  python debug_ytmusic_scoring.py --track-id 47qRVFSWe652gcUjco279e --limit 25
  python debug_ytmusic_scoring.py --track-id 47qRVFSWe652gcUjco279e --query "Jane Zhang, 王赫野 是你 - Live" --limit 25

Notes:
- Requires Spotify credentials in environment (same as backend):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
- Prints per-component scores so you can see *why* something ranked the way it did.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()
from ytmusicapi import YTMusic


# Best-effort: force UTF-8 output (Windows consoles often default to cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


CONFIDENCE_THRESHOLD = 0.65


def calculate_similarity(str1: str, str2: str) -> float:
    str1 = (str1 or "").lower().strip()
    str2 = (str2 or "").lower().strip()
    return SequenceMatcher(None, str1, str2).ratio()


def normalize_text(s: str) -> str:
    s = (s or "").lower()

    # unify separators
    s = s.replace("–", " ").replace("—", " ").replace("-", " ").replace(":", " ")

    # remove common meta tokens
    meta_tokens = [
        "official audio",
        "official video",
        "official music video",
        "lyrics",
        "lyric video",
        "audio",
        "mv",
        "hd",
        "4k",
        "official",
        "music video",
    ]
    for t in meta_tokens:
        s = s.replace(t, " ")

    # remove bracketed meta (best-effort)
    s = re.sub(r"\((official|mv|music video|lyrics|lyric video|audio|hd|4k)[^)]*\)", " ", s)
    s = re.sub(r"\[(official|mv|music video|lyrics|lyric video|audio|hd|4k)[^\]]*\]", " ", s)

    # normalize feat tokens
    s = re.sub(r"\b(feat\.|feat|ft\.|ft)\b", "feat", s)

    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: str) -> List[str]:
    s = normalize_text(s)
    # split on spaces; keep CJK chunks as-is
    parts = [p for p in re.split(r"\s+", s) if p]
    return parts


def title_score(spotify_title: str, yt_title: str) -> float:
    a = normalize_text(spotify_title)
    b = normalize_text(yt_title)

    # base similarity
    sim = calculate_similarity(a, b)

    # containment bonus: if all meaningful tokens appear
    ts = [t for t in tokens(a) if len(t) >= 2 and t not in {"feat"}]
    if ts:
        hits = sum(1 for t in ts if t in b)
        contain = hits / len(ts)
        sim = max(sim, 0.55 * sim + 0.45 * contain)

    # if spotify title appears as substring, floor it
    if a and a in b:
        sim = max(sim, 0.85)

    return max(0.0, min(sim, 1.0))


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


def artist_score(spotify_artists: List[str], yt_artists_text: str, yt_title: str) -> Tuple[float, int, List[Tuple[str, float]]]:
    """Return (score, matched_count, per_artist_sims)."""
    yt_blob = normalize_text(yt_artists_text) + " " + normalize_text(yt_title)

    per: List[Tuple[str, float]] = []
    for a in spotify_artists:
        sim = calculate_similarity(normalize_text(a), yt_blob)
        # substring check helps for short names
        if normalize_text(a) and normalize_text(a) in yt_blob:
            sim = max(sim, 0.95)
        per.append((a, max(0.0, min(sim, 1.0))))

    if not per:
        return 0.0, 0, []

    per_sorted = sorted(per, key=lambda x: x[1], reverse=True)
    best = per_sorted[0][1]

    matched = sum(1 for _, s in per if s >= 0.75)

    # multi-artist bonus: if multiple artists match well
    bonus = 0.0
    if matched >= 2:
        bonus = 0.08
    elif matched == 1:
        bonus = 0.02

    return max(0.0, min(best + bonus, 1.0)), matched, per_sorted


def parse_duration_to_seconds(duration_str: str) -> Optional[int]:
    if not duration_str:
        return None
    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return None
    return None


def duration_score(spotify_duration_ms: Optional[int], yt_duration_str: str) -> float:
    if not spotify_duration_ms:
        return 0.5  # neutral if unknown

    yt_sec = parse_duration_to_seconds(yt_duration_str)
    if yt_sec is None:
        return 0.5

    sp_sec = max(1.0, spotify_duration_ms / 1000.0)
    delta = abs(sp_sec - float(yt_sec))

    if delta <= 5:
        return 1.0
    if delta <= 15:
        return 0.85
    if delta <= 30:
        return 0.65
    if delta <= 60:
        return 0.35
    return 0.0


def rank_prior(rank: int, strength: float) -> float:
    """Return [0..1] prior from rank (1 is best). strength controls how much we trust YT ordering."""
    r = max(1, rank)
    # exponential decay; rank 1 ~ 1.0, rank grows -> prior approaches 0
    return math.exp(-(r - 1) / max(1e-6, strength))


def heuristic_adjustment(spotify_title: str, yt_title: str) -> float:
    sp = normalize_text(spotify_title)
    yt = normalize_text(yt_title)

    adj = 0.0

    # live hint
    sp_live = any(k in sp for k in ["live", "现场", "現場"])
    yt_live = any(k in yt for k in ["live", "现场", "現場"])
    if sp_live and yt_live:
        adj += 0.05

    # cover penalty if spotify doesn't suggest it
    if ("cover" in yt or "翻唱" in yt) and ("cover" not in sp and "翻唱" not in sp):
        adj -= 0.12

    # remix penalty
    if "remix" in yt and "remix" not in sp:
        adj -= 0.10

    return adj


@dataclass
class ScoredCandidate:
    idx: int
    video_id: str
    title: str
    channel: str
    duration: str
    url: str

    final: float
    title_s: float
    artist_s: float
    duration_s: float
    rank_s: float
    heur: float

    matched_artists: int
    artist_sims: List[Tuple[str, float]]


def ytmusic_search(query: str, limit: int) -> List[Dict[str, Any]]:
    ytmusic = YTMusic()
    return ytmusic.search(query, filter="songs", limit=limit)


def fetch_spotify_track(track_id: str) -> Dict[str, Any]:
    """Minimal Spotify track fetch using client-credentials flow."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
    except Exception as e:
        raise RuntimeError(
            "Missing spotify dependencies. Install backend requirements (spotipy) first."
        ) from e

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET not set in environment")

    cc = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(client_credentials_manager=cc)
    t = sp.track(track_id)

    return {
        "id": t["id"],
        "name": t["name"],
        "artists": [a["name"] for a in t["artists"]],
        "artist": ", ".join([a["name"] for a in t["artists"]]),
        "duration_ms": t.get("duration_ms"),
        "external_url": t.get("external_urls", {}).get("spotify"),
    }


def score_results(
    results: List[Dict[str, Any]],
    spotify_name: str,
    spotify_artists: List[str],
    spotify_duration_ms: Optional[int],
    rank_strength: float,
) -> List[ScoredCandidate]:
    scored: List[ScoredCandidate] = []

    for i, res in enumerate(results, start=1):
        video_id = res.get("videoId") or ""
        if not video_id:
            continue

        title = res.get("title", "") or ""
        channel = normalize_artists_list(res.get("artists"))
        duration = res.get("duration", "") or ""

        t_s = title_score(spotify_name, title)
        a_s, matched, a_sims = artist_score(spotify_artists, channel, title)
        d_s = duration_score(spotify_duration_ms, duration)
        r_s = rank_prior(i, rank_strength)
        heur = heuristic_adjustment(spotify_name, title)

        # Combine. Rank gets a bigger weight per your request.
        # Rationale: still bounded by title/duration so it can't pick wildly wrong items.
        final = (0.45 * t_s) + (0.25 * a_s) + (0.20 * d_s) + (0.10 * r_s) + heur
        final = max(0.0, min(final, 1.0))

        scored.append(
            ScoredCandidate(
                idx=i,
                video_id=video_id,
                title=title,
                channel=channel,
                duration=duration,
                url=f"https://music.youtube.com/watch?v={video_id}",
                final=round(final, 3),
                title_s=round(t_s, 3),
                artist_s=round(a_s, 3),
                duration_s=round(d_s, 3),
                rank_s=round(r_s, 3),
                heur=round(heur, 3),
                matched_artists=matched,
                artist_sims=[(n, round(s, 3)) for n, s in a_sims],
            )
        )

    scored.sort(key=lambda c: c.final, reverse=True)
    return scored


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug YTMusic search scoring (improved model)")
    parser.add_argument("--track-id", required=True, help="Spotify track id (e.g. 47qRVFSWe652gcUjco279e)")
    parser.add_argument(
        "--query",
        required=False,
        help="Optional YTMusic search query override. If omitted, uses '<artist> <track>'.",
    )
    parser.add_argument("--limit", type=int, default=25, help="Number of YTMusic results to fetch")
    parser.add_argument(
        "--rank-strength",
        type=float,
        default=6.0,
        help="How strongly to trust YTMusic rank (higher = trust rank deeper).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also print the raw YTMusic search payload for each result (verbose)",
    )

    args = parser.parse_args()

    sp_track = fetch_spotify_track(args.track_id)
    spotify_name = sp_track["name"]
    spotify_artists = sp_track["artists"]
    spotify_duration_ms = sp_track.get("duration_ms")

    query = args.query or f"{sp_track['artist']} {spotify_name}"

    print(f"Spotify track id: {sp_track['id']}")
    print(f"Spotify: name='{spotify_name}'")
    print(f"Spotify: artists={spotify_artists}")
    print(f"Spotify: duration_ms={spotify_duration_ms}")
    print(f"Spotify url: {sp_track.get('external_url')}")
    print()
    print(f"YTMusic query: {query}")
    print(f"Threshold: {CONFIDENCE_THRESHOLD}")
    print(f"Rank strength: {args.rank_strength} (bigger = more trust in YT ordering)")
    print(f"Fetching top {args.limit} YTMusic song results...\n")

    results = ytmusic_search(query, args.limit)

    if args.raw:
        print("=== RAW RESULTS ===")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print("=== END RAW RESULTS ===\n")

    scored = score_results(results, spotify_name, spotify_artists, spotify_duration_ms, args.rank_strength)

    if not scored:
        print("No scored results (no videoId entries).")
        return 2

    header = (
        "idx\tfinal\tT\tA\tD\tR\tH\tdur\ttitle\t|\tchannel\t|\turl"
    )
    print(header)
    for c in scored:
        flag = "" if c.final >= CONFIDENCE_THRESHOLD else "*"
        print(
            f"{c.idx}\t{c.final:.3f}{flag}\t{c.title_s:.3f}\t{c.artist_s:.3f}\t{c.duration_s:.3f}\t{c.rank_s:.3f}\t{c.heur:+.3f}\t{c.duration}\t{c.title}\t|\t{c.channel}\t|\t{c.url}"
        )

    best = scored[0]
    print("\nBest:")
    print(
        f"  final={best.final:.3f} (>= threshold? {best.final >= CONFIDENCE_THRESHOLD})\n"
        f"  title_s={best.title_s:.3f} artist_s={best.artist_s:.3f} duration_s={best.duration_s:.3f} rank_s={best.rank_s:.3f} heur={best.heur:+.3f}\n"
        f"  title={best.title}\n"
        f"  channel={best.channel}\n"
        f"  url={best.url}\n"
        f"  artist_sims(top)={best.artist_sims[:4]}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
