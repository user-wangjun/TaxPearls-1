"""Shared bounded Chat Completions transport; errors never include payloads."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TransportError(ValueError):
    def __init__(self, kind, status=None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def chat_content(settings, messages, timeout, *, temperature=0, max_tokens=None):
    payload = {"model": settings.effective_model, "messages": messages, "stream": False,
               "max_tokens": max_tokens or settings.max_tokens, "temperature": temperature}
    if settings.json_mode:
        payload["response_format"] = {"type": "json_object"}
    if settings.disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    request = Request(settings.base_url + "/chat/completions",
                      data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type":"application/json", "Authorization":"Bearer " + settings.api_key},
                      method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise TransportError("oversized")
        choice = json.loads(raw)["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise TransportError("incomplete")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise TransportError("empty")
        return content.strip()
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise TransportError("http", status) from None
    except TimeoutError:
        raise TransportError("timeout") from None
    except URLError:
        raise TransportError("network") from None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise TransportError("schema") from None
