"""Language-model client for local Ollama or the OpenAI Responses API.

The provider is selected explicitly with ``LLM_PROVIDER``.  Embeddings remain
local in Ollama even when OpenAI is used for text generation.
"""

import json
import re
import requests

import config

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


# Backward compatibility: callers and tests historically catch this name.
OllamaError = LLMError


def _strip_think(text: str) -> str:
    """Remove qwen3-style reasoning blocks from the visible answer."""
    text = _THINK.sub("", text)
    if "<think>" in text:  # unclosed block: the model hit the token limit
        text = text.split("<think>")[0]
    return text.strip()


def installed_models() -> list[str]:
    try:
        resp = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=10)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except requests.RequestException as exc:
        raise OllamaError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST}. Is `ollama serve` running?"
        ) from exc


def health() -> tuple[bool, str]:
    """Check the selected completion provider and configured model."""
    if config.LLM_PROVIDER == "openai":
        return _openai_health()
    try:
        available = installed_models()
    except OllamaError as exc:
        return False, str(exc)

    # `ollama list` shows "name:tag"; a bare name means the :latest tag.
    have = {m.split(":")[0] for m in available} | set(available)
    needed = {config.SQL_MODEL, config.ANSWER_MODEL, config.ROUTER_MODEL}
    if config.USE_EMBEDDINGS:
        needed.add(config.EMBED_MODEL)

    missing = [m for m in needed if m not in have and m.split(":")[0] not in have]
    if missing:
        return False, "Missing model(s). Run:\n" + "\n".join(
            f"  ollama pull {m}" for m in sorted(missing)
        )
    return True, f"Ollama ready ({len(available)} models installed)"


def complete(
    system: str,
    user: str,
    model: str = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    schema: dict = None,
    timeout: float = None,
) -> str:
    """One prompt in, text out through the explicitly selected provider.

    ``temperature`` applies to Ollama; the OpenAI reasoning model uses its
    configured reasoning effort. ``schema`` requests structured JSON output.
    """
    if config.LLM_PROVIDER == "openai":
        return _complete_openai(system, user, max_tokens=max_tokens, schema=schema, timeout=timeout)
    if config.LLM_PROVIDER != "ollama":
        raise LLMError(f"Unknown LLM provider '{config.LLM_PROVIDER}'. Choose 'ollama' or 'openai'.")
    return _complete_ollama(system, user, model, max_tokens, temperature, schema, timeout)


def _complete_ollama(system, user, model, max_tokens, temperature, schema, timeout) -> str:
    payload = {
        "model": model or config.ANSWER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": config.KEEP_ALIVE,
        "options": {
            "temperature": temperature,
            "num_ctx": config.NUM_CTX,
            "num_predict": max_tokens,
        },
    }
    if schema:
        payload["format"] = schema
    if config.DISABLE_THINK:
        payload["think"] = False

    # `timeout` lets a caller with a thinking budget cap this one call.
    limit = min(timeout, config.OLLAMA_TIMEOUT) if timeout else config.OLLAMA_TIMEOUT
    if limit <= 0:
        raise OllamaError("No time left in the thinking budget")
    resp = _post_with_retry(f"{config.OLLAMA_HOST}/api/chat", payload, timeout=limit)

    if resp.status_code == 400 and "think" in payload:
        payload.pop("think")  # older Ollama does not know this field
        resp = _post_with_retry(f"{config.OLLAMA_HOST}/api/chat", payload, timeout=limit)

    if not resp.ok:
        raise OllamaError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    # Ollama silently drops the start of the prompt when it exceeds num_ctx,
    # which removes the schema and produces confident nonsense. Surface it.
    if data.get("prompt_eval_count") and data["prompt_eval_count"] >= config.NUM_CTX - 8:
        import logging
        logging.getLogger("llm").warning(
            "prompt filled the whole context window (%s tokens); it was probably truncated. "
            "Raise OLLAMA_NUM_CTX.", data["prompt_eval_count"])
    return _strip_think(data.get("message", {}).get("content", ""))


def _openai_client(api_key: str = "", timeout: float | None = None):
    """Build a short-lived client so Settings-page changes take effect at once."""
    key = api_key or config.OPENAI_API_KEY
    if not key:
        raise LLMError("OpenAI API key is not configured. Add it on the Settings page or set OPENAI_API_KEY.")
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise LLMError("The OpenAI Python package is not installed. Run: pip install -r requirements.txt") from exc
    return OpenAI(
        api_key=key,
        base_url=config.OPENAI_BASE_URL,
        timeout=timeout or config.OPENAI_TIMEOUT,
        max_retries=2,
    )


def _openai_health(api_key: str = "", model: str = "") -> tuple[bool, str]:
    model = model or config.OPENAI_MODEL
    try:
        _openai_client(api_key, timeout=10).models.retrieve(model)
    except LLMError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Cannot use OpenAI model '{model}': {type(exc).__name__}. Check the API key and model access."
    return True, f"OpenAI ready ({model})"


def call_tool(system: str, user: str, name: str, description: str, parameters: dict,
              max_tokens: int = 2000, timeout: float = None) -> dict:
    """OpenAI function calling: the model must call the tool ``name``; returns the arguments it filled in."""
    if config.LLM_PROVIDER != "openai":
        raise LLMError("Tool calling needs the OpenAI provider.")
    limit = min(timeout, config.OPENAI_TIMEOUT) if timeout else config.OPENAI_TIMEOUT
    if limit <= 0:
        raise LLMError("No time left in the thinking budget")
    request = {
        "model": config.OPENAI_MODEL,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_tokens,
        "store": False,
        "reasoning": {"effort": config.OPENAI_REASONING_EFFORT},
        "tools": [{"type": "function", "name": name, "description": description, "parameters": parameters,
                   "strict": False}],
        "tool_choice": {"type": "function", "name": name},
    }
    response = _openai_create(request, limit)
    if _cut_off(response):
        request["max_output_tokens"] = min(max_tokens * 2, 16000)
        response = _openai_create(request, limit)
        if _cut_off(response):
            raise LLMError(f"OpenAI's {name} call was cut off at the {request['max_output_tokens']}-token limit.")
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "function_call" and getattr(item, "name", None) == name:
            try:
                arguments = json.loads(getattr(item, "arguments", None) or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(f"OpenAI's {name} call had unreadable arguments.") from exc
            if not isinstance(arguments, dict):
                raise LLMError(f"OpenAI's {name} call had unreadable arguments.")
            return arguments
    raise LLMError(f"OpenAI did not call the {name} tool.")


def _openai_create(request: dict, limit: float):
    try:
        return _openai_client(timeout=limit).responses.create(**request)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"OpenAI request failed: {type(exc).__name__}. "
                       "Check the API key, model access, and network.") from exc


def _cut_off(response) -> bool:
    details = getattr(response, "incomplete_details", None)
    return (getattr(response, "status", None) == "incomplete"
            and getattr(details, "reason", None) == "max_output_tokens")


def _complete_openai(system: str, user: str, max_tokens: int, schema: dict | None,
                     timeout: float | None) -> str:
    limit = min(timeout, config.OPENAI_TIMEOUT) if timeout else config.OPENAI_TIMEOUT
    if limit <= 0:
        raise LLMError("No time left in the thinking budget")
    request = {
        "model": config.OPENAI_MODEL,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_tokens,
        # API responses are stored by default; analytics prompts may contain
        # sensitive aggregates, so this integration opts out explicitly.
        "store": False,
        "reasoning": {"effort": config.OPENAI_REASONING_EFFORT},
    }
    if schema:
        request["text"] = {"format": {
            "type": "json_schema",
            "name": "datamyner_response",
            "schema": schema,
            # Existing app schemas intentionally contain optional properties.
            "strict": False,
        }}
    response = _openai_create(request, limit)
    if _cut_off(response):
        # A reply cut off at the output limit is broken JSON or half a SQL query, which used to surface as
        # "could not be parsed" or "no valid plan". Ask once more with room to finish.
        request["max_output_tokens"] = min(max_tokens * 2, 16000)
        response = _openai_create(request, limit)
        if _cut_off(response):
            raise LLMError(f"OpenAI's reply was cut off at the {request['max_output_tokens']}-token output limit.")
    text = getattr(response, "output_text", "") or ""
    if not text.strip():
        raise LLMError("OpenAI returned no text output.")
    return text.strip()


# Circuit breaker: when Ollama refuses connections, fail instantly for a short
# while instead of paying retry back-off on every call (deep thinking makes
# several calls per question, which added ~9 s when the model was down).
_CIRCUIT_SECONDS = 15.0
_down_until = 0.0


def recently_unavailable() -> bool:
    """True while the circuit breaker is open, i.e. a call failed moments ago.

    Lets an optional extra call (such as wording an answer that is already complete without it) be
    skipped outright instead of paying the connection timeout again.
    """
    import time

    return time.monotonic() < _down_until


def _post_with_retry(url: str, payload: dict, attempts: int = 3, timeout: float = None):
    """POST with exponential back-off on connection errors and 5xx responses."""
    import time

    global _down_until
    if time.monotonic() < _down_until:
        raise OllamaError(f"Cannot reach Ollama at {config.OLLAMA_HOST} (retrying shortly).")
    timeout = timeout or config.OLLAMA_TIMEOUT
    give_up_at = time.monotonic() + timeout
    delay = 1.0
    last_exc = None
    for attempt in range(attempts):
        try:
            left = give_up_at - time.monotonic()
            if left <= 0:
                raise OllamaError(f"Ollama did not answer within {timeout:.0f}s")
            resp = requests.post(url, json=payload, timeout=(min(5, left), left))
        except requests.ConnectionError as exc:
            last_exc = exc
        except requests.Timeout as exc:
            raise OllamaError(f"Ollama timed out after {timeout:.0f}s") from exc
        except requests.RequestException as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        else:
            if resp.status_code < 500 or attempt == attempts - 1:
                return resp
        if attempt < attempts - 1 and give_up_at - time.monotonic() > delay:
            time.sleep(delay)
            delay *= 2
    _down_until = time.monotonic() + _CIRCUIT_SECONDS
    raise OllamaError(f"Ollama request failed: {last_exc}")


def embed(texts: list[str], model: str = None, batch_size: int = 64, progress=False):
    """Embed a list of strings with nomic-embed-text. Returns an (n, d) array."""
    import numpy as np

    model = model or config.EMBED_MODEL
    vectors = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        try:
            resp = requests.post(
                f"{config.OLLAMA_HOST}/api/embed",
                json={"model": model, "input": chunk, "keep_alive": config.KEEP_ALIVE},
                timeout=config.OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            vectors.extend(resp.json()["embeddings"])
        except (requests.RequestException, KeyError):
            # Fall back to the older single-input endpoint.
            for text in chunk:
                resp = requests.post(
                    f"{config.OLLAMA_HOST}/api/embeddings",
                    json={"model": model, "prompt": text},
                    timeout=config.OLLAMA_TIMEOUT,
                )
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"])
        if progress:
            print(f"  embedded {min(start + batch_size, len(texts))}/{len(texts)}", end="\r")
    if progress:
        print()

    arr = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-9, None)  # normalised, so dot product = cosine
