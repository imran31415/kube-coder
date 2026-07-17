"""Channel adapters for the Conversation Gateway (issue #306).

Each adapter implements the tiny `gateway.ChannelAdapter` contract and owns ONLY
the provider specifics (signature verify, inbound parse, choice/media rendering,
outbound send). The channel-agnostic core lives in `gateway.py`.
"""

from .whatsapp import WhatsAppAdapter  # noqa: F401
from .internal import LoopbackAdapter  # noqa: F401
