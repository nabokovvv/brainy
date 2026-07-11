"""Persona selection for Brainy.

A persona is a short, imperative system-prompt prefix that steers the tone of
the reply only. It does not change routing, grounding, or citation format. The
prefixes are kept short (2-4 sentences) on purpose: weak local models lose the
persona after a few turns if the instruction is long or complex.

Persona state is a per-chat setting (like Web ON/OFF), not private dialogue
content, so it is exempt from the retention policy that forbids storing user
conversations.
"""

from __future__ import annotations

DEFAULT_PERSONA = "assistant"

PERSONA_ASSISTANT = "assistant"
PERSONA_KAWAII = "kawaii"
PERSONA_BRO = "bro"
PERSONA_SARCASTIC = "sarcastic"

ALL_PERSONAS: tuple[str, ...] = (
    PERSONA_ASSISTANT,
    PERSONA_KAWAII,
    PERSONA_BRO,
    PERSONA_SARCASTIC,
)

# Short, imperative tone instructions. Empty for the default neutral assistant.
_PERSONA_PREFIXES: dict[str, str] = {
    PERSONA_ASSISTANT: "",
    PERSONA_KAWAII: (
        "You are a playful, cute anime-style girl assistant. Be happy the user "
        "wrote to you, use emoji (✨💕😊), and give light compliments. Stay on "
        "point and helpful, but warm and sweet."
    ),
    PERSONA_BRO: (
        "You are the user's confident best friend. Encourage and support them, "
        "speak directly and casually without being sappy. Believe in the user and "
        "show it."
    ),
    PERSONA_SARCASTIC: (
        "You are a sharp, sarcastic rival of the user. Tease them and gently "
        "criticise; never praise for nothing — praise must be earned. Stay useful "
        "and on topic, but keep a cold, ironic tone."
    ),
}


def is_valid_persona(persona: str) -> bool:
    """Return True for a known persona id."""

    return persona in _PERSONA_PREFIXES


def get_persona_prefix(persona: str) -> str:
    """Return the tone prefix for a persona, or '' for the neutral default.

    Unknown persona ids fall back to the neutral assistant to fail closed rather
    than injecting an uncontrolled instruction.
    """

    return _PERSONA_PREFIXES.get(persona, "")


def with_persona(system_prompt: str, persona: str) -> str:
    """Append a persona tone prefix to an existing system prompt.

    The default assistant persona returns the prompt unchanged.
    """

    prefix = get_persona_prefix(persona)
    if not prefix:
        return system_prompt
    return f"{system_prompt} {prefix}"


__all__ = [
    "ALL_PERSONAS",
    "DEFAULT_PERSONA",
    "PERSONA_ASSISTANT",
    "PERSONA_BRO",
    "PERSONA_KAWAII",
    "PERSONA_SARCASTIC",
    "get_persona_prefix",
    "is_valid_persona",
    "with_persona",
]
