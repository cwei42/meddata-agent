"""Model backends. The harness talks to any OpenAI-compatible chat endpoint
(llama-server, vLLM, Ollama, ...). Swap models by changing the config, not code."""
import requests


class OpenAICompatModel:
    def __init__(self, url, model=None, temperature=0.2, max_tokens=1200, extra=None, timeout=600):
        self.url = url.rstrip("/")
        if not self.url.endswith("/chat/completions"):
            self.url += "/v1/chat/completions"
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self.timeout = timeout

    def health(self):
        base = self.url.split("/v1/")[0]
        try:
            return requests.get(base + "/health", timeout=5).ok
        except requests.RequestException:
            return False

    def name(self):
        try:
            data = requests.get(self.url.replace("/chat/completions", "/models"), timeout=5).json()
            return data["data"][0]["id"].rsplit("/", 1)[-1]
        except Exception:
            return self.model or "unknown"

    def chat(self, messages, tools=None):
        body = {"messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.model:
            body["model"] = self.model
        if tools:
            body["tools"] = tools
        body.update(self.extra)
        r = requests.post(self.url, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]


BACKENDS = {"openai": OpenAICompatModel}


def make_model(cfg: dict):
    cfg = dict(cfg)
    kind = cfg.pop("backend", "openai")
    return BACKENDS[kind](**cfg)
