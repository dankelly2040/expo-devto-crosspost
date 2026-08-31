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

## Automation (GitHub Actions)

`.github/workflows/crosspost.yml` runs the script daily at 19:00 UTC, and can be
triggered manually from the Actions tab.

The workflow requires these repository secrets. Without them the run fails at the
"Check required secrets" step:

| Secret | Required | Purpose |
| --- | --- | --- |
| `DEVTO_API_KEY` | Yes | Dev.to API key, from dev.to/settings/extensions |
| `ANTHROPIC_API_KEY` | Yes | Claude API key for the rewrite step |
| `ANTHROPIC_WORKSPACE_ID` | Only for identity-linked keys | Workspace the request acts in |
| `DEVTO_ORG_ID` | No | Publish under a Dev.to organization |

An identity-linked Anthropic API key rejects any request that does not name a
workspace, with `anthropic-workspace-id is required`. Set
`ANTHROPIC_WORKSPACE_ID` to the workspace ID (`wrkspc_...`) in that case. A
workspace-scoped key does not need it.

```bash
gh secret set DEVTO_API_KEY -R dankelly2040/expo-devto-crosspost
gh secret set ANTHROPIC_API_KEY -R dankelly2040/expo-devto-crosspost
gh secret set DEVTO_ORG_ID -R dankelly2040/expo-devto-crosspost   # optional
```

The workflow commits `posted.json` back to `main` after each run, so the job needs
write access to repository contents. The workflow declares `permissions: contents:
write` for this. If pushes still fail with a 403, the organization or repository
setting "Workflow permissions" is set to read-only for all workflows and must be
changed in Settings > Actions > General.

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

### Limiting drafts per run

Each run creates at most 5 drafts, so a large backlog is drained over several
days instead of flooding the Dev.to account in one go. Override with
`MAX_POSTS_PER_RUN`:

```bash
MAX_POSTS_PER_RUN=1 python3 crosspost.py
```

For a manual workflow run, set the same limit with the `max_posts` input:

```bash
gh workflow run crosspost.yml -f max_posts=20
```

Scheduled runs always use the default of 5.

### Automated via local cron (alternative to GitHub Actions)

`run.sh` is a wrapper script for cron. Add it to your crontab to run daily at noon PT:

```
# Expo -> Dev.to cross-posting (daily at 12:00 PM PT)
0 12 * * * /path/to/expo-devto-crosspost/run.sh
```

Logs are written to `crosspost.log` in the project directory.

## State tracking

`posted.json` tracks which slugs have been processed to avoid duplicates. It is
committed to the repository so that the GitHub Actions runner, which starts from
a clean checkout each run, carries state between runs.

Slugs are written sorted, and `--dry-run` never writes the file. If `posted.json`
is deleted, the script still checks existing Dev.to articles by canonical URL
before re-posting.

The script has no publish-date filter. Coverage is controlled entirely by which
slugs are recorded here. On 2026-08-31 the file was seeded with every blog post
and changelog entry published before 2026-07-01, so cross-posting starts from a
two-month window rather than the full expo.dev archive.
