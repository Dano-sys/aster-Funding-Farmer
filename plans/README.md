# Plans (report-driven)

This folder holds **human-review plans** generated from Claude advisor outputs.

- **1:1 mapping**: each plan links to a specific advisor JSON (`fly-logs/claude-advisor-*.json`)
  and its rendered Markdown report (`reports/claude-*.md`).
- **No auto-deploy**: plans are proposals only. Nothing in this folder is applied automatically.

## Generate a report + a plan (local)

From repo root:

```bash
bash scripts/generate_report_and_plan.sh
```

This will:

- pull a tail of `/data/funding_farmer.log` from Fly into `fly-logs/`
- run `claude_advisor.py` locally (requires `ANTHROPIC_API_KEY`)
- write a Markdown report under `reports/`
- write a plan under `plans/`

