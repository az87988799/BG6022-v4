"""Pure declarations for enabled ORCA evidence and explicit scientific rules."""

import re

LEGACY_CHECK_VERSION = "orca-hf-1"
CURRENT_CHECK_VERSION = "orca-hf-2"
OPI_MINIMUM_ORCA_VERSION = "6.1.1"
SUPPORTED_ORCA_VERSIONS = ("6.1.1",)


def orca_version_tokens(text: str) -> tuple[str, ...]:
    """Preserve whole banner tokens, including unsupported build suffixes."""
    return tuple(re.findall(r"^[ \t]*Program Version[ \t]+([^\s]+)", text, re.M))


def extract_orca_version(text: str) -> str | None:
    """A missing or repeated banner is insufficient to identify one program."""
    tokens = orca_version_tokens(text)
    declarations = re.findall(r"^[ \t]*Program Version(?=[ \t\r\n]|$)", text, re.M)
    return tokens[0] if len(tokens) == len(declarations) == 1 else None


def is_supported_orca_version(token: str | None) -> bool:
    return token in SUPPORTED_ORCA_VERSIONS
