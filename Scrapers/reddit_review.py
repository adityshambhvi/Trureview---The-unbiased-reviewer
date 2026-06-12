import praw
import os
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from dotenv import load_dotenv
load_dotenv()


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class RedditReview:
    """Represents a single Reddit post or comment about a product."""
    id: str
    source: str                  # "post" or "comment"
    subreddit: str
    title: Optional[str]         # only for posts
    body: str
    score: int                   # upvotes - downvotes
    upvote_ratio: Optional[float]  # only for posts
    url: str
    author: str
    created_utc: str             # ISO format
    num_comments: Optional[int]  # only for posts
    awards: int


# ─── Scraper Class ────────────────────────────────────────────────────────────

class RedditScraper:
    """
    Scrapes Reddit for product reviews and discussions.

    Usage:
        scraper = RedditScraper()
        results = scraper.scrape(product_name="Sony WH-1000XM5")
        scraper.save(results, "sony_wh1000xm5_reviews.json")
    """

    # Subreddits most likely to have genuine product reviews
    REVIEW_SUBREDDITS = [
        "reviews",
        "BuyItForLife",
        "gadgets",
        "tech",
        "hardware",
        "headphones",
        "audiophile",
        "Android",
        "apple",
        "laptops",
        "homeautomation",
        "smarthome",
        "Cameras",
        "photography",
        "cycling",
        "running",
        "Fitness",
        "india",              # great for India-specific product opinions
        "IndiaShopping",
        "IndianGaming",
    ]

    def __init__(self):
        self.reddit = praw.Reddit(
            client_id=os.getenv("REDDIT_CLIENT_ID"),
            client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
            user_agent=os.getenv("REDDIT_USER_AGENT", "ReviewLens/1.0"),
        )
        print(f"[Reddit] Connected (read-only mode: {self.reddit.read_only})")

    # ── Core Scraping Methods ─────────────────────────────────────────────────

    def scrape(
        self,
        product_name: str,
        max_posts: int = 30,
        max_comments_per_post: int = 10,
        include_subreddit_search: bool = True,
        min_score: int = 2,
    ) -> list[dict]:
        """
        Main entry point. Searches Reddit globally + targeted subreddits.

        Args:
            product_name:              Product to search for.
            max_posts:                 Max posts to pull from global search.
            max_comments_per_post:     Top comments to pull per post.
            include_subreddit_search:  Also search specific subreddits.
            min_score:                 Skip posts/comments below this score.

        Returns:
            List of RedditReview dicts, ready for NLP pipeline.
        """
        print(f"\n[Reddit] Scraping reviews for: '{product_name}'")
        all_reviews = []
        seen_ids = set()

        # 1. Global Reddit search
        global_results = self._search_all(
            product_name, max_posts, max_comments_per_post, min_score, seen_ids
        )
        all_reviews.extend(global_results)
        print(f"[Reddit] Global search: {len(global_results)} items found")

        # 2. Targeted subreddit search (catches niche communities)
        if include_subreddit_search:
            sub_results = self._search_subreddits(
                product_name, max_comments_per_post, min_score, seen_ids
            )
            all_reviews.extend(sub_results)
            print(f"[Reddit] Subreddit search: {len(sub_results)} additional items found")

        print(f"[Reddit] Total collected: {len(all_reviews)} items\n")
        return all_reviews

    def _search_all(
        self,
        query: str,
        max_posts: int,
        max_comments_per_post: int,
        min_score: int,
        seen_ids: set,
    ) -> list[dict]:
        """Search across all of Reddit."""
        reviews = []

        try:
            posts = self.reddit.subreddit("all").search(
                query=f"{query} review",
                sort="relevance",
                time_filter="year",     # last 1 year — keeps reviews fresh
                limit=max_posts,
            )

            for post in posts:
                if post.score < min_score or post.id in seen_ids:
                    continue

                seen_ids.add(post.id)

                # Add the post itself if it has meaningful body text
                body = (post.selftext or "").strip()
                if body and len(body) > 50:
                    reviews.append(asdict(self._parse_post(post)))

                # Pull top comments from each post
                comments = self._get_top_comments(
                    post, max_comments_per_post, min_score, seen_ids
                )
                reviews.extend(comments)

                time.sleep(0.3)  # be polite to the API

        except Exception as e:
            print(f"[Reddit] Global search error: {e}")

        return reviews

    def _search_subreddits(
        self,
        query: str,
        max_comments_per_post: int,
        min_score: int,
        seen_ids: set,
    ) -> list[dict]:
        """Search targeted subreddits for more specific results."""
        reviews = []

        for subreddit_name in self.REVIEW_SUBREDDITS:
            try:
                subreddit = self.reddit.subreddit(subreddit_name)
                posts = subreddit.search(
                    query=query,
                    sort="relevance",
                    time_filter="year",
                    limit=5,             # just top 5 per subreddit
                )

                for post in posts:
                    if post.score < min_score or post.id in seen_ids:
                        continue

                    seen_ids.add(post.id)

                    body = (post.selftext or "").strip()
                    if body and len(body) > 50:
                        reviews.append(asdict(self._parse_post(post)))

                    comments = self._get_top_comments(
                        post, max_comments_per_post, min_score, seen_ids
                    )
                    reviews.extend(comments)

                time.sleep(0.2)

            except Exception:
                # Subreddit may be private/banned — skip silently
                continue

        return reviews

    def _get_top_comments(
        self,
        post,
        max_comments: int,
        min_score: int,
        seen_ids: set,
    ) -> list[dict]:
        """Pull top-level comments from a post."""
        comments = []

        try:
            post.comments.replace_more(limit=0)  # don't expand MoreComments
            top_comments = sorted(
                (c for c in post.comments if hasattr(c, "body")),
                key=lambda c: c.score,
                reverse=True,
            )[:max_comments]

            for comment in top_comments:
                if (
                    comment.score < min_score
                    or comment.id in seen_ids
                    or not hasattr(comment, "body")
                    or len(comment.body.strip()) < 30
                    or comment.body in ("[deleted]", "[removed]")
                ):
                    continue

                seen_ids.add(comment.id)
                comments.append(asdict(self._parse_comment(comment, post)))

        except Exception as e:
            print(f"[Reddit] Comment fetch error on post {post.id}: {e}")

        return comments

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_post(self, post) -> RedditReview:
        return RedditReview(
            id=post.id,
            source="post",
            subreddit=str(post.subreddit),
            title=post.title,
            body=post.selftext.strip(),
            score=post.score,
            upvote_ratio=post.upvote_ratio,
            url=f"https://reddit.com{post.permalink}",
            author=str(post.author) if post.author else "[deleted]",
            created_utc=datetime.fromtimestamp(post.created_utc, tz=timezone.utc).isoformat(),
            num_comments=post.num_comments,
            awards=len(post.all_awardings) if hasattr(post, "all_awardings") else 0,
        )

    def _parse_comment(self, comment, post) -> RedditReview:
        return RedditReview(
            id=comment.id,
            source="comment",
            subreddit=str(post.subreddit),
            title=None,
            body=comment.body.strip(),
            score=comment.score,
            upvote_ratio=None,
            url=f"https://reddit.com{comment.permalink}",
            author=str(comment.author) if comment.author else "[deleted]",
            created_utc=datetime.fromtimestamp(comment.created_utc, tz=timezone.utc).isoformat(),
            num_comments=None,
            awards=len(comment.all_awardings) if hasattr(comment, "all_awardings") else 0,
        )

    # ── Save / Load ───────────────────────────────────────────────────────────

    def save(self, reviews: list[dict], filename: str):
        """Save scraped reviews to a JSON file."""
        os.makedirs("data/cache", exist_ok=True)
        filepath = os.path.join("data/cache", filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(reviews, f, indent=2, ensure_ascii=False)

        print(f"[Reddit] Saved {len(reviews)} reviews → {filepath}")

    def load(self, filename: str) -> list[dict]:
        """Load previously scraped reviews from cache."""
        filepath = os.path.join("data/cache", filename)

        with open(filepath, "r", encoding="utf-8") as f:
            reviews = json.load(f)

        print(f"[Reddit] Loaded {len(reviews)} reviews from cache")
        return reviews

    # ── Stats ─────────────────────────────────────────────────────────────────

    def print_stats(self, reviews: list[dict]):
        """Print a quick summary of what was scraped."""
        posts = [r for r in reviews if r["source"] == "post"]
        comments = [r for r in reviews if r["source"] == "comment"]
        subreddits = list({r["subreddit"] for r in reviews})

        print("\n── Scrape Summary ──────────────────────────")
        print(f"  Total items   : {len(reviews)}")
        print(f"  Posts         : {len(posts)}")
        print(f"  Comments      : {len(comments)}")
        print(f"  Subreddits    : {len(subreddits)}")
        print(f"  Top subs      : {', '.join(subreddits[:5])}")
        if reviews:
            avg_score = sum(r["score"] for r in reviews) / len(reviews)
            print(f"  Avg score     : {avg_score:.1f}")
        print("────────────────────────────────────────────\n")


# ─── Quick Test (run directly) ────────────────────────────────────────────────

if __name__ == "__main__":
    scraper = RedditScraper()

    product = input("Enter product name: ").strip()
    safe_name = product.lower().replace(" ", "_")

    reviews = scraper.scrape(
        product_name=product,
        max_posts=30,
        max_comments_per_post=10,
        include_subreddit_search=True,
        min_score=2,
    )

    scraper.print_stats(reviews)
    scraper.save(reviews, f"{safe_name}_reddit.json")

    # Preview first 3 results
    print("── Sample Reviews ──────────────────────────")
    for r in reviews[:3]:
        print(f"\n[{r['source'].upper()}] r/{r['subreddit']} | score: {r['score']}")
        if r["title"]:
            print(f"  Title  : {r['title']}")
        body_preview = r["body"][:200]
        if len(r["body"]) > 200:
            body_preview += "..."
        print(f"  Body   : {body_preview}")
        print(f"  Date   : {r['created_utc']}")
        print(f"  URL    : {r['url']}")
