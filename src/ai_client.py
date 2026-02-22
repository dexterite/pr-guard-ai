"""AI API client for OpenAI-compatible chat completion endpoints."""

import json
import time

import requests


class AIClient:
    """Thin wrapper around the OpenAI Chat Completions API.

    Works with any OpenAI-compatible endpoint (OpenAI, Azure OpenAI,
    Ollama, vLLM, LiteLLM, etc.).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        max_retries: int = 3,
        timeout: int = 180,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def analyze(self, system_prompt: str, user_content: str) -> dict:
        """Send an analysis request and return the parsed JSON response."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        raw = self._chat_completion(messages)
        return self._parse_json(raw)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _chat_completion(self, messages: list[dict]) -> str:
        """Call the chat/completions endpoint with retry & back-off."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]

                # Rate-limited
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                    print(f"    Rate-limited (429). Waiting {wait}s …")
                    time.sleep(wait)
                    continue

                # Transient server error
                if resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    print(f"    Server error ({resp.status_code}). Retry {attempt}…")
                    time.sleep(2 ** attempt)
                    continue

                # Client error — don't retry
                snippet = resp.text[:500]
                raise RuntimeError(
                    f"AI API client error {resp.status_code}: {snippet}"
                )

            except requests.exceptions.Timeout:
                last_error = "timeout"
                print(f"    Timeout on attempt {attempt}. Retrying…")
                time.sleep(2 ** attempt)

            except requests.exceptions.ConnectionError as exc:
                last_error = str(exc)[:200]
                print(f"    Connection error on attempt {attempt}. Retrying…")
                time.sleep(2 ** attempt)

        raise RuntimeError(
            f"AI API failed after {self.max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Parse JSON from AI output, tolerating markdown fences."""
        text = text.strip()

        # Strip ```json … ``` wrappers
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to extract the first JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

        # Last resort: wrap the raw text as a finding
        return {
            "findings": [
                {
                    "severity": "info",
                    "title": "Unparsed AI Response",
                    "description": text[:2000],
                    "file": "",
                    "line": 0,
                    "category": "parse-error",
                }
            ],
            "summary": "AI response could not be parsed as structured JSON.",
        }
