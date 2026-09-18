"""Model access, behind a seam.

Provider choice is not load-bearing for this system -- the model is used once,
during discovery, and never again. So the client is small and swappable, and
the default is a *local* model.

That default is a deliberate argument, not a convenience. Two reasons:

  1. Regulated data. Discovery reads live back-office screens. With a local
     model, member names and balances never leave the host, which removes an
     entire class of data-handling question from the discovery path.
  2. It is the harder test. If an observe/decide/act loop completes a real
     multi-step flow driven by a 7B model on a CPU, the loop design is carrying
     the weight rather than the model's cleverness. A frontier model would make
     a weak loop look fine.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class LLMReply:
    raw: str
    data: dict[str, Any] | None
    latency_ms: int
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class LLMClient(Protocol):
    name: str

    def decide(self, system: str, user: str, *, max_tokens: int = 110) -> LLMReply:
        """Return one structured decision."""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Salvage a JSON object from a small model's output.

    Small models wrap JSON in prose or fences more often than large ones. This
    is a pragmatic recovery, not a substitute for validation -- the caller still
    validates the result against the action schema and rejects it if wrong.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


@dataclass
class OllamaClient:
    """Local Ollama. No API key, no egress, no per-token cost."""
    model: str = os.environ.get("LEDGERHAND_MODEL", "qwen2.5:7b-instruct")
    host: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    temperature: float = 0.0
    #: CPU inference on a cold model can take minutes for the first call.
    timeout_s: float = 900.0
    #: Keep weights resident between steps. On CPU inference, reloading the
    #: model costs more than the generation itself.
    keep_alive: str = "15m"
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"ollama/{self.model}"

    def _options(self, max_tokens: int) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "num_predict": max_tokens,
            "top_p": 0.9,
            # A decision is ~40 tokens. On CPU inference the context window is a
            # direct wall-clock cost, so it is sized to the job.
            "num_ctx": 2048,
        }

    def warm(self) -> None:
        """Load the weights before the loop starts.

        On CPU the first call pays a multi-minute model load. Paying it inside
        step 1 makes a cold start look like a hung agent, and the loop's own
        stuck detection will -- correctly -- escalate a run that was only ever
        waiting for a disk read.

        The options must match the ones decide() sends. Ollama keys its resident
        instance on them, so warming with different options reloads the weights
        on the first real call and the warm-up buys nothing -- which is exactly
        what happened the first time I tried this.
        """
        try:
            httpx.post(f"{self.host}/api/chat", timeout=self.timeout_s, json={
                "model": self.model, "messages": [{"role": "user", "content": "ok"}],
                "stream": False, "format": "json", "keep_alive": self.keep_alive,
                "options": self._options(1),
            })
        except Exception:
            pass

    def decide(self, system: str, user: str, *, max_tokens: int = 110) -> LLMReply:
        started = time.monotonic()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": self._options(max_tokens),
        }
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # network, timeout, malformed response
            return LLMReply("", None, int((time.monotonic() - started) * 1000),
                            self.name, error=f"{type(exc).__name__}: {exc}")
        raw = (body.get("message") or {}).get("content", "")
        return LLMReply(
            raw=raw,
            data=_extract_json(raw),
            latency_ms=int((time.monotonic() - started) * 1000),
            model=self.name,
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            output_tokens=int(body.get("eval_count") or 0),
        )


@dataclass
class OpenAICompatClient:
    """Any OpenAI-shaped endpoint (Groq, Gemini's compat layer, OpenRouter...).

    Present so the provider choice is demonstrably a seam rather than an
    assumption. Reads its key from the environment; nothing is stored in-repo.
    """
    model: str = os.environ.get("LEDGERHAND_MODEL", "llama-3.3-70b-versatile")
    base_url: str = os.environ.get("LEDGERHAND_LLM_BASE", "https://api.groq.com/openai/v1")
    api_key_env: str = "LEDGERHAND_LLM_KEY"
    temperature: float = 0.0
    timeout_s: float = 120.0
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"openai-compat/{self.model}"

    def decide(self, system: str, user: str, *, max_tokens: int = 110) -> LLMReply:
        key = os.environ.get(self.api_key_env)
        started = time.monotonic()
        if not key:
            return LLMReply("", None, 0, self.name,
                            error=f"{self.api_key_env} is not set")
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": self.temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            return LLMReply("", None, int((time.monotonic() - started) * 1000),
                            self.name, error=f"{type(exc).__name__}: {exc}")
        raw = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        return LLMReply(raw, _extract_json(raw),
                        int((time.monotonic() - started) * 1000), self.name,
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        output_tokens=int(usage.get("completion_tokens") or 0))


def build_client(spec: str | None = None) -> LLMClient:
    """`ollama:qwen2.5:7b-instruct` | `openai:llama-3.3-70b` | None -> default."""
    spec = spec or os.environ.get("LEDGERHAND_LLM", "ollama")
    provider, _, model = spec.partition(":")
    if provider == "openai":
        return OpenAICompatClient(model=model) if model else OpenAICompatClient()
    return OllamaClient(model=model) if model else OllamaClient()


@dataclass
class ScriptedClient:
    """A deterministic stand-in for tests.

    Exists so loop behaviour -- stuck detection, policy refusals, invalid
    output handling, output binding -- can be tested exhaustively and fast.
    Those paths are the ones that matter and the ones a real model exercises
    least, since a real model mostly succeeds.
    """
    script: list[dict[str, Any]] = field(default_factory=list)
    name: str = "scripted"
    calls: list[str] = field(default_factory=list)
    #: Returned once the script runs out; defaults to conceding.
    fallback: dict[str, Any] = field(
        default_factory=lambda: {"action": "give_up", "reason": "script exhausted"})

    def decide(self, system: str, user: str, *, max_tokens: int = 110) -> LLMReply:
        self.calls.append(user)
        item = self.script.pop(0) if self.script else self.fallback
        raw = item if isinstance(item, str) else json.dumps(item)
        return LLMReply(raw=raw, data=_extract_json(raw), latency_ms=0, model=self.name)
