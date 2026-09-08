#!/usr/bin/env python3
"""
One-off: set the cover image on existing Dev.to articles that have none.

Every draft created between 2026-07-01 and 2026-09-08 was posted with the
cover in the wrong API field, so Dev.to ignored it. This script walks the
account's articles, resolves the cover from the canonical expo.dev page the
same way crosspost.py does for new drafts, and sets it with the update
endpoint.

Dry run by default. Pass --apply to write. Published articles are skipped
unless --include-published is given, because changing their cover is visible
to readers immediately.
"""

import sys
import time
import urllib.error
import urllib.parse

from crosspost import (
    DEVTO_API_BASE,
    DEVTO_API_KEY,
    EXPO_BLOG_BASE,
    EXPO_CHANGELOG_BASE,
    fetch_blog_content,
    fetch_changelog_content,
    fetch_json,
    fetch_rss_posts,
    get_devto_articles,
    log,
)

APPLY = "--apply" in sys.argv
INCLUDE_PUBLISHED = "--include-published" in sys.argv

# Pause between writes. Dev.to rate-limits article writes per account.
WRITE_DELAY_SECONDS = 1.5


def resolve_cover(canonical_url, rss_thumbnails):
    """Return a cover URL for an expo.dev blog or changelog URL, or None."""
    canonical = canonical_url.rstrip("/")
    parsed = urllib.parse.urlparse(canonical)
    if parsed.netloc != "expo.dev":
        return None

    if canonical.startswith(EXPO_BLOG_BASE + "/"):
        slug = canonical[len(EXPO_BLOG_BASE) + 1 :]
        post = fetch_blog_content(slug) or {}
        main_image = post.get("mainImage") or {}
        cover = main_image.get("imageUrl") if isinstance(main_image, dict) else None
        return cover or rss_thumbnails.get(slug)

    if canonical.startswith(EXPO_CHANGELOG_BASE + "/"):
        raw_slug = canonical[len(EXPO_CHANGELOG_BASE) + 1 :]
        post = fetch_changelog_content(raw_slug) or {}
        main_image = post.get("mainImage") or {}
        cover = main_image.get("imageUrl") if isinstance(main_image, dict) else None
        return cover or post.get("ogImageUrl")

    return None


def set_cover(article_id, cover):
    """PUT the cover onto one article. Retries once on a 429."""
    url = f"{DEVTO_API_BASE}/articles/{article_id}"
    payload = {"article": {"main_image": cover}}
    for attempt in range(2):
        try:
            fetch_json(url, headers={"api-key": DEVTO_API_KEY}, method="PUT", data=payload)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            if e.code == 429 and attempt == 0:
                log("    Rate limited, waiting 30s")
                time.sleep(30)
                continue
            log(f"    ERROR updating article {article_id}: {e} - {body[:200]}")
            return False
    return False


def main():
    if not DEVTO_API_KEY:
        log("ERROR: DEVTO_API_KEY environment variable not set")
        sys.exit(1)

    log("Backfilling cover images" + ("" if APPLY else " (dry run, pass --apply to write)"))

    articles = get_devto_articles()
    log(f"Found {len(articles)} articles on the account")

    try:
        rss_thumbnails = {p["slug"]: p["thumbnail"] for p in fetch_rss_posts() if p.get("thumbnail")}
    except Exception as e:
        log(f"WARNING: could not load RSS thumbnails: {e}")
        rss_thumbnails = {}

    updated = unresolved = skipped_cover = skipped_published = skipped_other = failed = 0

    for a in articles:
        article_id = a.get("id")
        title = a.get("title", "")
        canonical = a.get("canonical_url") or ""
        published = bool(a.get("published", a.get("published_at")))

        if a.get("cover_image"):
            skipped_cover += 1
            continue
        if not canonical.startswith("https://expo.dev/"):
            skipped_other += 1
            continue
        if published and not INCLUDE_PUBLISHED:
            log(f"Skipping published article without cover: {title!r} ({canonical})")
            skipped_published += 1
            continue

        try:
            cover = resolve_cover(canonical, rss_thumbnails)
        except Exception as e:
            log(f"ERROR resolving cover for {canonical}: {e}")
            cover = None
        if not cover:
            log(f"No cover found for {title!r} ({canonical})")
            unresolved += 1
            continue

        state = "published" if published else "draft"
        log(f"{'Setting' if APPLY else 'Would set'} cover on {state} {article_id} {title!r}")
        log(f"    {cover}")
        if not APPLY:
            updated += 1
            continue
        if set_cover(article_id, cover):
            updated += 1
        else:
            failed += 1
        time.sleep(WRITE_DELAY_SECONDS)

    verb = "updated" if APPLY else "would update"
    log(
        f"\nDone. {updated} {verb}, {failed} failed, {unresolved} without a resolvable cover, "
        f"{skipped_cover} already had a cover, {skipped_published} published skipped, "
        f"{skipped_other} not from expo.dev."
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
