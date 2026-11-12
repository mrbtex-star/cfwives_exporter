#!/usr/bin/env python3
"""
Extract cookies from a running browser and save them for the CFWives exporter.

Usage:
    python capture_cookies.py [--url URL] [--output PATH]
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser(description="Capture browser cookies for CFWives exporter")
    ap.add_argument("--url", default="https://cfwives.com/chat/",
                    help="URL to visit and capture cookies from")
    ap.add_argument("--output", default="cfwives_cookies.json",
                    help="Output JSON file path")
    args = ap.parse_args()

    print(f"  Launching browser and navigating to {args.url} ...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(args.url, wait_until="networkidle", timeout=30000)

        cookies = context.cookies()
        browser.close()

    # Filter to relevant cookies
    relevant = {c["name"]: c["value"] for c in cookies
                if any(k in c["name"] for k in ["PHPSESSID", "bc_", "utk"])}

    if not relevant:
        print("  No session cookies found. You need to log in first.")
        print("  Use --no-headless to see the login form:")
        print("    python capture_cookies.py --no-headless")
        return

    data = {"cookies": relevant, "url": args.url,
            "timestamp": __import__("datetime").datetime.now().isoformat()}

    out = Path(args.output)
    with open(out, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n  Captured {len(relevant)} cookies → {out}")
    print(f"  PHPSESSID: {relevant.get('PHPSESSID', '???')[:20]}...")
    print(f"  bc_userid: {relevant.get('bc_userid', '???')}")
    print(f"\n  Run the exporter with:")
    print(f"    python main.py --cookies {out}")


if __name__ == "__main__":
    main()
