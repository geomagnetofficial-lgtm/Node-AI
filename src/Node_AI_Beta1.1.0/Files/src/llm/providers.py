from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    protocol: str
    base_url: str
    env_key: str = ""
    local: bool = False
    needs_key: bool = True


_P = [
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    Provider("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    Provider("google", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    Provider("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", "XAI_API_KEY"),
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    Provider("together", "Together AI", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    Provider("custom", "Otro (compatible OpenAI, URL propia)", "openai", "", "CUSTOM_API_KEY"),
    Provider("gguf", "Archivo GGUF (motor integrado llama.cpp)", "gguf", "", local=True, needs_key=False),
    Provider("ollama", "Ollama (servidor local)", "openai", "http://localhost:11434/v1", local=True, needs_key=False),
    Provider("llamacpp", "llama.cpp server", "openai", "http://localhost:8080/v1", local=True, needs_key=False),
    Provider("lmstudio", "LM Studio", "openai", "http://localhost:1234/v1", local=True, needs_key=False),
]

PROVIDERS: dict[str, Provider] = {p.key: p for p in _P}


def providers_for(mode: str) -> list[Provider]:
    want_local = mode == "local"
    return [p for p in _P if p.local == want_local]


def get_provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise ValueError(f"Proveedor desconocido: '{key}'") from None
