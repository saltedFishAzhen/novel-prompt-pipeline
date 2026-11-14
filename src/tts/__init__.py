"""Optional clients for separately deployed text-to-speech services."""

from .client import BreezeTTSClient, TTSError
from .config import TTSConfig

__all__ = ["BreezeTTSClient", "TTSConfig", "TTSError"]
