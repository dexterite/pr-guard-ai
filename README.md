# PR Guard AI

**AI-powered code review for pull requests** — security, quality, and compliance checks using any OpenAI-compatible LLM.

Language-agnostic. Extensible. Zero config to start, fully configurable when you need it.

---

## Features

| Check | Purpose |
|-------|---------|
| **code-quality** | Complexity, readability, DRY, error handling, dead code |
| **sast** | OWASP Top 10, injection, auth flaws, crypto issues |
| **secret-detection** | API keys, passwords, tokens, private keys, high-entropy strings |
| **iac-security** | Terraform, CloudFormation, K8s, Bicep, Ansible misconfigs |
| **container-security** | Dockerfile best practices, privilege escalation, supply chain |

All checks are:
- **Language-agnostic** — works with any programming language or framework
- **Grouped by purpose** — each in its own folder under `checks/`
- **Independently configurable** — enable, disable, or customize each check
- **Extensible** — add your own checks by dropping a folder with `prompt.md` + `config.yml`

## Quick Start

```yaml
# .github/workflows/pr-guard.yml
name: PR Guard AI
on:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: dexterite/pr-guard-ai@main
        with:
          api-key: ${{ secrets.OPENAI_API_KEY }}
```

That's it. All 5 checks run on changed files and the report appears in the Actions step summary.

## Inputs

| Input | Required | Default | Description |
|-------|:--------:|---------|-------------|
| `api-key` | **yes** | — | AI API key (OpenAI-compatible). **Store as a secret.** |
| `api-base-url` | no | `https://api.openai.com/v1` | Base URL — works with Azure OpenAI, Ollama, vLLM, LiteLLM |
| `model` | no | `gpt-4o` | Model identifier |
| `checks` | no | `all` | Comma-separated: `code-quality,sast,secret-detection,iac-security,container-security` |
| `config-file` | no | — | Path to `pr-guard.config.yml` |
| `custom-checks-dir` | no | — | Directory with custom check folders |
| `full-scan` | no | `false` | Scan **all** tracked files regardless of git diff (overrides `diff-only`) |
| `diff-only` | no | `true` | Only analyze changed files |
| `severity-threshold` | no | `high` | Exit non-zero if findings >= this level (`info\|low\|medium\|high\|critical`) |
| `output-format` | no | `markdown` | `markdown`, `json`, or `sarif` |
| `ship-to` | no | `github-summary` | Destinations (comma-separated): `github-summary`, `file`, `webhook`, `github-pr-comment` |
| `ship-webhook-url` | no | — | Webhook endpoint URL |
| `ship-file-path` | no | `pr-guard-report` | Base path for file output |
| `max-file-size-kb` | no | `100` | Skip files larger than this |
| `max-context-tokens` | no | `100000` | Token budget per AI call |
| `exclude-patterns` | no | — | Comma-separated globs to exclude |
| `github-token` | no | — | For PR comments (`secrets.GITHUB_TOKEN`) |
| `request-delay-ms` | no | `0` | Minimum delay (ms) between API calls; also auto-ramps on 429 |
| `debug` | no | `false` | Verbose logging (git commands, file filtering, AI responses) |

## Outputs

| Output | Description |
|--------|-------------|
| `findings-count` | Total findings across all checks |
| `critical-count` | Number of critical-severity findings |
| `report-path` | Path to report file (when `ship-to` includes `file`) |
| `exit-code` | `0` = pass, `1` = findings at/above threshold |

## Configuration File

Place a `pr-guard.config.yml` (or `.pr-guard.yml`) in your repo root to customize behavior:

```yaml
checks:
  code-quality:
    enabled: true
    extra_instructions: |
      Our team uses the Result pattern instead of exceptions.
      Pay special attention to error handling.

  sast:
    enabled: true
    extra_instructions: |
      Focus on OWASP Top 10. Flag any use of eval().

  secret-detection:
    enabled: true

  iac-security:
    enabled: true
    extra_instructions: |
      All resources must have tags: environment, team, cost-center.

  container-security:
    enabled: true

# Global excludes (merged with built-in defaults)
exclude_patterns:
  - "**/vendor/**"
  - "**/third_party/**"
```

See [pr-guard.config.example.yml](pr-guard.config.example.yml) for the full reference.

## Custom Checks

Create your own checks by adding a folder with two files:

```
my-checks/
  license-headers/
    prompt.md       # AI instructions (what to look for)
    config.yml      # File patterns, excludes
  api-conventions/
    prompt.md
    config.yml
```

Then reference it:

```yaml
- uses: dexterite/pr-guard-ai@main
  with:
    api-key: ${{ secrets.OPENAI_API_KEY }}
    custom-checks-dir: "./my-checks"
```

### prompt.md format

Write a system prompt that tells the AI what to analyze. End with the JSON output schema:

```markdown
# My Custom Check

You are an expert reviewing code for [specific concern].

## What to Look For
- [Rule 1]
- [Rule 2]

## Output Format
Respond with JSON:
{"findings": [{"file": "...", "line": 0, "severity": "...", "category": "...",
  "title": "...", "description": "...", "suggestion": "..."}],
 "summary": "..."}
```

### config.yml format

```yaml
file_patterns:
  - "**/*.py"
  - "**/*.js"
exclude_patterns:
  - "**/test/**"
```

## Output Destinations

### GitHub Step Summary (default)

The Markdown report is written to the Actions run summary — no setup needed.

### File

```yaml
ship-to: "github-summary,file"
ship-file-path: "reports/pr-guard"
```

Produces `reports/pr-guard.md` (or `.json` / `.sarif.json` depending on `output-format`).

### Webhook

```yaml
ship-to: "webhook"
ship-webhook-url: ${{ secrets.WEBHOOK_URL }}
```

POSTs a JSON payload:

```json
{
  "source": "pr-guard-ai",
  "repository": "owner/repo",
  "ref": "refs/pull/42/merge",
  "sha": "abc123...",
  "run_id": "12345",
  "total_findings": 5,
  "results": [ ... ],
  "report": "# markdown report ..."
}
```

Use this to integrate with Slack, PagerDuty, Jira, Datadog, or any system that accepts webhooks. The webhook contract can also serve as the adapter interface for custom destinations.

### PR Comment

```yaml
ship-to: "github-pr-comment"
github-token: ${{ secrets.GITHUB_TOKEN }}
```

Posts a collapsible comment on the pull request.

### SARIF (GitHub Code Scanning)

```yaml
output-format: sarif
ship-to: file
ship-file-path: pr-guard-report
```

Then upload to GitHub Code Scanning:

```yaml
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: pr-guard-report.sarif.json
```

## Rate-Limit Handling & Throttling

Full-scan mode (or large PRs) can produce many API calls. PR Guard AI has a two-layer throttle to avoid 429 errors:

### 1. Base delay (`request-delay-ms`)

A fixed minimum pause between every API call. Set it to match your provider's rate limit:

```yaml
request-delay-ms: "500"   # 500 ms between calls → ~2 requests/sec
```

### 2. Adaptive auto-ramp (always on)

Even with `request-delay-ms: "0"`, the client reacts to 429 responses automatically:

- Reads the `Retry-After` header from the provider
- Ramps the internal delay up (`×1.5 + 1 s`, floored by `Retry-After`)
- On each successful call, decays the extra delay back down (`×0.75 − 0.1 s`)
- Up to 5 retries per request before failing

The two layers stack: `effective delay = base delay + adaptive penalty`.

**Recommended for full scans:** set `request-delay-ms` to a conservative value (e.g. `500`–`1000`) so the adaptive layer rarely needs to kick in. For diff-only runs on small PRs you can leave it at `0`.

After all checks complete, throttle stats are printed:

```
  Throttle stats: 23 API calls, 4.2s throttled, effective delay 500ms
```

## Architecture

```
pr-guard-ai/
├── action.yml                     # Composite GitHub Action entry point
├── requirements.txt               # Python deps (requests, pyyaml)
├── checks/                        # Built-in check definitions
│   ├── code-quality/
│   │   ├── prompt.md              # AI system prompt
│   │   └── config.yml             # File patterns & settings
│   ├── sast/
│   ├── secret-detection/
│   ├── iac-security/
│   └── container-security/
├── src/                           # Python engine
│   ├── main.py                    # Entry point & orchestration
│   ├── config_loader.py           # Env + file + check config merging
│   ├── file_collector.py          # Git diff, glob matching, filtering
│   ├── ai_client.py               # OpenAI-compatible API client
│   ├── runner.py                  # Check execution & batching
│   ├── output_formatter.py        # Markdown / JSON / SARIF + sanitization
│   └── shipper.py                 # Result delivery (summary, file, webhook, PR)
├── schemas/
│   └── config.schema.json         # JSON Schema for config validation
└── pr-guard.config.example.yml    # Example user config
```

### Flow

1. **Config** — merge defaults → env vars → user config → per-check config
2. **File collection** — git diff (or ls-files) → filter by patterns & size
3. **Batching** — split files into token-limited batches
4. **AI analysis** — send each batch with the check's prompt to the LLM
5. **Aggregation** — merge findings, tag with check name & severity
6. **Formatting** — render as Markdown, JSON, or SARIF
7. **Sanitization** — strip any accidentally leaked secrets
8. **Shipping** — deliver to configured destinations

## Extending the Result Interface

The webhook payload and JSON output format serve as the **integration interface**. To ship results to a new destination:

1. **Webhook adapter**: Point `ship-webhook-url` at a proxy/adapter service that transforms the payload for your target (e.g., Slack, Jira, S3, Elasticsearch).

2. **File + post-step**: Use `ship-to: file`, then add a subsequent workflow step that reads the report and pushes it wherever you need:

   ```yaml
   - uses: dexterite/pr-guard-ai@main
     with:
       api-key: ${{ secrets.OPENAI_API_KEY }}
       ship-to: file
       output-format: json
       ship-file-path: report

   - name: Push to S3
     run: aws s3 cp report.json s3://my-bucket/reports/${{ github.sha }}.json
   ```

3. **Custom shipper module**: Fork and add a shipping backend in `src/shipper.py` — the function signature is `ship(report, results, config)`.

## LLM Compatibility

Works with any OpenAI Chat Completions–compatible API:

| Provider | `api-base-url` | Notes |
|----------|----------------|-------|
| OpenAI | `https://api.openai.com/v1` | Default |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | Set model to deployment name |
| GitHub Models | `https://models.inference.ai.azure.com/v1` | Use a PAT from a user with GitHub Models access |
| Ollama | `http://localhost:11434/v1` | Local models |
| vLLM | `http://localhost:8000/v1` | Self-hosted |
| LiteLLM | `http://localhost:4000/v1` | Proxy for 100+ providers |
| Anthropic (via proxy) | Depends on proxy | Use an OpenAI-compatible wrapper |

> **Tip — GitHub Models:** If you have access to [GitHub Models](https://github.com/marketplace/models), you can use it instead of OpenAI. The built-in `GITHUB_TOKEN` does **not** work — generate a [Personal Access Token](https://github.com/settings/tokens) and save it as a repo secret.
>
> **Important:** GitHub Models has a low token limit (~8 000 for gpt-4o). Set `max-context-tokens` accordingly:
>
> ```yaml
> - uses: dexterite/pr-guard-ai@main
>   with:
>     api-key: ${{ secrets.GH_MODELS_PAT }}
>     api-base-url: "https://models.inference.ai.azure.com/v1"
>     model: "gpt-4o"
>     max-context-tokens: "8000"
>     request-delay-ms: "1000"
> ```

## Security Considerations

- **API keys** are never printed in logs — the output sanitizer explicitly redacts them
- **Secrets in code** are reported by type and location only, never echoed
- All output passes through a sanitization layer that matches common secret patterns
- The action runs with the permissions you grant — `contents: read` is sufficient for most checks
- Webhook payloads are capped at 50 KB to prevent accidental data leakage

## License

MIT