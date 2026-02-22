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
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self) -> list[dict]:
        """Execute all enabled checks. Returns a list of result dicts."""
        results: list[dict] = []

        for check_name, check_def in self.config["check_definitions"].items():
            print(f"\n::group::Check: {check_name}")
            result = self._run_check(check_name, check_def)
            results.append(result)
            print(f"::endgroup::")

        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_check(self, name: str, check_def: dict) -> dict:
        """Run a single check against matching files."""
        check_config = check_def.get("config", {})
        prompt = check_def["prompt"]

        # Collect matching files
        files = collect_files(check_config, self.config)
        print(f"  Matched files : {len(files)}")

        if not files:
            return {
                "check": name,
                "files_analyzed": 0,
                "findings": [],
                "summary": "No matching files found.",
            }

        # Split into token-limited batches
        batches = self._build_batches(files)
        print(f"  Batches       : {len(batches)}")

        all_findings: list[dict] = []

        for idx, batch in enumerate(batches, start=1):
            file_count = len(batch)
            print(f"  Batch {idx}/{len(batches)} ({file_count} file(s)) …")

            user_msg = self._build_user_message(batch)

            try:
                response = self.client.analyze(prompt, user_msg)
                findings = response.get("findings", [])

                # Tag every finding with the originating check
                for f in findings:
                    f.setdefault("check", name)

                all_findings.extend(findings)
                print(f"    → {len(findings)} finding(s)")

            except Exception as exc:
                print(f"::warning::Batch {idx} of '{name}' failed: {exc}")
                all_findings.append(
                    {
                        "check": name,
                        "severity": "info",
                        "title": "Analysis Error",
                        "description": f"AI analysis failed for batch {idx}: {str(exc)[:300]}",
                        "file": "",
                        "line": 0,
                        "category": "error",
                    }
                )

        return {
            "check": name,
            "files_analyzed": len(files),
            "findings": all_findings,
            "summary": f"Analyzed {len(files)} file(s), found {len(all_findings)} issue(s).",
        }

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
