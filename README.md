# AI News Pipeline

This repository contains an automated pipeline that surfaces AI news, posts highlights to Slack, and mirrors the editorial state into Markdown files for long-term reference.

## Environment variables

Provide the following variables when running scripts or the GitHub Actions workflow. Suggested defaults are shown below.

```bash
export LOOKBACK_HOURS=12
export MAX_ITEMS=20
export KEYWORDS="artificial intelligence OR generative AI OR LLM OR machine learning"
```

Slack configuration:

- `SLACK_BOT_TOKEN`: Bot OAuth token with permission to post and manage messages.
- `SLACK_CH_DAILY`: Channel ID for daily updates (e.g., `C0123456789`).
- `SLACK_CH_WEEKLY`: Channel ID for weekly recaps.
- `SLACK_CH_MONTHLY`: Channel ID for monthly archive.

GitHub configuration:

- `GITHUB_TOKEN`: Token with `contents` and `pull_request` scopes for the automation bot.
- `GITHUB_REPOSITORY`: `owner/repo` string for the target repository.

Optional enrichment:

- `OPENAI_API_KEY`: Enables semantic enrichment of meaning/impact/affected fields.
- `SUBSTACK_FEEDS`: Comma-separated list of additional RSS feeds to include (used for AI-focused Substack publications).
- `DOMAIN_WEIGHTS_PATH`: Override the path to `domain_weights.yml` for domain-specific scoring tweaks.

## Running locally

1. Install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   The scripts only rely on the Python standard library and `requests`, so you can also `pip install requests` directly.

2. Dry-run the fetcher to preview candidates without posting to Slack:

   ```bash
   LOOKBACK_HOURS=3 python scripts/fetch_and_post_daily.py --dry-run
   ```

   The script writes fresh items into `state/items.jsonl` and, when `PREVIEW_JSON` is set, outputs a JSON preview of the first items that would post.

3. Promote and expire items:

   ```bash
   python scripts/promote_and_expire.py --dry-run
   ```

   This updates Markdown files under `news/` and applies promotion rules without touching Slack.

4. Adjust domain weights in `domain_weights.yml` to tune scoring for preferred publications. Unknown domains default to `0.5`.

5. To sync Markdown content with GitHub via API:

   ```bash
   python scripts/github_agent.py
   ```

   Provide `GITHUB_TOKEN` and `GITHUB_REPOSITORY` to open a pull request. When the token is missing, the agent updates files locally instead.

## GitHub Actions workflow and schema validation

The `.github/workflows/ai-news.yml` workflow runs every 30 minutes. It executes the fetcher and promoter sequentially and commits back any changes under `news/` or `state/`. A concurrency guard ensures only one run happens at a time. Secrets required for Slack and GitHub should be stored in the repository or organization settings.

After content changes land, a dedicated schema validation job executes `scripts/validate_news_schema.py` to assert that every Markdown file includes required front matter fields and uses supported enum values.

## Minimal runbook

- **Secrets rotate**: Update the secrets in GitHub and re-run the workflow manually.
- **Promotion thresholds change**: Modify the constants in `scripts/promote_and_expire.py` and push a new PR.
- **Rollback**: Revert the last automation PR; TTL logic will naturally remove stale Slack posts during the next cycle.
