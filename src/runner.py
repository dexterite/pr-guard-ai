"""Check runner — orchestrates AI analysis for each enabled check."""

from ai_client import AIClient
from file_collector import collect_files, read_file_content

# Rough chars-per-token ratio for source code
_CHARS_PER_TOKEN = 3.5


class CheckRunner:
    """Runs every enabled check and collects findings."""

    def __init__(self, config: dict):
        self.config = config
        self.client = AIClient(
            api_key=config["api_key"],
            base_url=config["api_base_url"],
            model=config["model"],
            request_delay_ms=config.get("request_delay_ms", 0),
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self) -> list[dict]:
        """Execute all enabled checks. Returns a list of result dicts."""
        results: list[dict] = []
        total = len(self.config["check_definitions"])

        for i, (check_name, check_def) in enumerate(self.config["check_definitions"].items(), 1):
            print(f"\n::group::Check {i}/{total}: {check_name}")
            result = self._run_check(check_name, check_def)
            results.append(result)
            print(f"::endgroup::")

        self._log_throttle_stats()
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_check(self, name: str, check_def: dict) -> dict:
        """Run a single check against matching files."""
        check_config = check_def.get("config", {})
        prompt = check_def["prompt"]
        debug = self.config.get("debug", False)

        # Collect matching files
        print(f"  Collecting files...")
        files = collect_files(check_config, self.config)
        print(f"  Matched files : {len(files)}")

        if not files:
            return {
                "check": name,
                "files_analyzed": 0,
                "findings": [],
                "summary": "No matching files found.",
            }

        if debug:
            for f in files[:10]:
                print(f"    [debug]   {f}")
            if len(files) > 10:
                print(f"    [debug]   ... and {len(files) - 10} more")

        # Split into token-limited batches
        print(f"  Building batches (token budget: {self.config.get('max_context_tokens', 100_000):,})...")
        batches = self._build_batches(files)
        print(f"  Batches       : {len(batches)}")

        all_findings: list[dict] = []

        for idx, batch in enumerate(batches, start=1):
            file_count = len(batch)
            print(f"  Batch {idx}/{len(batches)} ({file_count} file(s)) — sending to AI...")

            user_msg = self._build_user_message(batch)

            try:
                response = self.client.analyze(prompt, user_msg)
                findings = response.get("findings", [])
                summary = response.get("summary", "")

                # Tag every finding with the originating check
                for f in findings:
                    f.setdefault("check", name)

                all_findings.extend(findings)
                print(f"    → {len(findings)} finding(s)")
                if summary:
                    print(f"    AI summary: {summary[:200]}")

            except Exception as exc:
                error_msg = str(exc)
                print(f"::warning::Batch {idx} of '{name}' failed: {error_msg}")

                # Build a user-friendly description
                if "429" in error_msg or "rate-limit" in error_msg.lower():
                    friendly = (
                        f"Batch {idx} was rate-limited by the AI provider after multiple retries. "
                        f"Try increasing 'request-delay-ms' (e.g. 500–1000) or reducing the number "
                        f"of files with 'exclude-patterns' / 'max-file-size-kb'."
                    )
                elif "timeout" in error_msg.lower():
                    friendly = (
                        f"Batch {idx} timed out waiting for an AI response. "
                        f"The batch may contain too many files — try lowering 'max-context-tokens' "
                        f"to create smaller batches, or check your API endpoint availability."
                    )
                elif "connection" in error_msg.lower():
                    friendly = (
                        f"Batch {idx} could not connect to the AI API. "
                        f"Verify 'api-base-url' is correct and the endpoint is reachable."
                    )
                else:
                    friendly = f"Batch {idx} failed: {error_msg[:300]}"

                batch_files = [fp for fp, _ in batch]
                file_list = ", ".join(batch_files[:5])
                if len(batch_files) > 5:
                    file_list += f" (+{len(batch_files) - 5} more)"

                all_findings.append(
                    {
                        "check": name,
                        "severity": "medium",
                        "title": f"AI Analysis Failed — Batch {idx}/{len(batches)}",
                        "description": f"{friendly}\n\nAffected files: {file_list}",
                        "file": batch_files[0] if batch_files else "",
                        "line": 0,
                        "category": "analysis-error",
                        "suggestion": "Re-run with 'debug: true' for full diagnostics. "
                                       "If rate-limited, add 'request-delay-ms: 1000'.",
                    }
                )

        return {
            "check": name,
            "files_analyzed": len(files),
            "findings": all_findings,
            "summary": f"Analyzed {len(files)} file(s), found {len(all_findings)} issue(s).",
        }

    def _log_throttle_stats(self):
        """Print throttle statistics if any throttling occurred."""
        stats = self.client.stats
        if stats["total_throttle_s"] > 0 or stats["effective_delay_ms"] > 0:
            print(f"\n  Throttle stats: {stats['total_calls']} API calls, "
                  f"{stats['total_throttle_s']}s throttled, "
                  f"effective delay {stats['effective_delay_ms']}ms")

    # ------------------------------------------------------------------
    # Batching
    # ------------------------------------------------------------------

    def _build_batches(self, files: list[str]) -> list[list[tuple[str, str]]]:
        """Split files into batches that fit the token budget."""
        max_tokens = self.config.get("max_context_tokens", 100_000)
        available = int(max_tokens * 0.70)  # leave room for prompt + response

        batches: list[list[tuple[str, str]]] = []
        current_batch: list[tuple[str, str]] = []
        current_tokens = 0

        for filepath in files:
            content, _ = read_file_content(filepath)
            tokens = len(content) / _CHARS_PER_TOKEN

            # If this single file exceeds the budget, it still gets its own batch
            if current_tokens + tokens > available and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append((filepath, content))
            current_tokens += tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(file_batch: list[tuple[str, str]]) -> str:
        """Compose the user-role message containing file contents."""
        parts = [
            "Analyze the following source files and report any findings.\n\n",
            "Respond with a JSON object in this exact schema:\n",
            "```json\n",
            '{\n',
            '  "findings": [\n',
            '    {\n',
            '      "file": "relative/path/to/file.ext",\n',
            '      "line": 42,\n',
            '      "severity": "high",\n',
            '      "category": "category-id",\n',
            '      "title": "Short descriptive title",\n',
            '      "description": "Detailed description of the issue",\n',
            '      "suggestion": "How to fix it"\n',
            '    }\n',
            '  ],\n',
            '  "summary": "Brief summary of findings"\n',
            '}\n',
            "```\n\n",
            "Allowed severity values: critical, high, medium, low, info\n",
            'If no issues are found return: {"findings": [], "summary": "No issues found."}\n\n',
            "---\n\n",
        ]

        for filepath, content in file_batch:
            parts.append(f"### FILE: {filepath}\n```\n{content}\n```\n\n")

        return "".join(parts)
