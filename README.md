# Expo to Dev.to cross-poster

Automatically cross-posts new content from the [Expo blog](https://expo.dev/blog) and [changelog](https://expo.dev/changelog) to [Dev.to](https://dev.to/expo) as drafts.

Each post is rewritten via the Claude API to create original, Dev.to-optimized content while preserving all code blocks, links, and technical accuracy.

## How it works

1. Fetches the Expo blog RSS feed and changelog index
2. Compares against local state (`posted.json`) and existing Dev.to articles to skip duplicates
3. Extracts full post content from Expo's embedded page data
4. Converts Sanity Portable Text to Markdown
5. Rewrites prose via Claude API (code blocks are extracted, preserved, and reassembled)
6. Publishes as a draft to Dev.to for review before going live

## Setup

### Requirements

- Python 3.9+
- `anthropic` Python package

### Environment variables

Create a `.env` file in the project root:

```
DEVTO_API_KEY=your_dev_to_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
DEVTO_ORG_ID=your_org_id  # optional, for publishing under an organization
```

### Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install anthropic
```

## Usage

### Manual run

```bash
source .venv/bin/activate
python3 crosspost.py
```

### Dry run (no publishing)

```bash
python3 crosspost.py --dry-run
```

### Automated via cron

`run.sh` is a wrapper script for cron. Add it to your crontab to run daily at noon PT:

```
# Expo -> Dev.to cross-posting (daily at 12:00 PM PT)
0 12 * * * /path/to/expo-devto-crosspost/run.sh
```

Logs are written to `crosspost.log` in the project directory.

## State tracking

`posted.json` tracks which slugs have been processed to avoid duplicates. This file is gitignored. If deleted, the script will also check existing Dev.to articles by canonical URL before re-posting.
