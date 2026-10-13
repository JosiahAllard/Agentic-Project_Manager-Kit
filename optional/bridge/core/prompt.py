"""Shared recipient instructions for explicitly correlated bridge replies.

This is message content, not a caller proof or permission grant. The core still
checks the replying chat and its current reverse connection independently.
"""
from .messages import DeliveryError, OutboundMessage


def delivery_prompt(message):
    """Keep the admitted body intact beneath its validated operation reference."""
    if type(message) is not OutboundMessage:
        raise DeliveryError('invalid-delivery-message')
    message.__post_init__()
    if message.reply_of is not None:
        return ('Reply to bridge operation: '+message.reply_of+
            '\nReply delivery: '+message.operation_id+'\n\n'+message.text)
    return ('Bridge operation: '+message.operation_id+
        '\nUse bridge_reply with this operation ID for a correlated reply.\n\n'+message.text)
