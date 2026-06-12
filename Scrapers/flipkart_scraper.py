"""
TruReview — Flipkart Scraper
------------------------------
Scrapes product reviews from Flipkart using Playwright.

Why Playwright over Selenium:
    - Auto-waits for JS to finish rendering before reading the DOM
    - Better stealth — harder to fingerprint than Selenium
    - Faster and more stable for paginated scraping

Why not requests + BeautifulSoup:
    - Flipkart loads reviews via JavaScript after the initial HTML loads
    - requests only gets the bare shell — reviews are invisible to BS4

Install:
    pip install playwright beautifulsoup4
    playwright install chromium

How it works:
    1. Search flipkart.com for the product
    2. Show matching results and ask user to confirm the right one
    3. Navigate to that product's review page
    4. Paginate through reviews page by page
    5. Return clean list of dicts for the NLP pipeline

IMPORTANT — selectors:
    Flipkart uses obfuscated, minified class names (e.g. "_3LWZlK", "t-ZTKy")
    that change when they redeploy. If this scraper returns empty results,
    open Flipkart in Chrome DevTools and update the SELECTORS dict below.
    This is normal — plan for it every few weeks.
"""

import json
import os
import re
import time
import random
from dataclasses import dataclass, asdict
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ─── Selectors ────────────────────────────────────────────────────────────────
# These are the CSS selectors for Flipkart's current HTML structure.
# If reviews come back empty, open Chrome DevTools on a Flipkart review page
# and update these. They change with Flipkart deployments.

SELECTORS = {
    # Search results page
    "search_results":   "div[data-id]",           # each product card on search
    "product_title":    "div[class*='KzDlHZ'], div[class*='WKTcLC'], div[class*='RG5Slk'], ._4rR01T, .s1Q9rs",
    "product_link":     "a[href*='/p/'], a.k7wcnx", # product page anchor

    # Review page
    "review_container": "div[class*='RcXBOT'], div._1YokD2._3Mn1Hb, div[class*='col-9-12'], div[class*='_1AtVbE']",
    "review_card":      "div[class*='cPHDOP'], div._1AtVbE, div[class*='_27M-N9'], div.fWi7J_, div.BOoM4k",
    "rating":           "div[class*='XQDdHH'], div._3LWZlK, div[class*='MKiFS6'], div.XQDdHH",
    "review_title":     "p[class*='z9E0IG'], p._2-N8zT, p[class*='_2-N8zT'], .z9E0IG",
    "review_body":      "div[class*='ZmyHeo'], div.t-ZTKy, div[class*='t-ZTKy'], .ZmyHeo, .t-ZTKy",
    "reviewer_name":    "p[class*='_4ATV6X'], p._2sc7ZR, p[class*='_2sc7ZR'], ._2sc7ZR",
    "review_date":      "p[class*='_2sc7ZR']:last-child",
    "verified_badge":   "span[class*='_2mcQb2'], span._1e_JrY",
    "helpful_count":    "span[class*='_1mOPAs']",
    "next_page":        "a[class*='_9QVEpD']:last-child, nav a:last-child",
}

# User agents to rotate — keeps requests looking like real browsers
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class FlipkartReview:
    product_name: str
    product_url: str
    rating: Optional[float]         # 1.0 – 5.0
    title: str
    body: str
    reviewer: str
    date: str
    verified_purchase: bool
    helpful_count: int
    page_number: int
    source: str = "flipkart"


# ─── Scraper ──────────────────────────────────────────────────────────────────

class FlipkartScraper:
    """
    Scrapes product reviews from Flipkart.

    Usage:
        scraper = FlipkartScraper()

        # Interactive mode — shows search results, you pick the product
        reviews = scraper.scrape("boAt Rockerz 450")

        # Direct mode — if you already have the review page URL
        reviews = scraper.scrape_url(
            url="https://www.flipkart.com/boat-rockerz.../product-reviews/...",
            product_name="boAt Rockerz 450"
        )

        scraper.save(reviews, "boat_rockerz450_flipkart.json")
    """

    BASE_URL = "https://www.flipkart.com"
    SEARCH_URL = "https://www.flipkart.com/search?q={query}&otracker=search"

    def __init__(self, headless: bool = True):
        """
        Args:
            headless: Run browser in background (True) or show window (False).
                      Set headless=False while debugging to see what's happening.
        """
        self.headless = headless

    # ── Public API ────────────────────────────────────────────────────────────

    def scrape(
        self,
        product_name: str,
        max_pages: int = 5,
        max_reviews: int = 100,
        auto_select: bool = False,
    ) -> list[dict]:
        """
        Search for product, confirm selection, then scrape reviews.

        Args:
            product_name:  Product to search for.
            max_pages:     Max review pages to paginate through.
            max_reviews:   Hard cap on total reviews collected.
            auto_select:   If True, auto-picks the first search result.
                           If False (default), shows options and asks you to confirm.

        Returns:
            List of FlipkartReview dicts for the NLP pipeline.
        """
        print(f"\n[Flipkart] Searching for: '{product_name}'")

        with sync_playwright() as p:
            browser, page = self._launch(p)

            try:
                # Step 1 — search
                search_results = self._search(page, product_name)
                if not search_results:
                    print("[Flipkart] No results found. Saving page source for debug...")
                    with open("debug_flipkart_search.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                    return []

                # Step 2 — product selection
                product = self._select_product(search_results, auto_select)
                if not product:
                    print("[Flipkart] No product selected.")
                    return []

                print(f"[Flipkart] Selected: {product['title']}")

                # Step 3 — navigate to review page
                review_url = self._get_review_url(page, product["url"])
                if not review_url:
                    print("[Flipkart] Could not find review page for this product.")
                    return []

                # Step 4 — scrape reviews
                reviews = self._scrape_reviews(
                    page, review_url, product["title"], max_pages, max_reviews
                )

            finally:
                browser.close()

        print(f"[Flipkart] Done. Total: {len(reviews)} reviews\n")
        return [asdict(r) for r in reviews]

    def scrape_url(
        self,
        url: str,
        product_name: str,
        max_pages: int = 5,
        max_reviews: int = 100,
    ) -> list[dict]:
        """
        Scrape directly from a known review page URL.
        Use this when you already have the Flipkart review URL.

        The review URL format is:
            https://www.flipkart.com/{product-slug}/product-reviews/{item-id}?...
        """
        print(f"\n[Flipkart] Scraping reviews from URL: {url[:80]}...")

        with sync_playwright() as p:
            browser, page = self._launch(p)
            try:
                reviews = self._scrape_reviews(
                    page, url, product_name, max_pages, max_reviews
                )
            finally:
                browser.close()

        print(f"[Flipkart] Done. Total: {len(reviews)} reviews\n")
        return [asdict(r) for r in reviews]

    def _scrape_reviews_js(self, page, product_name, product_url, page_num) -> list[FlipkartReview]:
        """Extract reviews using JavaScript execution in the browser."""
        try:
            # A more robust JS extraction for the new layout
            extracted = page.evaluate(r"""
                () => {
                    const results = [];
                    // Find all review blocks. They usually contain 'Certified Buyer'
                    const allDivs = Array.from(document.querySelectorAll('div'));
                    const reviewBlocks = allDivs.filter(div => 
                        div.innerText && 
                        div.innerText.includes('Certified Buyer') && 
                        div.innerText.length > 30 &&
                        div.innerText.length < 2000 // Avoid large container divs
                    );
                    
                    // Filter out parents to get only the most specific review divs
                    const leafReviews = reviewBlocks.filter(div => 
                        !div.querySelector('div')?.innerText?.includes('Certified Buyer')
                    );

                    leafReviews.forEach(card => {
                        const text = card.innerText;
                        const lines = text.split('\n').filter(l => l.trim().length > 0);
                        
                        // Try to find a rating (usually a number like 4 or 5)
                        let rating = 5.0;
                        const ratingMatch = text.match(/^(\d)(\.\d)?/m);
                        if (ratingMatch) {
                            rating = parseFloat(ratingMatch[0]);
                        }

                        if (lines.length >= 1) {
                            results.push({
                                title: lines[0].substring(0, 100),
                                body: text.substring(0, 1000),
                                reviewer: lines.find(l => l.includes('Certified Buyer')) || 'Flipkart Customer',
                                rating: rating
                            });
                        }
                    });
                    return results;
                }
            """)
            
            reviews = []
            for item in extracted:
                reviews.append(FlipkartReview(
                    product_name=product_name,
                    product_url=product_url,
                    rating=item.get('rating', 5.0),
                    title=item.get('title', ''),
                    body=item.get('body', ''),
                    reviewer=item.get('reviewer', 'Flipkart Customer').replace('Certified Buyer', '').strip(' •'),
                    date='Recent',
                    verified_purchase=True,
                    helpful_count=0,
                    page_number=page_num
                ))
            return reviews
        except Exception as e:
            print(f"[Flipkart] JS extraction error: {e}")
            return []

    # ── Browser Setup ─────────────────────────────────────────────────────────

    def _launch(self, playwright):
        """Launch Chromium with stealth settings."""
        browser = playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        )

        # Mask playwright's navigator.webdriver fingerprint
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        """)

        page = ctx.new_page()

        # Block images and fonts to load pages faster
        page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())

        return browser, page

    # ── Search ────────────────────────────────────────────────────────────────

    def _search(self, page, product_name: str) -> list[dict]:
        """Search Flipkart and return list of product dicts."""
        query = product_name.replace(" ", "+")
        url = self.SEARCH_URL.format(query=query)

        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
            self._dismiss_login_popup(page)
            self._human_delay(2, 3)

            # Wait for product cards to appear
            page.wait_for_selector(SELECTORS["search_results"], timeout=10000)

        except PWTimeout:
            print("[Flipkart] Search page timed out.")
            return []
        except Exception as e:
            print(f"[Flipkart] Search error: {e}")
            return []

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        return self._parse_search_results(soup)

    def _parse_search_results(self, soup: BeautifulSoup) -> list[dict]:
        """Extract product cards from search results page."""
        results = []
        cards = soup.select(SELECTORS["search_results"])

        for card in cards[:8]:  # top 8 results
            # title — try multiple selector fallbacks
            title_el = card.select_one(SELECTORS["product_title"])
            title = title_el.get_text(strip=True) if title_el else ""

            # product link
            link_el = card.select_one(SELECTORS["product_link"])
            href = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = self.BASE_URL + href

            if not title or not href:
                continue

            # rating if available
            rating_el = card.select_one(SELECTORS["rating"])
            rating = rating_el.get_text(strip=True) if rating_el else "N/A"

            results.append({
                "title": title,
                "url": href,
                "rating": rating,
            })

        return results

    # ── Product Selection ─────────────────────────────────────────────────────

    def _select_product(self, results: list[dict], auto_select: bool) -> Optional[dict]:
        """Show search results and let user confirm the right product."""
        if not results:
            return None

        if auto_select:
            return results[0]

        print("\n── Search Results ──────────────────────────────────")
        for i, r in enumerate(results):
            print(f"  [{i+1}] {r['title'][:70]}")
            print(f"       Rating: {r['rating']}  |  {r['url'][:60]}...")
        print("────────────────────────────────────────────────────")

        while True:
            try:
                choice = input("\nEnter number to select product (or 0 to cancel): ").strip()
                idx = int(choice)
                if idx == 0:
                    return None
                if 1 <= idx <= len(results):
                    return results[idx - 1]
                print(f"Please enter a number between 1 and {len(results)}.")
            except ValueError:
                print("Please enter a valid number.")

    # ── Review Page Navigation ────────────────────────────────────────────────

    def _get_review_url(self, page, product_url: str) -> Optional[str]:
        try:
            page.goto(product_url, timeout=30000, wait_until="networkidle")
            self._dismiss_login_popup(page)
            self._human_delay(2, 3)

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Look for "All Reviews" link or any /product-reviews/ link
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/product-reviews/" in href:
                    full = href if href.startswith("http") else self.BASE_URL + href
                    # Strip to base review URL (remove sort params etc.)
                    full = re.sub(r"&sort=[^&]+", "", full)
                    return full

        except Exception as e:
            print(f"[Flipkart] Could not get review URL: {e}")

        return None

    # ── Review Scraping ───────────────────────────────────────────────────────

    def _scrape_reviews(
        self,
        page,
        review_url: str,
        product_name: str,
        max_pages: int,
        max_reviews: int,
    ) -> list[FlipkartReview]:
        """Paginate through review pages and extract all reviews."""
        all_reviews = []
        current_url = review_url
        page_num = 1

        while page_num <= max_pages and len(all_reviews) < max_reviews:
            print(f"[Flipkart] Page {page_num} — {current_url[:80]}...")

            try:
                page.goto(current_url, timeout=60000, wait_until="networkidle")
                self._dismiss_login_popup(page)
                self._human_delay(3, 5)

                # Wait for review content to be hydrated
                try:
                    page.wait_for_selector("text=Certified Buyer", timeout=15000)
                except PWTimeout:
                    print(f"[Flipkart] Page {page_num} 'Certified Buyer' text didn't appear.")

                # Extra wait for stability
                page.wait_for_timeout(3000)

                # Scroll down to trigger lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                self._human_delay(1, 2)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                self._human_delay(1, 2)

                html = page.content()

            except PWTimeout:
                print(f"[Flipkart] Page {page_num} timed out. Stopping.")
                break
            except Exception as e:
                print(f"[Flipkart] Page {page_num} error: {e}")
                break

            soup = BeautifulSoup(html, "html.parser")
            page_reviews = self._parse_reviews(soup, product_name, review_url, page_num)

            # If BS4 fails, try direct JS extraction as a fallback
            if not page_reviews:
                print(f"[Flipkart] BS4 extraction failed on page {page_num}. Trying JS extraction...")
                page_reviews = self._scrape_reviews_js(page, product_name, review_url, page_num)

            if not page_reviews:
                print(f"[Flipkart] No reviews found on page {page_num}. Saving debug info...")
                page.screenshot(path=f"debug_reviews_page_{page_num}.png")
                with open(f"debug_flipkart_reviews_page_{page_num}.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                break

            all_reviews.extend(page_reviews)
            print(f"[Flipkart] Page {page_num}: {len(page_reviews)} reviews (total: {len(all_reviews)})")

            # Find next page URL
            next_url = self._get_next_page_url(soup, current_url, page_num)
            if not next_url:
                print("[Flipkart] No more pages.")
                break

            current_url = next_url
            page_num += 1
            self._human_delay(3, 5)  # longer delay between pages

        return all_reviews[:max_reviews]

    def _parse_reviews(
        self,
        soup: BeautifulSoup,
        product_name: str,
        product_url: str,
        page_num: int,
    ) -> list[FlipkartReview]:
        """Parse all review cards from a single review page."""
        reviews = []

        # Try to find the review section container first
        container = soup.select_one(SELECTORS["review_container"])
        search_scope = container if container else soup

        # Each review is in a card div
        cards = search_scope.select(SELECTORS["review_card"])
        
        if not cards:
            # Fallback — look for any div containing "Certified Buyer"
            # This is more robust against obfuscated class name changes
            cards = [
                div.parent for div in soup.find_all(string=re.compile("Certified Buyer"))
                if div.parent
            ]
            # De-duplicate cards
            unique_cards = []
            seen = set()
            for card in cards:
                if id(card) not in seen:
                    unique_cards.append(card)
                    seen.add(id(card))
            cards = unique_cards

        for card in cards:
            review = self._parse_single_review(card, product_name, product_url, page_num)
            if review:
                reviews.append(review)

        return reviews

    def _parse_single_review(
        self,
        card,
        product_name: str,
        product_url: str,
        page_num: int,
    ) -> Optional[FlipkartReview]:
        """Extract fields from a single review card."""

        # Rating
        rating_el = card.select_one(SELECTORS["rating"])
        rating = None
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True).split()[0])
            except (ValueError, IndexError):
                pass

        # Title
        title_el = card.select_one(SELECTORS["review_title"])
        title = title_el.get_text(strip=True) if title_el else ""

        # Body — the actual review text
        body_el = card.select_one(SELECTORS["review_body"])
        body = ""
        if body_el:
            # Flipkart sometimes nests in a <div class="row">
            spans = body_el.find_all("span", recursive=False)
            if spans:
                body = " ".join(s.get_text(strip=True) for s in spans)
            else:
                body = body_el.get_text(strip=True)

        # Skip cards that have no meaningful content
        if not body or len(body) < 10:
            return None

        # Reviewer name
        name_el = card.select_one(SELECTORS["reviewer_name"])
        reviewer = name_el.get_text(strip=True) if name_el else ""

        # Date
        date_el = card.select_one(SELECTORS["review_date"])
        date = date_el.get_text(strip=True) if date_el else ""

        # Verified purchase badge
        verified = bool(card.select_one(SELECTORS["verified_badge"]))

        # Helpful count
        helpful_el = card.select_one(SELECTORS["helpful_count"])
        helpful = 0
        if helpful_el:
            try:
                helpful = int(re.sub(r"\D", "", helpful_el.get_text()))
            except ValueError:
                pass

        return FlipkartReview(
            product_name=product_name,
            product_url=product_url,
            rating=rating,
            title=title,
            body=body,
            reviewer=reviewer,
            date=date,
            verified_purchase=verified,
            helpful_count=helpful,
            page_number=page_num,
        )

    def _get_next_page_url(
        self, soup: BeautifulSoup, current_url: str, current_page: int
    ) -> Optional[str]:
        """Find the URL for the next review page."""

        # Method 1 — look for next page anchor in pagination
        next_el = soup.select_one(SELECTORS["next_page"])
        if next_el and next_el.get("href"):
            href = next_el["href"]
            if "page=" in href or "/product-reviews/" in href:
                return href if href.startswith("http") else self.BASE_URL + href

        # Method 2 — manually increment page parameter in URL
        if "page=" in current_url:
            return re.sub(r"page=\d+", f"page={current_page + 1}", current_url)

        # Method 3 — append page parameter
        sep = "&" if "?" in current_url else "?"
        return f"{current_url}{sep}page={current_page + 1}"

    # ── Anti-Detection Helpers ────────────────────────────────────────────────

    def _dismiss_login_popup(self, page):
        """Close Flipkart's login popup if it appears."""
        try:
            close_btn = page.query_selector("button._2KpZ6l._2doB4z")
            if close_btn:
                close_btn.click()
                self._human_delay(0.5, 1)
        except Exception:
            pass  # popup wasn't there, that's fine

    def _human_delay(self, min_s: float = 1.5, max_s: float = 3.0):
        """Random delay to mimic human browsing speed."""
        time.sleep(random.uniform(min_s, max_s))

    # ── Save / Load ───────────────────────────────────────────────────────────

    def save(self, reviews: list[dict], filename: str):
        """Save reviews to JSON cache."""
        os.makedirs("data/cache", exist_ok=True)
        path = os.path.join("data/cache", filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reviews, f, indent=2, ensure_ascii=False)
        print(f"[Flipkart] Saved {len(reviews)} reviews → {path}")

    def load(self, filename: str) -> list[dict]:
        """Load cached reviews."""
        path = os.path.join("data/cache", filename)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[Flipkart] Loaded {len(data)} reviews from cache")
        return data

    # ── Stats ─────────────────────────────────────────────────────────────────

    def print_stats(self, reviews: list[dict]):
        """Print summary of scraped data."""
        if not reviews:
            print("[Flipkart] Nothing to summarise.")
            return

        rated = [r for r in reviews if r["rating"] is not None]
        avg_rating = sum(r["rating"] for r in rated) / len(rated) if rated else 0
        verified = sum(1 for r in reviews if r["verified_purchase"])
        pages = {r["page_number"] for r in reviews}

        print("\n── Flipkart Summary ────────────────────────────────")
        print(f"  Total reviews    : {len(reviews)}")
        print(f"  Pages scraped    : {len(pages)}")
        print(f"  Avg rating       : {avg_rating:.2f} / 5.0")
        print(f"  Verified purchase: {verified} ({verified*100//len(reviews)}%)")
        rating_dist = {i: sum(1 for r in rated if r["rating"] == i) for i in range(1, 6)}
        max_count = max(rating_dist.values())
        for star, count in sorted(rating_dist.items(), reverse=True):
            bar = "█" * (count * 20 // max_count) if max_count > 0 else ""
            print(f"  {star}★  {bar} {count}")
        print("────────────────────────────────────────────────────\n")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scraper = FlipkartScraper(headless=True)

    product = input("Enter product name: ").strip()
    safe = product.lower().replace(" ", "_")

    # Interactive mode — shows results, you confirm
    reviews = scraper.scrape(
        product_name=product,
        max_pages=5,
        max_reviews=100,
        auto_select=False,
    )

    scraper.print_stats(reviews)
    scraper.save(reviews, f"{safe}_flipkart.json")

    # Preview
    print("── Sample Reviews ──────────────────────────────────")
    for r in reviews[:3]:
        print(f"\n  {'⭐' * int(r['rating'] or 0)} ({r['rating']}) | Verified: {r['verified_purchase']}")
        print(f"  Title : {r['title']}")
        print(f"  Review: {r['body'][:200]}")
        print(f"  By    : {r['reviewer']} | {r['date']}")
