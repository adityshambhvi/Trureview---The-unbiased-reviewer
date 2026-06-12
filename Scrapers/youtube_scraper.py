"""
TruReview — YouTube Scraper
-----------------------------
Fetches comments from YouTube product review videos.
No API key needed. No Google Cloud. No card.

Libraries:
    youtube-comment-downloader  -> scrapes comments from video URLs
    youtube-search-python       -> searches YouTube for review videos

Install (from project root):
    pip install -r requirements.txt

Note:
    youtube-search-python requires httpx<0.28. Newer httpx removes the
    `proxies` argument and breaks search until the library is updated.
"""

import json
import re
import sys
import time
import itertools
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from youtubesearchpython import CustomSearch, VideoDurationFilter
from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR


# Cache lives at project_root/data/cache regardless of where the script is run from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

MIN_COMMENT_BODY_LEN = 15
MIN_MEANINGFUL_TEXT_LEN = 10
COMMENT_FETCH_MULTIPLIER = 8
COMMENT_FETCH_MIN_RAW = 100


# --- Data Models ----------------------------------------------------------------

@dataclass
class YouTubeVideo:
    video_id: str
    title: str
    channel: str
    url: str
    view_count: str
    duration: str
    published: str


@dataclass
class YouTubeComment:
    id: str
    video_id: str
    video_title: str
    video_channel: str
    body: str
    likes: int
    author: str
    is_reply: bool
    published: str
    time_parsed: Optional[float]
    source: str = "youtube"


# --- Helpers --------------------------------------------------------------------

def _log(message: str) -> None:
    """Print safely on Windows consoles that lack UTF-8 support."""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="replace").decode("ascii"))


def _parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in version.split(".")[:3]:
        try:
            parts.append(int(segment.split("-")[0]))
        except ValueError:
            break
    return tuple(parts)


def _check_httpx_compat() -> None:
    try:
        import httpx
    except ImportError:
        return

    if _parse_version(httpx.__version__) >= (0, 28, 0):
        _log(
            "[YouTube] WARNING: httpx>=0.28 breaks youtube-search-python. "
            "Install a compatible version: pip install 'httpx>=0.27,<0.28'"
        )


def parse_likes(raw: str) -> int:
    """Convert YouTube like strings to int (0, 1,234, 1.2K, 10K, 1.1M)."""
    if not raw:
        return 0
    raw = str(raw).strip().replace(",", "")
    try:
        upper = raw.upper()
        if upper.endswith("K"):
            return int(float(raw[:-1]) * 1_000)
        if upper.endswith("M"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except (ValueError, TypeError):
        return 0


def is_short(duration: str) -> bool:
    """True if video is <= 60 seconds (YouTube Short)."""
    if not duration:
        return False
    parts = duration.split(":")
    if len(parts) == 1:
        return True
    if len(parts) == 2:
        try:
            return int(parts[0]) == 0
        except ValueError:
            return False
    return False


def is_spam(text: str) -> bool:
    """Filter out affiliate links, self-promo, and bot comments."""
    signals = [
        "subscribe to my", "check out my channel", "visit my channel",
        "promo code", "discount code", "use code", "coupon",
        "click the link", "link in bio", "t.me/", "whatsapp.com",
        "bit.ly", "tinyurl", "amzn.to",
    ]
    lower = text.lower()
    meaningful = re.sub(r"[^\w\s]", "", text).strip()
    if len(meaningful) < MIN_MEANINGFUL_TEXT_LEN:
        return True
    return any(signal in lower for signal in signals)


def normalize_comment_record(record: dict) -> dict:
    """Map legacy `channel` field to `video_channel` for older cache files."""
    if "video_channel" not in record and "channel" in record:
        record = {**record, "video_channel": record["channel"]}
    return record


# --- Scraper --------------------------------------------------------------------

class YouTubeScraper:
    """
    Searches YouTube for product review videos and scrapes their comments.

    Usage:
        scraper = YouTubeScraper()
        results = scraper.scrape("boAt Rockerz 450")
        scraper.save(results, "boat_rockerz450_youtube.json")
    """

    SEARCH_TEMPLATES = [
        "{product} review",
        "{product} honest review",
        "{product} worth buying",
        "{product} pros and cons",
        "{product} after 6 months",
    ]

    REVIEW_KEYWORDS = {
        "review", "unboxing", "honest", "pros", "cons", "worth",
        "buying", "should you buy", "detailed", "in depth", "after",
        "months", "weeks", "real world", "experience", "opinion",
        "verdict", "rating", "test", "compared", "vs",
    }

    SKIP_TITLE_KEYWORDS = {
        "#ad", "sponsored", "advertisement", "giveaway",
        "win free", "promo", "coupon",
    }

    def __init__(self, region: str = "IN"):
        _check_httpx_compat()
        self.region = region
        self.downloader = YoutubeCommentDownloader()
        self._last_errors: list[str] = []
        _log(f"[YouTube] Scraper ready (region={region}, no API key needed)")

    @staticmethod
    def cache_path(filename: str) -> Path:
        return CACHE_DIR / filename

    def scrape(
        self,
        product_name: str,
        max_videos: int = 5,
        max_comments_per_video: int = 50,
        min_likes: int = 0,
    ) -> list[dict]:
        """Search YouTube for review videos and return their top comments."""
        self._last_errors = []
        _log(f"\n[YouTube] Scraping: '{product_name}'")

        videos = self._find_videos(product_name, max_videos)
        if not videos:
            _log("[YouTube] No review videos found.")
            self._print_error_summary()
            return []

        _log(f"[YouTube] Found {len(videos)} videos to scrape")

        all_comments: list[YouTubeComment] = []
        for i, video in enumerate(videos):
            _log(f"[YouTube] [{i + 1}/{len(videos)}] {video.title[:65]}...")
            comments = self._scrape_video(video, max_comments_per_video, min_likes)
            all_comments.extend(comments)
            _log(f"           -> {len(comments)} comments collected")
            time.sleep(1.5)

        _log(f"[YouTube] Done. Total: {len(all_comments)} comments\n")
        self._print_error_summary()
        return [asdict(comment) for comment in all_comments]

    def _find_videos(self, product_name: str, max_videos: int) -> list[YouTubeVideo]:
        seen_ids: set[str] = set()
        videos: list[YouTubeVideo] = []

        for template in self.SEARCH_TEMPLATES:
            if len(videos) >= max_videos:
                break

            query = template.format(product=product_name)
            batch = self._search(query)

            for item in batch:
                if len(videos) >= max_videos:
                    break

                vid_id = item.get("id", "")
                title = item.get("title", "")
                duration = item.get("duration") or ""

                if not vid_id or vid_id in seen_ids:
                    continue
                if is_short(duration):
                    continue
                if self._is_ad(title):
                    continue
                if not self._looks_like_review(title):
                    continue

                seen_ids.add(vid_id)
                videos.append(YouTubeVideo(
                    video_id=vid_id,
                    title=title,
                    channel=item.get("channel", {}).get("name", ""),
                    url=f"https://www.youtube.com/watch?v={vid_id}",
                    view_count=item.get("viewCount", {}).get("short", ""),
                    duration=duration,
                    published=item.get("publishedTime", ""),
                ))

            time.sleep(0.5)

        return videos

    def _search(self, query: str) -> list[dict]:
        try:
            result = CustomSearch(
                query,
                VideoDurationFilter.long,
                limit=6,
                region=self.region,
            )
            return result.result().get("result", [])
        except Exception as exc:
            message = f"Search failed for '{query}': {exc}"
            self._last_errors.append(message)
            _log(f"[YouTube] {message}")
            return []

    def _is_ad(self, title: str) -> bool:
        lower = title.lower()
        return any(keyword in lower for keyword in self.SKIP_TITLE_KEYWORDS)

    def _looks_like_review(self, title: str) -> bool:
        lower = title.lower()
        return any(keyword in lower for keyword in self.REVIEW_KEYWORDS)

    def _scrape_video(
        self,
        video: YouTubeVideo,
        max_comments: int,
        min_likes: int,
    ) -> list[YouTubeComment]:
        comments: list[YouTubeComment] = []
        raw_budget = max(max_comments * COMMENT_FETCH_MULTIPLIER, COMMENT_FETCH_MIN_RAW)

        try:
            raw_iter = self.downloader.get_comments_from_url(
                video.url,
                sort_by=SORT_BY_POPULAR,
            )
            if raw_iter is None:
                message = f"Comments disabled or unavailable for {video.video_id}"
                self._last_errors.append(message)
                _log(f"           -> {message}")
                return []

            for raw in itertools.islice(raw_iter, raw_budget):
                if len(comments) >= max_comments:
                    break

                body = (raw.get("text") or "").strip()
                if not body or len(body) < MIN_COMMENT_BODY_LEN:
                    continue
                if is_spam(body):
                    continue

                likes = parse_likes(raw.get("votes", "0"))
                if likes < min_likes:
                    continue

                comments.append(YouTubeComment(
                    id=raw.get("cid", ""),
                    video_id=video.video_id,
                    video_title=video.title,
                    video_channel=video.channel,
                    body=body,
                    likes=likes,
                    author=raw.get("author", ""),
                    is_reply=bool(raw.get("reply", False)),
                    published=raw.get("time", ""),
                    time_parsed=raw.get("time_parsed"),
                ))

            if len(comments) < max_comments:
                self._last_errors.append(
                    f"Only collected {len(comments)}/{max_comments} comments "
                    f"for {video.video_id} (filters or disabled comments)"
                )

        except Exception as exc:
            message = f"Comment fetch failed for {video.video_id}: {exc}"
            self._last_errors.append(message)
            _log(f"           -> error: {exc}")

        return comments

    def _print_error_summary(self) -> None:
        if not self._last_errors:
            return
        _log(f"[YouTube] {len(self._last_errors)} warning(s) during scrape:")
        for error in self._last_errors[:10]:
            _log(f"  - {error}")
        if len(self._last_errors) > 10:
            _log(f"  ... and {len(self._last_errors) - 10} more")

    def save(self, comments: list[dict], filename: str) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = self.cache_path(filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(comments, handle, indent=2, ensure_ascii=False)
        _log(f"[YouTube] Saved {len(comments)} comments -> {path}")
        return path

    def load(self, filename: str) -> list[dict]:
        path = self.cache_path(filename)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        normalized = [normalize_comment_record(item) for item in data]
        _log(f"[YouTube] Loaded {len(normalized)} comments from cache")
        return normalized

    def print_stats(self, comments: list[dict]) -> None:
        if not comments:
            _log("[YouTube] Nothing to summarise.")
            return

        comments = [normalize_comment_record(item) for item in comments]
        video_channels = {
            item.get("video_channel") or item.get("channel", "")
            for item in comments
        }
        top_level = [item for item in comments if not item["is_reply"]]
        replies = [item for item in comments if item["is_reply"]]
        avg_likes = sum(item["likes"] for item in comments) / len(comments)
        top = max(comments, key=lambda item: item["likes"])

        _log("\n-- YouTube Summary -----------------------------------")
        _log(f"  Total comments  : {len(comments)}")
        _log(f"  Top-level       : {len(top_level)}")
        _log(f"  Replies         : {len(replies)}")
        _log(f"  Video channels  : {len(video_channels)}")
        _log(f"  Avg likes       : {avg_likes:.1f}")
        _log(f"  Most liked      : ({top['likes']} likes) {top['body'][:70]}...")
        _log("------------------------------------------------------\n")


if __name__ == "__main__":
    scraper = YouTubeScraper(region="IN")

    product = input("Enter product name: ").strip()
    if not product:
        _log("[YouTube] No product name provided.")
        sys.exit(1)

    safe = re.sub(r"[^\w]+", "_", product.lower()).strip("_")

    comments = scraper.scrape(
        product_name=product,
        max_videos=5,
        max_comments_per_video=50,
        min_likes=0,
    )

    scraper.print_stats(comments)
    scraper.save(comments, f"{safe}_youtube.json")

    _log("-- Sample Comments -----------------------------------")
    for comment in comments[:3]:
        channel = comment.get("video_channel", comment.get("channel", ""))
        _log(
            f"\n  [{channel}] {comment['likes']} likes | "
            f"author={comment['author']} | reply={comment['is_reply']}"
        )
        _log(f"  {comment['body'][:200]}")
        _log(f"  Video : {comment['video_title'][:60]}")
        _log(f"  Posted: {comment['published']}")
