#!/usr/bin/env python3
"""
CFWives Chat Stream Exporter (Standalone)
=========================================
A Playwright-based script that runs independently to:
  1. Open cfwives.com chat and capture all visible messages from the DOM
  2. Download every image linked in chat messages (avatars, posted photos)
  3. Export messages as JSON and Markdown

Usage:
    python main.py [options]

Options:
    --room STR         Room URL path (default: room/1 = main room)
    --output DIR       Output directory (default: export/)
    --formats LIST     Output formats: json,md (default: json,md)
    --limit NUM        Stop after this many new messages (0 = unlimited)
    --interval SEC     DOM poll interval in seconds (default: 3)
    --no-headless      Keep browser window visible (for debugging)
    --timeout SECS     Page load timeout (default: 60)
    --cookies PATH     Load session cookies from JSON file
    --verbose          Verbose output to stderr
    --dry-run          Print what would be done, skip downloads
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


class CFWivesExporter:
    """Standalone CFWives chat stream exporter using Playwright."""

    BASE_URL = "https://cfwives.com"
    ROOM_LABELS = {
        "room/1": "Main Room",
        "cuckold-chat": "Cuckold Chat",
        "adult-chat": "Adult Chat",
        "tchat-sexe": "French Sex Chat",
        "sex-chat": "Sex Chat",
        "chat-sesso": "Italian Sex Chat",
        "chat-sexo": "Spanish Sex Chat",
    }

    def __init__(
        self,
        room: str = "room/1",
        output_dir: str = "export",
        formats: list = None,
        limit: int = 0,
        interval: float = 3.0,
        headless: bool = True,
        timeout: int = 60,
        dry_run: bool = False,
        cookies_file: str = "",
        verbose: bool = False,
    ):
        self.room = room
        self.room_name = self.ROOM_LABELS.get(room, room)
        self.output_dir = Path(output_dir) / f"cfwives_{room.replace('/', '_')}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.img_dir = self.output_dir / "images"
        self.img_dir.mkdir(exist_ok=True)
        self.formats = formats or ["json", "md"]
        self.limit = limit
        self.interval = interval
        self.headless = headless
        self.timeout_ms = timeout * 1000
        self.dry_run = dry_run
        self.cookies_file = cookies_file
        self.verbose = verbose

        self.messages = []
        self.seen_ids = set()
        self.downloaded_hashes = set()
        self.total_downloaded = 0
        self.image_requests = []
        self.running = True

    # ------------------------------------------------------------------ logging

    def log(self, msg: str, force: bool = False):
        if self.verbose or force:
            print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)

    def load_cookies(self) -> bool:
        """Load session cookies from JSON file and return list for Playwright."""
        if not self.cookies_file or not Path(self.cookies_file).exists():
            return False
        try:
            with open(self.cookies_file) as f:
                data = json.load(f)
            cookie_data = data.get("cookies", {})
            if not cookie_data:
                return False
            cookies = []
            # Only keep the essential auth cookies (skip analytics/tracking ones)
            keep_keys = {"PHPSESSID", "bc_userid", "bc_utk", "XSRF-TOKEN"}
            for name, value in cookie_data.items():
                if name not in keep_keys:
                    continue
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": ".cfwives.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": name != "XSRF-TOKEN",
                    "sameSite": "Lax",
                })
            self.log(f"Loaded {len(cookies)} auth cookies from {self.cookies_file}")
            return cookies
        except Exception as exc:
            self.log(f"Failed to load cookies: {exc}", force=True)
            return False

    # ------------------------------------------------------------------ helpers

    def absolute_url(self, url: str) -> str:
        if not url:
            return ""
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return self.BASE_URL + url
        return f"{self.BASE_URL}/{url}"

    def url_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:12]

    # ---------------------------------------------------------------- image download

    def save_image(self, url: str) -> str:
        """Download an image, save locally, return local path."""
        if self.dry_run:
            self.log(f"[DRY-RUN] Would download {url}")
            self.total_downloaded += 1
            return ""

        ext = ".jpg"
        low = url.lower()
        if ".png" in low or "tumb.png" in low:
            ext = ".png"
        elif ".jpeg" in low or ".jpg" in low:
            ext = ".jpg"
        elif ".webp" in low:
            ext = ".webp"
        elif ".gif" in low:
            ext = ".gif"

        h = self.url_hash(url)
        dest = self.img_dir / (h + ext)

        if h in self.downloaded_hashes:
            return str(dest)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                dest.write_bytes(data)
            self.downloaded_hashes.add(h)
            self.total_downloaded += 1
            self.log(f"  Downloaded {dest.name} ({len(data)} bytes)")
            return str(dest)
        except Exception as exc:
            self.log(f"  Failed {url}: {exc}", force=True)
            return ""

    # ------------------------------------------------------ message extraction

    def _parse_message(self, raw: dict) -> dict:
        """Convert a JS-extracted message dict into our canonical format."""
        msg = {
            "id": raw.get("id", ""),
            "log_id": raw.get("logId", ""),
            "username": raw.get("username", "Unknown"),
            "date": raw.get("date", ""),
            "text": raw.get("text", ""),
            "image_url": self.absolute_url(raw.get("imageUrl", "")),
            "full_image_url": self.absolute_url(raw.get("fullImageUrl", "")),
            "avatar_url": self.absolute_url(raw.get("avatarUrl", "")),
            "rank": raw.get("rank", ""),
            "level": raw.get("level", "0"),
            "user_id": raw.get("userId", 0),
            "is_image_only": (
                not raw.get("text")
                and (raw.get("imageUrl") or raw.get("fullImageUrl"))
            ),
        }
        return msg

    def extract_messages(self, page) -> list:
        """Extract all messages from the DOM via JS evaluation."""
        raw_msgs = page.evaluate("""
            () => {
                var items = document.querySelectorAll('#chat_logs_container li.ch_logs');
                var results = [];
                for (var i = 0; i < items.length; i++) {
                    var li = items[i];
                    var msg = {};
                    msg.id = li.getAttribute('data') || '';
                    msg.logId = li.id || '';

                    var ue = li.querySelector('.username');
                    msg.username = ue ? ue.textContent.trim() : '';

                    var de = li.querySelector('.cdate');
                    msg.date = de ? de.textContent.trim() : '';

                    var me = li.querySelector('.chat_message');
                    if (me) {
                        var clone = me.cloneNode(true);
                        clone.querySelectorAll('img').forEach(function(img){img.remove();});
                        msg.text = clone.textContent.trim();
                    } else {
                        msg.text = '';
                    }

                    var ie = li.querySelector('.chat_image');
                    msg.imageUrl = ie ? ie.src : '';

                    var fe = li.querySelector('.fancybox');
                    msg.fullImageUrl = fe ? fe.href : '';

                    var ae = li.querySelector('.avav');
                    msg.avatarUrl = ae ? ae.src : '';

                    var re = li.querySelector('.chat_rank');
                    msg.rank = re ? re.title : '';

                    var le = li.querySelector('.av-level');
                    msg.level = le ? le.textContent.trim() : '0';

                    var oncl = li.getAttribute('onclick') || '';
                    var m = oncl.match(/openAvMenu\\([^,]+,\\s*[^,]+,\\s*(\\d+)/);
                    msg.userId = m ? parseInt(m[1]) : 0;

                    msg.isImageOnly = !msg.text && (msg.imageUrl || msg.fullImageUrl);
                    results.push(msg);
                }
                return results;
            }
        """)

        return [self._parse_message(r) for r in raw_msgs]

    def capture_messages(self, page) -> int:
        """Extract messages, add only new ones. Returns count added."""
        msgs = self.extract_messages(page)
        added = 0
        for m in msgs:
            if m["id"] and m["id"] not in self.seen_ids:
                self.seen_ids.add(m["id"])
                self.messages.append(m)
                added += 1
        return added

    # ----------------------------------------------------------- exports

    def export_json(self):
        if "json" not in self.formats:
            return
        path = self.output_dir / "messages.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.messages, f, indent=2, ensure_ascii=False)
        print(f"  JSON:  {path}  ({len(self.messages)} messages)")

    def export_markdown(self):
        if "md" not in self.formats:
            return
        path = self.output_dir / "messages.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write("# CFWives Chat Export\n\n")
            f.write(f"**Room:** {self.room_name} (`{self.room}`)\n")
            f.write(f"**Exported:** {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(f"**Total Messages:** {len(self.messages)}\n")
            f.write(f"**Images Downloaded:** {self.total_downloaded}\n")
            f.write(f"**Output:** `{self.output_dir}`\n\n---\n\n")
            for m in self.messages:
                f.write(f"## {m['username']} (Level {m['level']}, {m['rank']})\n\n")
                f.write(f"**ID:** `{m['id']}`  \n")
                f.write(f"**Date:** {m['date']}  \n")
                f.write(f"**User ID:** {m.get('user_id', 'N/A')}  \n\n")
                if m["text"]:
                    f.write(f"{m['text']}\n\n")
                if m.get("image_url"):
                    f.write(f"**Image:** [{m['image_url']}]({m['image_url']})\n\n")
                if m.get("full_image_url"):
                    f.write(f"**Full Image:** [{m['full_image_url']}]({m['full_image_url']})\n\n")
                f.write("---\n\n")
        print(f"  MD:    {path}  ({len(self.messages)} messages)")

    def export_users(self):
        users = {}
        for m in self.messages:
            u = m["username"]
            if u not in users:
                users[u] = {"username": u, "count": 0, "first": m["date"],
                            "last": m["date"], "rank": m.get("rank",""), "level": m.get("level","0")}
            users[u]["count"] += 1
            users[u]["last"] = m["date"]
        path = self.output_dir / "users.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(users.values()), f, indent=2, ensure_ascii=False)
        print(f"  Users: {path}  ({len(users)} unique)")

    # ----------------------------------------------------------- main loop

    def run(self):
        print("=" * 60)
        print("  CFWives Chat Stream Exporter (Playwright)")
        print("=" * 60)
        print(f"  Room:     {self.room} ({self.room_name})")
        print(f"  Output:   {self.output_dir}")
        print(f"  Formats:  {', '.join(self.formats)}")
        print(f"  Images:   {self.img_dir}")
        print(f"  Limit:    {self.limit or 'unlimited'}")
        print(f"  Interval: {self.interval}s")
        print(f"  Headless: {self.headless}")
        print(f"  Dry Run:  {self.dry_run}")
        print("=" * 60)

        if self.dry_run:
            print("\n  [DRY RUN — no files will be written or downloaded]\n")

        # Load session cookies if provided
        cookies_list = self.load_cookies()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context_opts = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            if cookies_list:
                context = browser.new_context(**{k: v for k, v in context_opts.items() if k != "cookies"})
                context.add_cookies(cookies_list)
            else:
                context = browser.new_context(**context_opts)

            page = context.new_page()

            # Intercept image requests for metadata tracking
            def on_response(resp):
                ct = resp.headers.get("content-type", "")
                url = resp.url
                if any(e in ct or url.lower().endswith(e)
                       for e in [".jpg", ".jpeg", ".png", ".gif", ".webp", "image/"]):
                    if "cfwives.com" in url and url not in self.image_requests:
                        self.image_requests.append(url)

            page.on("response", on_response)

            try:
                chat_url = f"{self.BASE_URL}/{self.room}"
                print(f"\n  Navigating to {chat_url} ...")
                page.goto(chat_url, wait_until="networkidle", timeout=self.timeout_ms)
                self.log(f"Page loaded: {page.title()}")
                self.log(f"URL: {page.url}")

                # Wait for chat container
                print("  Waiting for chat to initialise ...")
                page.wait_for_selector("#chat_logs_container", timeout=30000)
                print("  Chat container found.")

                # Initial snapshot
                initial = self.capture_messages(page)
                if initial == 0:
                    time.sleep(5)
                    initial = self.capture_messages(page)
                print(f"\n  Initial snapshot: {initial} messages\n")
                print("  Polling for new messages every %.1fs (Ctrl+C to stop).\n" % self.interval)

                cycle = 0
                while self.running:
                    time.sleep(self.interval)
                    cycle += 1
                    added = self.capture_messages(page)
                    if added:
                        print(f"  +{added} new messages (total: {len(self.messages)})")
                    if self.limit and len(self.messages) >= self.limit:
                        print(f"\n  Limit reached ({self.limit}). Stopping.\n")
                        break
                    # Intermediate save every 20 cycles
                    if cycle % 20 == 0 and self.messages:
                        self.export_json()

            except KeyboardInterrupt:
                print("\n  Interrupted by user.")
            except Exception as exc:
                self.log(f"Error: {exc}", force=True)
            finally:
                browser.close()

        # Final exports
        print("\n" + "=" * 60)
        print("  Finalising exports ...")
        print("=" * 60)

        if self.messages:
            self.export_json()
            self.export_markdown()
            self.export_users()
            print(f"\n  All exports → {self.output_dir}/")
        else:
            print("\n  No messages captured.")

        # Download all images
        print("\n" + "=" * 60)
        print("  Downloading images ...")
        print("=" * 60)

        if self.messages:
            all_urls = set()
            for m in self.messages:
                if m.get("image_url"):
                    all_urls.add(m["image_url"])
                if m.get("full_image_url"):
                    all_urls.add(m["full_image_url"])
            for u in self.image_requests:
                if "cfwives.com" in u:
                    all_urls.add(u)

            # Only download chat-related images (user posts + avatars)
            chat_imgs = {
                u for u in all_urls
                if "cfwives.com" in u and ("upload/chat/" in u or "/avatar/" in u)
            }

            print(f"\n  {len(chat_imgs)} unique chat images → {self.img_dir}\n")

            for i, url in enumerate(sorted(chat_imgs), 1):
                self.save_image(url)
                if i % 30 == 0:
                    print(f"  [{i}/{len(chat_imgs)}]")

            if chat_imgs:
                print(f"\n  {self.total_downloaded} images downloaded")
        else:
            print("  Skipping — no messages captured.")

        print("\n" + "=" * 60)
        print(f"  DONE — {len(self.messages)} messages, {self.total_downloaded} images")
        print(f"  Output: {self.output_dir}")
        print("=" * 60)


def main():
    ap = argparse.ArgumentParser(
        description="Export chat stream from cfwives.com"
    )
    ap.add_argument(
        "--room", default="room/1",
        help="Room URL path (default: room/1 = main room)"
    )
    ap.add_argument("--output", default="export", help="Output directory")
    ap.add_argument(
        "--formats", default="json,md",
        help="Comma-separated formats (json,md)"
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Max messages to capture (0 = unlimited)"
    )
    ap.add_argument(
        "--interval", type=float, default=3.0,
        help="DOM poll interval (seconds)"
    )
    ap.add_argument(
        "--no-headless", action="store_true",
        help="Keep browser window visible"
    )
    ap.add_argument(
        "--timeout", type=int, default=60,
        help="Page load timeout (seconds)"
    )
    ap.add_argument(
        "--cookies", default="",
        help="Path to JSON file with session cookies"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print plan only, skip actual downloads"
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="Verbose output to stderr"
    )
    args = ap.parse_args()

    exporter = CFWivesExporter(
        room=args.room,
        output_dir=args.output,
        formats=[x.strip() for x in args.formats.split(",")],
        limit=args.limit,
        interval=args.interval,
        headless=not args.no_headless,
        timeout=args.timeout,
        dry_run=args.dry_run,
        cookies_file=args.cookies,
        verbose=args.verbose,
    )
    exporter.run()


if __name__ == "__main__":
    main()
