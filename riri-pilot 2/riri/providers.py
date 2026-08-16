"""Provider adapter: one interface, two backends (Anthropic / Gemini).

The proposal commits to deploying 'equivalently under Gemini or Claude
depending on availability.' This module is that commitment. Switch with:
    RIRI_PROVIDER=anthropic|gemini

v2:
- One shared AsyncClient per provider (v1 built and tore down a client per
  request — connection churn under a full section).
- Retry with backoff on transient statuses (429/5xx/529) and network errors:
  2 retries, then ProviderError. A class-hour rate-limit blip no longer
  surfaces as an instant 502 to a student mid-thought.
- Network exceptions are wrapped as ProviderError so app.py's one except
  clause catches everything the provider can throw.
- get_provider() names the missing env var instead of a bare KeyError, so a
  misconfigured VM fails at boot with a message worth reading.
- Gemini: a safety-blocked response (no candidates) now reports blockReason /
  finishReason instead of a bare KeyError string.
"""
import asyncio, os, random, httpx

TIMEOUT = 60.0
MAX_TOKENS = int(os.environ.get("RIRI_MAX_TOKENS", "1024"))
RETRY_STATUS = {429, 500, 502, 503, 529}
MAX_ATTEMPTS = 3


class ProviderError(Exception):
    pass


class _HttpProvider:
    name = "base"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def _post(self, url: str, **kw) -> httpx.Response:
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                r = await self._client.post(url, **kw)
            except httpx.HTTPError as e:
                last = f"network: {e!r}"
            else:
                if r.status_code not in RETRY_STATUS:
                    return r
                last = f"{r.status_code}: {r.text[:300]}"
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (2 ** attempt) + random.random() * 0.4)
        raise ProviderError(f"{self.name} {last}")


class AnthropicProvider(_HttpProvider):
    name = "anthropic"

    def __init__(self):
        super().__init__()
        self.key = os.environ["ANTHROPIC_API_KEY"]
        self.model = os.environ.get("RIRI_ANTHROPIC_MODEL", "claude-sonnet-4-6")

    async def chat(self, system: str, messages: list[dict]) -> str:
        r = await self._post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": messages,
            },
        )
        if r.status_code != 200:
            raise ProviderError(f"anthropic {r.status_code}: {r.text[:300]}")
        data = r.json()
        return "".join(b.get("text", "") for b in data["content"] if b["type"] == "text")


class GeminiProvider(_HttpProvider):
    name = "gemini"

    def __init__(self):
        super().__init__()
        self.key = os.environ["GEMINI_API_KEY"]
        self.model = os.environ.get("RIRI_GEMINI_MODEL", "gemini-2.5-pro")

    async def chat(self, system: str, messages: list[dict]) -> str:
        contents = [
            {"role": "user" if m["role"] == "user" else "model",
             "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        r = await self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {"maxOutputTokens": MAX_TOKENS},
            },
        )
        if r.status_code != 200:
            raise ProviderError(f"gemini {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return "".join(
                p.get("text", "")
                for p in data["candidates"][0]["content"]["parts"]
            )
        except (KeyError, IndexError):
            block = (data.get("promptFeedback", {}).get("blockReason")
                     or (data.get("candidates") or [{}])[0].get("finishReason")
                     or "unknown")
            raise ProviderError(f"gemini returned no text (reason: {block})")


def get_provider():
    which = os.environ.get("RIRI_PROVIDER", "anthropic").lower()
    cls = GeminiProvider if which == "gemini" else AnthropicProvider
    try:
        return cls()
    except KeyError as e:
        raise RuntimeError(
            f"Provider '{cls.name}' selected but env var {e.args[0]} is not set."
        ) from None


def est_tokens(text: str) -> int:
    """Rough budget accounting only (chars/4). Not billing-grade; caps are coarse by design."""
    return max(1, len(text) // 4)
