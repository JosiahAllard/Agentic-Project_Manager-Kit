"""Validated internal message records; construction supplies no identity authority."""
from dataclasses import dataclass, field
import hashlib
import math

from kitlib.ident import digest, identifier
from .pairing import ChatBinding

REPLY_OPERATION_PREFIX = 'bridge-reply-'


def reply_operation_id(parent):
    identifier(parent)
    return REPLY_OPERATION_PREFIX + hashlib.sha256(('bridge-reply-v1:'+parent).encode('ascii')).hexdigest()


MAX_TEXT_BYTES = 64 * 1024
NATIVE_STATES = frozenset(('recipient-accepted', 'held', 'reply-received', 'refused', 'expired', 'failed'))
TERMINAL_STATES = frozenset(('reply-received', 'refused', 'expired', 'failed'))


class DeliveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('bridge delivery rejected: ' + code)


class BeforeWriteError(DeliveryError):
    """Only a trusted adapter may assert that it sent no native bytes."""


def text_bytes(value):
    if type(value) is not str:
        raise DeliveryError('invalid-message-text')
    try:
        data = value.encode('utf-8')
    except UnicodeError:
        raise DeliveryError('invalid-message-text') from None
    if len(data) > MAX_TEXT_BYTES:
        raise DeliveryError('message-text-limit')
    return data


def expiry(value):
    if value is not None and (type(value) not in (int, float) or not 0 < value <= 1e12 or not math.isfinite(value)):
        raise DeliveryError('invalid-message-expiry')


@dataclass(frozen=True)
class SenderAuthority:
    sender: ChatBinding
    task_root: str

    def __post_init__(self):
        if type(self.sender) is not ChatBinding:
            raise DeliveryError('invalid-sender-authority')
        identifier(self.task_root)


@dataclass(frozen=True)
class MessageIntent:
    operation_id: str
    recipient: ChatBinding
    body_sha256: str
    expires_at: float | None = None

    def __post_init__(self):
        identifier(self.operation_id)
        if type(self.recipient) is not ChatBinding:
            raise DeliveryError('invalid-recipient-binding')
        digest(self.body_sha256)
        expiry(self.expires_at)


@dataclass(frozen=True)
class OutboundMessage:
    operation_id: str
    task_root: str
    sender: ChatBinding
    recipient: ChatBinding
    text: str = field(repr=False)
    expires_at: float | None = None
    reply_of: str | None = None

    def __post_init__(self):
        identifier(self.operation_id)
        identifier(self.task_root)
        if self.reply_of is not None:
            identifier(self.reply_of)
            if self.operation_id!=reply_operation_id(self.reply_of):
                raise DeliveryError('invalid-reply-delivery-link')
        if type(self.sender) is not ChatBinding or type(self.recipient) is not ChatBinding:
            raise DeliveryError('invalid-message-binding')
        text_bytes(self.text)
        expiry(self.expires_at)

    @property
    def intent(self):
        return MessageIntent(self.operation_id, self.recipient, hashlib.sha256(text_bytes(self.text)).hexdigest(),
                             self.expires_at)


@dataclass(frozen=True)
class NativeResult:
    operation_id: str
    recipient_ref: str
    state: str
    native_id: str
    reply: str | None = field(default=None, repr=False)

    def __post_init__(self):
        identifier(self.operation_id)
        digest(self.recipient_ref)
        identifier(self.native_id)
        if type(self.state) is not str or self.state not in NATIVE_STATES:
            raise DeliveryError('invalid-native-state')
        if self.state == 'reply-received':
            text_bytes(self.reply)
        elif self.reply is not None:
            raise DeliveryError('unexpected-native-reply')


@dataclass(frozen=True)
class DeliveryReceipt:
    operation_id: str
    state: str
    broker_epoch: int
    native_id: str | None
    reason: str | None
    body_sha256: str
    reply_sha256: str | None


@dataclass(frozen=True)
class DeliveryEvent:
    cursor: int
    operation_id: str
    state: str
    broker_epoch: int
