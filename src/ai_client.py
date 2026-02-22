"""AI API client for OpenAI-compatible chat completion endpoints."""

import json
import time

import requests


class AIClient:
    """Thin wrapper around the OpenAI Chat Completions API.

    Works with any OpenAI-compatible endpoint (OpenAI, Azure OpenAI,
    Ollama, vLLM, LiteLLM, etc.).

    Includes:
    - Configurable base delay between calls (``request_delay_ms``)
    - Adaptive throttle: on 429 the delay auto-ramps and decays over time
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        max_retries: int = 5,
        timeout: int = 300,
        request_delay_ms: int = 0,
        temperature: float = 0.1,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout
        self.temperature = temperature

        # --- Throttle state -----------------------------------------------
        self._base_delay_s = request_delay_ms / 1000.0  # user-configured floor
        self._adaptive_delay_s = 0.0  # extra delay added on 429
        self._last_call_time = 0.0
        self._total_calls = 0
        self._total_throttle_s = 0.0

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

    def preflight(self) -> dict:
        """Quick smoke-test: verify API key, endpoint, and model availability.

        Returns a dict with 'ok' (bool), 'model' (str), and 'error' (str|None).
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 5,
            "temperature": 0,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)

            if resp.status_code == 200:
                return {"ok": True, "model": self.model, "error": None}

            # Try to extract structured error from JSON body
            detail = ""
            error_code = ""
            try:
                body = resp.json()
                err = body.get("error", {})
                detail = err.get("message", resp.text[:300])
                error_code = err.get("code", "")
            except Exception:
                detail = resp.text[:300]

            # Model not accessible — can be 400, 403, or 404 with various error codes
            if (resp.status_code == 404
                    or error_code == "model_not_found"
                    or "does not have access to model" in detail.lower()
                    or "model_not_found" in detail.lower()):
                return {
                    "ok": False,
                    "model": self.model,
                    "error": f"Model '{self.model}' is not available for your API key (HTTP {resp.status_code}). "
                             f"{detail}\n"
                             f"  → Verify the model is enabled in your OpenAI project settings.\n"
                             f"  → Ensure the API key in GitHub secrets belongs to the correct project.\n"
                             f"  → Try the dated name: gpt-4.1-mini-2025-04-14",
                }
            if resp.status_code in (401, 403):
                return {
                    "ok": False,
                    "model": self.model,
                    "error": f"Authentication failed (HTTP {resp.status_code}). "
                             f"Check your API key and permissions. Detail: {detail}",
                }
            if resp.status_code == 429:
                # Rate-limited even on preflight — API works but heavily throttled
                return {
                    "ok": True,
                    "model": self.model,
                    "error": "Warning: rate-limited (429) on preflight. "
                             "Expect slow execution. Consider increasing request-delay-ms.",
                }
            return {
                "ok": False,
                "model": self.model,
                "error": f"Unexpected HTTP {resp.status_code}: {detail}",
            }

        except requests.exceptions.Timeout:
            return {
                "ok": False,
                "model": self.model,
                "error": f"Connection timed out after 30s. "
                         f"Check api-base-url: {self.base_url}",
            }
        except requests.exceptions.ConnectionError as exc:
            return {
                "ok": False,
                "model": self.model,
                "error": f"Cannot connect to {self.base_url} — {str(exc)[:200]}",
            }

    @property
    def effective_delay_ms(self) -> int:
        """Current per-call delay (base + adaptive) in milliseconds."""
        return int((self._base_delay_s + self._adaptive_delay_s) * 1000)

    @property
    def stats(self) -> dict:
        """Return call statistics for logging."""
        return {
            "total_calls": self._total_calls,
            "total_throttle_s": round(self._total_throttle_s, 1),
            "effective_delay_ms": self.effective_delay_ms,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _throttle(self):
        """Sleep enough to respect the configured + adaptive delay."""
        delay = self._base_delay_s + self._adaptive_delay_s
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_call_time
        remaining = delay - elapsed
        if remaining > 0:
            self._total_throttle_s += remaining
            time.sleep(remaining)

    def _decay_adaptive_delay(self):
        """Slowly reduce the adaptive penalty after a successful call."""
        if self._adaptive_delay_s > 0:
            self._adaptive_delay_s = max(0.0, self._adaptive_delay_s * 0.75 - 0.1)

    def _ramp_adaptive_delay(self, retry_after: float):
        """Increase adaptive delay after a 429."""
        # At least the Retry-After value, but keep growing if repeated
        self._adaptive_delay_s = max(
            self._adaptive_delay_s * 1.5 + 1.0,
            retry_after,
        )
        print(f"    Adaptive delay ramped to {self.effective_delay_ms}ms")

    def _chat_completion(self, messages: list[dict]) -> str:
        """Call the chat/completions endpoint with retry, back-off & throttle."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }

        last_error: str | None = None

        # Respect throttle before first attempt
        self._throttle()

        for attempt in range(1, self.max_retries + 1):
            try:
                self._last_call_time = time.monotonic()
                self._total_calls += 1

                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                if resp.status_code == 200:
                    self._decay_adaptive_delay()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]

                # Rate-limited
                if resp.status_code == 429:
                    last_error = "rate-limited (429) — your API plan's rate limit was exceeded"
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    self._ramp_adaptive_delay(retry_after)
                    wait = max(retry_after, self._base_delay_s + self._adaptive_delay_s)
                    print(f"    Rate-limited (429). Waiting {wait:.1f}s … (attempt {attempt}/{self.max_retries})")
                    self._total_throttle_s += wait
                    time.sleep(wait)
                    continue

                # Transient server error
                if resp.status_code >= 500:
                    last_error = f"server error (HTTP {resp.status_code})"
                    wait = 2 ** attempt
                    print(f"    Server error ({resp.status_code}). Retry {attempt} in {wait}s…")
                    time.sleep(wait)
                    continue

                # Client error — don't retry
                snippet = resp.text[:500]
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Authentication failed (HTTP {resp.status_code}). "
                        f"Check that your API key is valid and has the required permissions. "
                        f"Detail: {snippet}"
                    )
                if resp.status_code == 413:
                    raise RuntimeError(
                        f"Request too large (HTTP 413). The batch exceeds the model's token limit. "
                        f"Lower 'max-context-tokens' to create smaller batches "
                        f"(GitHub Models gpt-4o limit is ~8 000 tokens). "
                        f"Detail: {snippet}"
                    )
                raise RuntimeError(
                    f"AI API returned HTTP {resp.status_code}: {snippet}"
                )

            except requests.exceptions.Timeout:
                last_error = f"request timed out after {self.timeout}s"
                wait = 2 ** attempt
                print(f"    Timeout on attempt {attempt}. Retrying in {wait}s…")
                time.sleep(wait)

            except requests.exceptions.ConnectionError as exc:
                last_error = f"connection error — {str(exc)[:150]}"
                wait = 2 ** attempt
                print(f"    Connection error on attempt {attempt}. Retrying in {wait}s…")
                time.sleep(wait)

        raise RuntimeError(
            f"AI API failed after {self.max_retries} attempts — last error: {last_error}"
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
