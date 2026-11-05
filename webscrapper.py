#!/usr/bin/env python3
"""
keyword_scraper.py

A simple command-line web scraper that takes a single keyword as input,
runs a search via DuckDuckGo's HTML search endpoint (no API key needed),
and scrapes the resulting page titles, links, and snippets.

Usage:
    python keyword_scraper.py "your keyword"
    python keyword_scraper.py "your keyword" --num 20 --out results.csv
    python keyword_scraper.py "your keyword" --fetch-pages

Requirements:
    pip install requests beautifulsoup4
"""

import argparse
import csv
import sys
import time
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SEARCH_URL = "https://html.duckduckgo.com/html/"


def search_keyword(keyword: str, num_results: int = 10, delay: float = 1.0):
    """
    Query DuckDuckGo's HTML search endpoint for a keyword and return a list
    of dicts with 'title', 'url', and 'snippet'.
    """
    results = []
    headers = {"User-Agent": USER_AGENT}
    params = {"q": keyword}

    session = requests.Session()
    resp = session.post(SEARCH_URL, data=params, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    result_blocks = soup.select("div.result")

    for block in result_blocks:
        if len(results) >= num_results:
            break

        title_tag = block.select_one("a.result__a")
        snippet_tag = block.select_one("a.result__snippet") or block.select_one(
            "div.result__snippet"
        )

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag.get("href", "")
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        results.append({"title": title, "url": link, "snippet": snippet})

    time.sleep(delay)  # be polite between requests
    return results


def fetch_page_text(url: str, timeout: int = 10) -> str:
    """
    Fetch a page and return its visible text content (best-effort).
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"[Error fetching page: {e}]"

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove script/style tags
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = " ".join(soup.get_text(separator=" ").split())
    return text[:2000]  # cap length for readability


def save_to_csv(results, out_path: str):
    fieldnames = ["title", "url", "snippet", "page_text"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main():
    parser = argparse.ArgumentParser(
        description="Scrape web search results for a single keyword."
    )
    parser.add_argument("keyword", help="The keyword/phrase to search for")
    parser.add_argument(
        "--num", type=int, default=10, help="Number of results to fetch (default: 10)"
    )
    parser.add_argument(
        "--out", type=str, default=None, help="Optional path to save results as CSV"
    )
    parser.add_argument(
        "--fetch-pages",
        action="store_true",
        help="Also fetch and extract text content from each result page",
    )
    args = parser.parse_args()

    print(f"Searching for: {args.keyword!r}\n")

    try:
        results = search_keyword(args.keyword, num_results=args.num)
    except requests.RequestException as e:
        print(f"Search request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No results found.")
        return

    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")
        print(f"   {r['url']}")
        if r["snippet"]:
            print(f"   {r['snippet']}")

        if args.fetch_pages:
            print("   Fetching page content...")
            r["page_text"] = fetch_page_text(r["url"])
            preview = r["page_text"][:200]
            print(f"   Preview: {preview}...")
            time.sleep(1)  # polite delay between page fetches
        print()

    if args.out:
        save_to_csv(results, args.out)
        print(f"Saved {len(results)} results to {args.out}")


if __name__ == "__main__":
    main()