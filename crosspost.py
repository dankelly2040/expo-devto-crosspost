#!/usr/bin/env python3
"""
Cross-posts new Expo blog posts and changelogs to Dev.to.
Checks the expo.dev/blog RSS feed and changelog index, compares against
published Dev.to articles, and publishes any new content.
"""

import json
import os
import re
import sys
import time
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXPO_RSS_URL = "https://expo.dev/blog/rss.xml"
EXPO_BLOG_BASE = "https://expo.dev/blog"
EXPO_CHANGELOG_BASE = "https://expo.dev/changelog"
DEVTO_API_BASE = "https://dev.to/api"

# Load .env file if present (for cron environments)
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

DEVTO_API_KEY = os.environ.get("DEVTO_API_KEY", "")
DEVTO_ORG_ID = os.environ.get("DEVTO_ORG_ID", "")  # optional
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
STATE_FILE = os.path.join(os.path.dirname(__file__), "posted.json")
DRY_RUN = "--dry-run" in sys.argv

# Dev.to tag mapping (max 4 tags per post)
TAG_MAP = {
    "React Native": "reactnative",
    "Development": "javascript",
    "Product": "mobile",
    "Users": "mobile",
    "AI": "ai",
    "Security notices": "security",
}
DEFAULT_TAGS = ["reactnative", "mobile", "javascript", "expo"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"posted_slugs": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_url(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "ExpoDevtoCrosspost/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def fetch_json(url, headers=None, method="GET", data=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    req.add_header("User-Agent", "ExpoDevtoCrosspost/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# RSS feed parsing
# ---------------------------------------------------------------------------

def fetch_rss_posts():
    """Returns list of dicts: {title, slug, link, pub_date, description, thumbnail}"""
    raw = fetch_url(EXPO_RSS_URL)

    # Fix unescaped & in URLs (common RSS issue with query params)
    # Replace & that isn't already &amp; &lt; &gt; &quot; &apos; or a numeric ref
    raw = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', raw)

    # Register media namespace so ET can parse it
    namespaces = {"media": "http://search.yahoo.com/mrss/"}
    for prefix, uri in namespaces.items():
        ET.register_namespace(prefix, uri)

    root = ET.fromstring(raw)
    posts = []
    for item in root.findall(".//item"):
        link = item.findtext("link", "")
        slug = link.rstrip("/").split("/")[-1] if link else ""
        pub_date_str = item.findtext("pubDate", "")

        # Parse authors
        authors = [a.text for a in item.findall("author") if a.text]

        # Parse thumbnail (media:thumbnail with namespace)
        thumb = None
        thumb_el = item.find("{http://search.yahoo.com/mrss/}thumbnail")
        if thumb_el is not None:
            thumb = thumb_el.get("url")

        posts.append({
            "title": item.findtext("title", ""),
            "slug": slug,
            "link": link,
            "pub_date": pub_date_str,
            "description": item.findtext("description", ""),
            "authors": authors,
            "thumbnail": thumb,
        })
    return posts


# ---------------------------------------------------------------------------
# Blog content extraction
# ---------------------------------------------------------------------------

def fetch_blog_content(slug):
    """Fetch a blog post page and extract the Portable Text body from embedded JSON."""
    url = f"{EXPO_BLOG_BASE}/{slug}"
    page_html = fetch_url(url)

    # Extract the __EXPO_ROUTER_LOADER_DATA__ JSON
    # It's stored as: globalThis.__EXPO_ROUTER_LOADER_DATA__ = JSON.parse("...");
    pattern = r'globalThis\.__EXPO_ROUTER_LOADER_DATA__\s*=\s*JSON\.parse\("(.+?)"\);\s*</script>'
    match = re.search(pattern, page_html, re.DOTALL)
    if not match:
        log(f"  Could not find loader data for {slug}")
        return None

    try:
        # The escaped_json is a JS string literal content (with \" etc.)
        # Wrap it in quotes and use json.loads to unescape the JS string,
        # then parse the result as JSON
        escaped_json = match.group(1)
        # json.loads('"..."') will properly unescape a JSON string literal
        raw_json = json.loads(f'"{escaped_json}"')
        loader_data = json.loads(raw_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log(f"  Failed to parse loader data JSON for {slug}: {e}")
        return None

    # The post data is under a key like "/blog/{slug}"
    post_data = None
    for key, value in loader_data.items():
        if isinstance(value, dict) and "post" in value:
            post_data = value["post"]
            break

    return post_data


# ---------------------------------------------------------------------------
# Changelog content extraction
# ---------------------------------------------------------------------------

def fetch_changelog_list():
    """Fetch the changelog index and return list of {title, slug, link, pub_date, authors}."""
    page_html = fetch_url(EXPO_CHANGELOG_BASE)

    # Changelog uses React Server Components (RSC) format
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page_html, re.DOTALL
    )
    for chunk in chunks:
        if "initialPosts" not in chunk:
            continue
        unescaped = chunk.encode().decode("unicode_escape")
        idx = unescaped.find('"initialPosts"')
        if idx < 0:
            continue
        obj_start = unescaped.rfind("{", 0, idx)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(unescaped[obj_start:])
        posts = data.get("initialPosts", [])

        results = []
        for p in posts:
            slug_val = p.get("slug", "")
            if isinstance(slug_val, dict):
                slug_val = slug_val.get("current", "")
            authors = [
                a.get("name", "") for a in p.get("authors", []) if isinstance(a, dict)
            ]
            results.append({
                "title": p.get("title", ""),
                "slug": f"changelog-{slug_val}",  # prefix to avoid collision with blog slugs
                "raw_slug": slug_val,
                "link": f"{EXPO_CHANGELOG_BASE}/{slug_val}",
                "pub_date": p.get("publishAt", ""),
                "authors": authors,
                "source": "changelog",
            })
        return results

    return []


def fetch_changelog_content(raw_slug):
    """Fetch a changelog post page and extract the Portable Text body from RSC data."""
    url = f"{EXPO_CHANGELOG_BASE}/{raw_slug}"
    page_html = fetch_url(url)

    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page_html, re.DOTALL
    )
    for chunk in chunks:
        if "changelogPost" not in chunk:
            continue
        unescaped = chunk.encode().decode("unicode_escape")
        idx = unescaped.find('{"changelogPost"')
        if idx < 0:
            continue
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(unescaped[idx:])
        return data.get("changelogPost")

    return None


# ---------------------------------------------------------------------------
# Portable Text -> Markdown conversion
# ---------------------------------------------------------------------------

def portable_text_to_markdown(body, mark_defs_map=None):
    """Convert Sanity Portable Text blocks to Markdown."""
    if not body:
        return ""

    lines = []
    for block in body:
        block_type = block.get("_type", "")

        if block_type == "block":
            md = convert_block(block)
            if md is not None:
                lines.append(md)

        elif block_type == "codeBlock":
            lang = block.get("language", "")
            text = block.get("text", "")
            filename = block.get("fileName", "")
            header = f"```{lang}" if lang else "```"
            if filename:
                header += f"\n// {filename}"
            lines.append(f"{header}\n{text}\n```")

        elif block_type == "customImage":
            img_url = block.get("imageUrl", "")
            alt = block.get("altText", "")
            caption = block.get("caption", "")
            if img_url:
                lines.append(f"![{alt}]({img_url})")
                if caption:
                    lines.append(f"*{caption}*")

        elif block_type == "callout":
            # Callout blocks often have a body field
            callout_body = block.get("body", [])
            if callout_body:
                callout_md = portable_text_to_markdown(callout_body)
                lines.append(f"> {callout_md.replace(chr(10), chr(10) + '> ')}")

        elif block_type == "table":
            rows = block.get("rows", [])
            if rows:
                lines.append(convert_table(rows))

        # Skip unknown block types silently

    return "\n\n".join(lines)


def convert_block(block):
    """Convert a single Portable Text block to markdown."""
    style = block.get("style", "normal")
    children = block.get("children", [])
    mark_defs = {m["_key"]: m for m in block.get("markDefs", [])}
    list_item = block.get("listItem")
    level = block.get("level", 1)

    # Build inline text
    text = ""
    for child in children:
        if child.get("_type") == "span":
            span_text = child.get("text", "")
            marks = child.get("marks", [])
            span_text = apply_marks(span_text, marks, mark_defs)
            text += span_text

    if not text.strip() and style == "normal" and not list_item:
        return ""

    # Apply block-level formatting
    if list_item == "bullet":
        indent = "  " * (level - 1)
        return f"{indent}- {text}"
    elif list_item == "number":
        indent = "  " * (level - 1)
        return f"{indent}1. {text}"

    if style == "h1":
        return f"# {text}"
    elif style == "h2":
        return f"## {text}"
    elif style == "h3":
        return f"### {text}"
    elif style == "h4":
        return f"#### {text}"
    elif style == "blockquote":
        return f"> {text}"
    else:
        return text


def apply_marks(text, marks, mark_defs):
    """Apply inline marks (bold, italic, code, links) to text."""
    if not text or not marks:
        return text

    for mark in marks:
        if mark == "strong":
            text = f"**{text}**"
        elif mark == "em":
            text = f"*{text}*"
        elif mark == "code":
            text = f"`{text}`"
        elif mark == "underline":
            text = f"<u>{text}</u>"
        elif mark == "strikethrough":
            text = f"~~{text}~~"
        elif mark in mark_defs:
            defn = mark_defs[mark]
            if defn.get("_type") == "link":
                href = defn.get("href", "")
                text = f"[{text}]({href})"

    return text


def convert_table(rows):
    """Convert Sanity table rows to markdown table."""
    if not rows:
        return ""

    md_rows = []
    for i, row in enumerate(rows):
        cells = row.get("cells", [])
        cell_texts = []
        for cell in cells:
            if isinstance(cell, str):
                cell_texts.append(cell)
            elif isinstance(cell, dict):
                # Could be a block
                cell_texts.append(str(cell.get("text", "")))
            else:
                cell_texts.append(str(cell))
        md_rows.append("| " + " | ".join(cell_texts) + " |")

        # Add header separator after first row
        if i == 0:
            md_rows.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")

    return "\n".join(md_rows)


# ---------------------------------------------------------------------------
# Content rewriting via Claude API
# ---------------------------------------------------------------------------

REWRITE_SYSTEM_PROMPT = """\
You are drafting a Dev.to post based on an Expo blog post. The original was \
published on expo.dev. Your draft must be a semantic variation: same technical \
content and depth, but restructured and reworded so it stands as independent \
content for the Dev.to audience.

## Post classification

Classify the input and adapt structure accordingly:

- **Single feature launch** (600-900 words): New API, package, or capability. \
Structure: problem statement, the news in one line, payoff with a number if \
available, "what is it" section, "how it works" section, "how to use it" \
section with code, limitations if any, closing CTA.

- **Major feature roundup** (1400-2200 words): Multiple new things in a release. \
Structure: hook + framing, one H2 per feature with prose lede, visual, code, \
docs link. Closing with "where to start" 3-bullet recap.

- **Educational guide** (1800-3000 words): Best practices not tied to a release. \
Structure: problem for the role, promise of what they'll learn, 3-7 H2 \
sections each a principle with examples, closing with next step.

- **Bug fix or perf note** (400-700 words): Tight, no marketing intro. What \
broke, what we did, what changed.

Pick the right structure. Don't pad to hit a length target.

## Voice and tone

Direct, technically literal, lightly playful. Confidence without hype. Sound \
like an engineer sharing something useful with another engineer over coffee. \
Never sound like a company announcing something to its users.

Empathetic, not performative. The problem statement should be specific enough \
that the right reader thinks "that happened to me last Tuesday." Not \
"developers often struggle with slow builds" (market research voice) but \
"You're waiting 14 minutes for a build that used to take 3" (engineer voice).

Name the mechanism, not just the outcome. "Faster builds" is a claim. \
"Pre-configured cloud workers maintained by the team that builds the React \
Native framework" is a mechanism.

Honest about beta status, limitations, and rough edges. Include a \
"Limitations" section when the feature has them.

## Intro pattern

1. A moment the reader recognizes (a specific scenario a real developer has \
lived through, not "developers often struggle with X")
2. The news in one line
3. The payoff with a number, bolded if quantified
4. Beta/availability flag + "here's how it works"

No throat-clearing. The first sentence is the scenario or the news.

## Closing pattern

Never just stop. End with one of:
- Point to docs, changelog, or migration guide (for readers who already use Expo)
- "Where to start" section with 3-bullet recap + concrete first step (for newer users)
- "Feedback" section for beta features (what's coming next, where to report issues)

End with: "This post is based on content from the [Expo blog](SOURCE_URL). \
Follow [@expo](https://dev.to/expo) for more React Native content."

## What you must preserve exactly

- Every __CODE_BLOCK_N__ placeholder must appear in your output exactly as-is, \
in the same logical position relative to the surrounding prose. Never modify, \
remove, reorder, or add placeholders.
- All inline code (text in backticks like `useState`) must stay verbatim.
- All image markdown (![alt](url)) must stay verbatim.
- All URLs in links must stay verbatim (you can change link text).
- All version numbers, API names, product names, benchmark numbers.
- Product name casing: Expo, Expo SDK, EAS Update, EAS Build, EAS Workflows, \
EAS Hosting, EAS Submit, Expo Router, Expo Orbit, Expo Go.

## Banned constructions (mandatory)

Never use these patterns:
- Em dashes (use commas, periods, colons, or parentheses instead)
- Triple constructions ("It's fast, scalable, and open source")
- Staccato bursts ("This matters. It always has. And it always will.")
- Throat-clearing openers ("In today's rapidly evolving landscape...")
- Pivot paragraphs ("But here's where it gets interesting.")
- Question-then-answer ("So what does this mean? It means everything.")
- Balanced takes ("While X has drawbacks, it also offers benefits.")
- Three-word marketing taglines as section headers ("Built for scale")
- Title case in headers (use sentence case throughout)

## Banned words and phrases (mandatory)

Never use:
- crucial, vital, robust, comprehensive, fundamental, arguably, straightforward
- leverage (use "use"), delve (use "look at"), utilize (use "use")
- facilitate (use "help"), transform (use "change"), craft (use "make")
- multifaceted, nuanced, pivotal, unprecedented, seamlessly
- navigate, foster, underscores, resonates, embark, streamline, spearhead
- "it's important to note", "it's worth noting", "game-changer", "revolutionary"
- "In an era of...", "broader implications"
- "Furthermore", "Moreover", "In conclusion"

Register: write one level below where instinct says. "Demonstrate" becomes \
"show." "Facilitate" becomes "help." Match the register of an engineer \
talking to another engineer.

Vary sentence length. Vary paragraph length. Don't start consecutive \
paragraphs with transition words. Cut unnecessary elaboration.

## Self-check before output

- H1 is sentence case, contains the feature name, no clickbait
- Problem statement appears within the first 2 paragraphs
- Every named product/API has a link on first mention
- Code blocks are preserved via placeholders
- Limitations section exists if the feature is beta or has caveats
- Closing section names a next action and links to it
- No banned phrases anywhere
- Sentence case throughout, except product names
- Mechanism test: every major technical claim names how Expo achieves it
- Voice test: does it sound like an engineer sharing something useful, \
or a company making an announcement? If the latter, rewrite.

## Output format

Return ONLY the rewritten markdown. Start with a # title. No commentary, \
no explanations, no metadata blocks outside the content.
"""


def split_prose_and_code(markdown):
    """Strip fenced code blocks, replacing with placeholders. Returns (prose, blocks)."""
    code_blocks = []
    counter = [0]

    def replacer(match):
        idx = counter[0]
        counter[0] += 1
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{idx}__"

    prose = re.sub(r"```[\s\S]*?```", replacer, markdown)
    return prose, code_blocks


def reassemble_content(rewritten_prose, code_blocks):
    """Substitute code block placeholders back in. Returns (result, success)."""
    result = rewritten_prose
    for i, block in enumerate(code_blocks):
        placeholder = f"__CODE_BLOCK_{i}__"
        if placeholder not in result:
            return None, False
        result = result.replace(placeholder, block, 1)
    return result, True


def rewrite_via_claude(markdown, title, description, source_url):
    """Rewrite markdown content using the Claude API. Returns (new_title, new_markdown) or None."""
    if not ANTHROPIC_API_KEY:
        log("  WARNING: ANTHROPIC_API_KEY not set, skipping rewrite")
        return None

    try:
        import anthropic
    except ImportError:
        log("  WARNING: anthropic package not installed, skipping rewrite")
        return None

    # Split code blocks out
    prose, code_blocks = split_prose_and_code(markdown)
    log(f"  Extracted {len(code_blocks)} code block(s) for preservation")

    # Build the prompt
    system = REWRITE_SYSTEM_PROMPT.replace("SOURCE_URL", source_url)
    user_msg = (
        f"Original title: {title}\n"
        f"Original description: {description}\n"
        f"Source: {source_url}\n\n"
        f"Rewrite the following blog post for Dev.to:\n\n{prose}"
    )

    # Call Claude with retries
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    last_error = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6-20250627",
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            rewritten_prose = response.content[0].text
            break
        except Exception as e:
            last_error = e
            log(f"  Claude API attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
    else:
        log(f"  Claude API failed after 3 attempts: {last_error}")
        return None

    # Reassemble code blocks
    rewritten_full, success = reassemble_content(rewritten_prose, code_blocks)
    if not success:
        log("  ERROR: Code block placeholders were lost during rewrite, rejecting")
        return None

    # Validate length (60-150% of original)
    orig_len = len(markdown)
    new_len = len(rewritten_full)
    ratio = new_len / orig_len if orig_len > 0 else 0
    if ratio < 0.4 or ratio > 2.0:
        log(f"  ERROR: Rewrite length ratio {ratio:.1%} is outside bounds, rejecting")
        return None

    # Extract new title from the rewritten content (first # heading)
    title_match = re.match(r"^#\s+(.+)$", rewritten_full, re.MULTILINE)
    new_title = title_match.group(1).strip() if title_match else title
    # Remove the title line from the body (Dev.to adds title separately)
    if title_match:
        rewritten_full = rewritten_full[title_match.end():].lstrip("\n")

    return new_title, rewritten_full


# ---------------------------------------------------------------------------
# Dev.to API
# ---------------------------------------------------------------------------

def get_devto_articles():
    """Fetch all articles (published and drafts) from the Dev.to account."""
    if not DEVTO_API_KEY:
        return []

    articles = []
    page = 1
    while True:
        url = f"{DEVTO_API_BASE}/articles/me/all?page={page}&per_page=100"
        try:
            batch = fetch_json(url, headers={"api-key": DEVTO_API_KEY})
        except urllib.error.HTTPError as e:
            log(f"  Error fetching Dev.to articles: {e}")
            break
        if not batch:
            break
        articles.extend(batch)
        page += 1
        if len(batch) < 100:
            break

    return articles


def get_devto_canonical_urls():
    """Get set of canonical URLs already published on Dev.to."""
    articles = get_devto_articles()
    urls = set()
    for a in articles:
        canonical = a.get("canonical_url", "")
        if canonical:
            urls.add(canonical.rstrip("/"))
        # Also track by slug in the URL
        url = a.get("url", "")
        if url:
            urls.add(url.rstrip("/"))
    return urls


def select_tags(categories):
    """Map Expo blog categories to Dev.to tags (max 4)."""
    tags = []
    if categories:
        for cat in categories:
            name = cat.get("name", "") if isinstance(cat, dict) else str(cat)
            mapped = TAG_MAP.get(name)
            if mapped and mapped not in tags:
                tags.append(mapped)

    # Always include expo
    if "expo" not in tags:
        tags.insert(0, "expo")

    return tags[:4]


def publish_to_devto(title, markdown, description, tags, cover_image=None,
                     canonical_url=None, published=False):
    """Publish an article to Dev.to."""
    if not DEVTO_API_KEY:
        log("  ERROR: DEVTO_API_KEY not set")
        return None

    article_data = {
        "article": {
            "title": title,
            "body_markdown": markdown,
            "published": published,
            "description": description[:150] if description else "",
            "tags": tags,
        }
    }

    if canonical_url:
        article_data["article"]["canonical_url"] = canonical_url

    if cover_image:
        article_data["article"]["main_image"] = cover_image

    if DEVTO_ORG_ID:
        article_data["article"]["organization_id"] = int(DEVTO_ORG_ID)

    if DRY_RUN:
        log(f"  [DRY RUN] Would publish: {title}")
        log(f"  Tags: {tags}")
        log(f"  Canonical: {canonical_url or 'none (original content)'}")
        log(f"  Published: {published}")
        log(f"  Body length: {len(markdown)} chars")
        return {"id": "dry-run", "url": "dry-run"}

    try:
        result = fetch_json(
            f"{DEVTO_API_BASE}/articles",
            headers={"api-key": DEVTO_API_KEY},
            method="POST",
            data=article_data,
        )
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        log(f"  ERROR publishing to Dev.to: {e} - {body}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_blog_post(post, posted_slugs, existing_urls):
    """Process, rewrite, and publish a single blog post. Returns True if published."""
    slug = post["slug"]
    log(f"\nProcessing blog: {post['title']}")

    post_data = fetch_blog_content(slug)
    if not post_data:
        log(f"  Skipping {slug}: could not extract content")
        return False

    body = post_data.get("body", [])
    markdown = portable_text_to_markdown(body)
    if not markdown.strip():
        log(f"  Skipping {slug}: empty markdown conversion")
        return False

    categories = post_data.get("categories", [])
    tags = select_tags(categories)

    main_image = post_data.get("mainImage", {})
    cover_image = main_image.get("imageUrl") if isinstance(main_image, dict) else None
    if not cover_image:
        cover_image = post.get("thumbnail")

    description = post_data.get("metadataDescription", post.get("description", ""))

    # Rewrite content via Claude API
    rewrite_result = rewrite_via_claude(
        markdown=markdown,
        title=post["title"],
        description=description,
        source_url=post["link"],
    )

    if rewrite_result is None:
        log(f"  Skipping {slug}: rewrite failed")
        return False

    new_title, rewritten_markdown = rewrite_result
    log(f"  Rewritten: '{post['title']}' -> '{new_title}'")

    # Publish as draft with no canonical URL (original content)
    result = publish_to_devto(
        title=new_title,
        markdown=rewritten_markdown,
        description=description,
        tags=tags,
        cover_image=cover_image,
        published=False,
    )
    if result:
        draft_url = result.get("url", "draft")
        log(f"  Draft created: {draft_url}")
        return True
    log(f"  Failed to publish {slug}")
    return False


def process_changelog(post, posted_slugs, existing_urls):
    """Process, rewrite, and publish a single changelog entry. Returns True if published."""
    raw_slug = post["raw_slug"]
    log(f"\nProcessing changelog: {post['title']}")

    post_data = fetch_changelog_content(raw_slug)
    if not post_data:
        log(f"  Skipping {raw_slug}: could not extract content")
        return False

    body = post_data.get("body", [])
    markdown = portable_text_to_markdown(body)
    if not markdown.strip():
        log(f"  Skipping {raw_slug}: empty markdown conversion")
        return False

    tags = ["expo", "reactnative", "mobile", "javascript"]

    main_image = post_data.get("mainImage", {})
    cover_image = main_image.get("imageUrl") if isinstance(main_image, dict) else None

    description = post_data.get("metadataDescription", "")

    # Rewrite content via Claude API
    rewrite_result = rewrite_via_claude(
        markdown=markdown,
        title=post["title"],
        description=description,
        source_url=post["link"],
    )

    if rewrite_result is None:
        log(f"  Skipping {raw_slug}: rewrite failed")
        return False

    new_title, rewritten_markdown = rewrite_result
    log(f"  Rewritten: '{post['title']}' -> '{new_title}'")

    # Publish as draft with no canonical URL (original content)
    result = publish_to_devto(
        title=new_title,
        markdown=rewritten_markdown,
        description=description,
        tags=tags,
        cover_image=cover_image,
        published=False,
    )
    if result:
        draft_url = result.get("url", "draft")
        log(f"  Draft created: {draft_url}")
        return True
    log(f"  Failed to publish {raw_slug}")
    return False


def main():
    log("Starting Expo -> Dev.to cross-post check")

    if not DEVTO_API_KEY and not DRY_RUN:
        log("ERROR: DEVTO_API_KEY environment variable not set")
        sys.exit(1)

    # Load state
    state = load_state()
    posted_slugs = set(state.get("posted_slugs", []))

    # Check what's already on Dev.to
    if not DRY_RUN:
        log("Fetching existing Dev.to articles...")
        existing_urls = get_devto_canonical_urls()
        log(f"  Found {len(existing_urls)} existing articles on Dev.to")
    else:
        existing_urls = set()

    # --- Blog posts ---
    log("Fetching Expo blog RSS feed...")
    rss_posts = fetch_rss_posts()
    log(f"  Found {len(rss_posts)} blog posts in RSS feed")

    new_blog_posts = []
    for post in rss_posts:
        slug = post["slug"]
        canonical = post["link"].rstrip("/")
        post["source"] = "blog"
        if slug in posted_slugs:
            continue
        if canonical in existing_urls:
            posted_slugs.add(slug)
            continue
        new_blog_posts.append(post)

    # --- Changelogs ---
    log("Fetching Expo changelog index...")
    changelog_posts = fetch_changelog_list()
    log(f"  Found {len(changelog_posts)} changelog entries")

    new_changelogs = []
    for post in changelog_posts:
        slug = post["slug"]  # prefixed with "changelog-"
        canonical = post["link"].rstrip("/")
        if slug in posted_slugs:
            continue
        if canonical in existing_urls:
            posted_slugs.add(slug)
            continue
        new_changelogs.append(post)

    # --- Combine and process ---
    all_new = new_blog_posts + new_changelogs
    if not all_new:
        log("No new content to cross-post.")
        save_state({"posted_slugs": list(posted_slugs)})
        return

    log(f"Found {len(new_blog_posts)} new blog post(s) and {len(new_changelogs)} new changelog(s)")

    for post in all_new:
        slug = post["slug"]
        if post.get("source") == "changelog":
            success = process_changelog(post, posted_slugs, existing_urls)
        else:
            success = process_blog_post(post, posted_slugs, existing_urls)

        if success:
            posted_slugs.add(slug)

    # Save state
    save_state({"posted_slugs": list(posted_slugs)})
    log("\nDone.")


if __name__ == "__main__":
    main()
