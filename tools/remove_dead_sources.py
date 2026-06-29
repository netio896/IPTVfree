#!/usr/bin/env python3
"""
Remove dead sources from IPTVfree lists.
Scans all [>] entries in lists/*.md, tests URL connectivity,
and marks failed URLs as [x].

Usage: python3 tools/remove_dead_sources.py [--timeout 10] [--dry-run]
"""

import os
import sys
import re
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import urllib.request
    import urllib.error
except ImportError:
    print("ERROR: urllib required. This script runs in GitHub Actions (Python 3).")
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(description="Detect and mark dead IPTV sources")
    parser.add_argument("--timeout", type=int, default=8, help="Request timeout in seconds (default: 8)")
    parser.add_argument("--dry-run", action="store_true", help="Only report, don't modify files")
    parser.add_argument("--workers", type=int, default=20, help="Max concurrent checks (default: 20)")
    parser.add_argument("--lists-dir", type=str, default=None, help="Override lists directory path")
    return parser.parse_args()


def extract_entries_from_file(filepath):
    """Parse a lists/*.md file and return list of (line_number, line_text, url, marker_pos) for [>] entries."""
    entries = []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if "[>]" not in line:
            continue
        # Extract URL from [>](url) pattern
        match = re.search(r'\[>\]\((https?://[^)]+)\)', line)
        if match:
            url = match.group(1)
            entries.append({
                "line_num": i,
                "line": line,
                "url": url,
                "filepath": filepath,
            })
    return entries


def check_url(url, timeout):
    """Check if a URL is reachable. Returns True if alive, False if dead."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        opener = urllib.request.build_opener()
        response = opener.open(req, timeout=timeout)
        response.close()
        return True
    except Exception:
        pass

    # Fallback: GET with small read limit
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        opener = urllib.request.build_opener()
        response = opener.open(req, timeout=timeout)
        # Read only first 1KB to save bandwidth
        response.read(1024)
        response.close()
        return True
    except Exception:
        return False


def process_entry(entry, timeout):
    """Check a single entry. Returns result dict."""
    url = entry["url"]
    start = time.time()
    try:
        alive = check_url(url, timeout)
        elapsed = round(time.time() - start, 2)
        return {
            "alive": alive,
            "elapsed": elapsed,
            "entry": entry,
        }
    except Exception as e:
        return {
            "alive": False,
            "elapsed": round(time.time() - start, 2),
            "entry": entry,
            "error": str(e),
        }


def mark_dead_in_file(filepath, dead_entries):
    """Replace [>](url) with [x](url) in the given file for the specified entries."""
    if not dead_entries:
        return 0

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    modified = 0
    for entry in dead_entries:
        line_num = entry["line_num"]
        if line_num < len(lines):
            old_line = lines[line_num]
            new_line = re.sub(r'\[>\]\(', '[x](', old_line, count=1)
            if new_line != old_line:
                lines[line_num] = new_line
                modified += 1

    if modified > 0:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    return modified


def main():
    args = parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lists_dir = args.lists_dir or os.path.join(base_dir, "lists")

    if not os.path.isdir(lists_dir):
        print(f"ERROR: lists directory not found: {lists_dir}")
        sys.exit(1)

    # Collect all [>] entries across all .md files
    all_entries = []
    for filename in sorted(os.listdir(lists_dir)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(lists_dir, filename)
        entries = extract_entries_from_file(filepath)
        all_entries.extend(entries)

    if not all_entries:
        print("No [>] entries found. Nothing to check.")
        return

    print(f"Found {len(all_entries)} [>] entries to check across {len(set(e['filepath'] for e in all_entries))} files")
    print(f"Timeout: {args.timeout}s, Workers: {args.workers}\n")

    # Check all URLs concurrently
    results_alive = []
    results_dead = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_entry, entry, args.timeout): entry for entry in all_entries}
        completed = 0

        for future in as_completed(futures):
            completed += 1
            result = future.result()
            entry = result["entry"]
            url_short = entry["url"][:80] + ("..." if len(entry["url"]) > 80 else "")

            if result["alive"]:
                results_alive.append(entry)
                print(f"[OK] ({result['elapsed']}s) {url_short}", file=sys.stderr)
            else:
                results_dead.append(entry)
                print(f"[DEAD] ({result['elapsed']}s) {url_short}", file=sys.stderr)

            # Progress indicator
            sys.stderr.write(f"\rProgress: {completed}/{len(all_entries)}")
            sys.stderr.flush()

    print(f"\n\n{'='*60}")
    print(f"Results: {len(results_alive)} alive, {len(results_dead)} dead out of {len(all_entries)} total")
    print(f"{'='*60}")

    if results_dead:
        print(f"\nDead entries marked for removal:")
        for entry in results_dead:
            url_short = entry["url"][:80] + ("..." if len(entry["url"]) > 80 else "")
            fname = os.path.basename(entry["filepath"])
            print(f"  [{fname}:{entry['line_num']+1}] {url_short}")

        if not args.dry_run:
            # Group dead entries by file
            dead_by_file = {}
            for entry in results_dead:
                fp = entry["filepath"]
                if fp not in dead_by_file:
                    dead_by_file[fp] = []
                dead_by_file[fp].append(entry)

            total_modified = 0
            for filepath, entries in dead_by_file.items():
                mod_count = mark_dead_in_file(filepath, entries)
                fname = os.path.basename(filepath)
                print(f"  Modified {fname}: {mod_count} entries marked as [x]")
                total_modified += mod_count

            print(f"\nTotal files modified: {len(dead_by_file)}, total entries marked: {total_modified}")
            print("Next step: run make_playlist.py to regenerate playlist without dead sources.")
        else:
            print("\n(Dry run: no files were modified)")
    else:
        print("\nAll sources are alive! No changes needed.")


if __name__ == "__main__":
    main()
