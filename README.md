# Trureview---The-unbiased-reviewer
Multi-source product review aggregator with aspect-based sentiment analysis across Amazon, Flipkart, and Reddit.

*Because star ratings don't tell the whole story.*

You're about to buy something. Amazon says 4.3 stars. Flipkart says 4.1. Sounds great, right?

Then you find a Reddit thread where 40 people are saying the battery dies in 3 hours.

Trureview fixes that.It pulls reviews from everywhere — Reddit, Amazon, Flipkart — runs them through an NLP pipeline, and tells you what people *actually* think about each feature. Not just a number. A real answer.

---

## What it does

- Scrapes reviews from Reddit (PRAW API), Amazon, and Flipkart
- Runs aspect-based sentiment analysis — so instead of "mostly positive", you get "camera 😊 82% | battery 😞 34% | display 😊 71%"
- Compares BERT-based transformers vs VADER to analyse the accuracy vs speed tradeoff
- Shows everything in a Streamlit dashboard with charts, trends, and source breakdowns
- Generates a short AI verdict summarising all sources into plain English

---

## Project Structure

```
ReviewLens/
│
├── scrapers/
│   ├── reddit_scraper.py        # PRAW-based Reddit scraper (done)
│   ├── amazon_scraper.py        # BeautifulSoup + Selenium (in progress)
│   ├── flipkart_scraper.py      # BeautifulSoup scraper (in progress)
│   └── youtube_scraper.py       # YouTube Data API comments (planned)
│
├── nlp/
│   ├── sentiment.py             # HuggingFace BERT + VADER pipeline
│   ├── aspects.py               # KeyBERT aspect extraction
│   └── summarizer.py            # AI verdict generation
│
├── api/
│   └── main.py                  # FastAPI backend — serves scraper + NLP as REST endpoints
│
├── dashboard/
│   └── app.py                   # Streamlit UI with Plotly charts
│
├── data/
│   └── cache/                   # Scraped reviews cached as JSON (avoids re-scraping)
│
├── .env.example                 # Reddit API credentials template
├── requirements.txt
└── README.md
```

---

## Tech Stack

| Layer | Tools |
|---|---|
| Scraping | `PRAW` `BeautifulSoup4` `Selenium` `YouTube Data API` |
| NLP | `HuggingFace Transformers` `VADER` `KeyBERT` `sumy` |
| Backend | `FastAPI` `Python` |
| Frontend | `Streamlit` `Plotly` |
| Infra | `python-dotenv` JSON caching `HuggingFace Spaces` (deployment) |

---

## Status

This is actively being built. Here's where things stand:

| Module | Status |
|---|---|
| Reddit Scraper | ✅ Done |
| Flipkart Scraper | 🔄 In Progress |
| Amazon Scraper | 🔄 In Progress |
| Sentiment Pipeline (BERT + VADER) | 🔜 Up Next |
| Aspect Extraction (KeyBERT) | 🔜 Up Next |
| FastAPI Backend | 🔜 Up Next |
| Streamlit Dashboard | 🔜 Up Next |
| YouTube Scraper | 🔜 Planned |
| HuggingFace Spaces Deployment | 🔜 Planned |

---

## Setup & Run

```bash
git clone https://github.com/yourusername/ReviewLens.git
cd ReviewLens
pip install -r requirements.txt
```

You'll need Reddit API credentials. It takes about 2 minutes to set up:

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
2. Click **Create App** → choose **script**
3. Name it anything, set redirect URI to `http://localhost:8080`
4. Copy your `client_id` and `client_secret`

Then:

```bash
cp .env.example .env
# paste your credentials into .env
```

Run the Reddit scraper:

```bash
python scrapers/reddit_scraper.py
# Enter a product name when prompted
# Results saved to data/cache/
----


## Why aspect-based and not just sentiment?

Most sentiment tools give you a single score. That's not useful when you're actually deciding whether to buy something.

"Battery life is terrible but the display is stunning" should not average out to "neutral". It should tell you the battery is bad and the display is good — so you can decide if that tradeoff works for you.

That's what aspect extraction does. It pulls out the specific features people talk about and scores each one independently.


## Roadmap

- [ ] Hindi review support (important for Flipkart reviews)
- [ ] Fake review detection layer
- [ ] Price tracking integration
- [ ] Export report as PDF
- [ ] Deploy on Hugging Face Spaces


---

*Built as part of a data science portfolio. Feedback welcome.*
