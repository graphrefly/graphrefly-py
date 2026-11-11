"""Canonical protobuf codec for the wire bridge (graphrefly:D497, D803).

A strict hand-written codec for ``WireBridgeEnvelope`` and ``WireEdgeCause`` over
graphrefly spec/protocol.proto, with no dependency. A valid canonical byte sequence
decodes to a semantic value, passes validation, re-encodes deterministically and
matches the original bytes exactly; anything else is refused with one of the D497
error categories. Private: the public package exposes bytes and frames, not these
envelope values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from graphrefly._kernel.messages import COMPLETE, DATA, ERROR, INVALIDATE, Message

type Category = Literal[
    "unknown_field",
    "duplicate_singular",
    "noncanonical_bytes",
    "invalid_oneof",
    "missing_required",
    "invalid_wire_edge",
    "default_emission",
    "malformed",
]


class ProtobufError(ValueError):
    """Bytes refused by the canonical profile, with its D497 category."""

    def __init__(self, category: Category, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category: Category = category
        self.detail = message


# ---- semantic values ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WireEdgeArrays:
    """One edge of a cause: its id and the arrays it brought, in order (R-bridge).

    Each array is a tuple of ``(DATA, bytes)``, ``(ERROR, bytes)``, ``(INVALIDATE,)``
    and ``(COMPLETE,)`` messages.
    """

    edge_id: str
    arrays: tuple[tuple[Message, ...], ...]


@dataclass(frozen=True, slots=True)
class WireEdgeCause:
    """One cause of a wire edge group: one sender wave, sent as one frame (R-bridge).

    ``session`` names one lifetime of the group's outbound node and ``cause`` counts
    from 1 within it.
    """

    group: str
    session: str
    cause: int
    edges: tuple[WireEdgeArrays, ...]


@dataclass(frozen=True, slots=True)
class Metadata:
    seq: int
    cursor: int
    idempotency_key: str
    attempt: int
    max_attempts: int
    timestamp_ms: int | None = None
    ack_for_seq: int | None = None
    request_id: str | None = None


type PayloadKind = Literal["start", "data", "ack", "nack", "status", "error", "close"]


@dataclass(frozen=True, slots=True)
class Envelope:
    """A wire bridge envelope. ``payload`` names the oneof case; ``value`` is the DATA
    value bytes, ``edge_cause`` a DATA edge cause, and ``detail`` the bytes of a NACK
    error, STATUS, ERROR or CLOSE reason."""

    session_id: str
    metadata: Metadata
    payload: PayloadKind
    value: bytes | None = None
    edge_cause: WireEdgeCause | None = None
    detail: bytes | None = None


# ---- wire reading ---------------------------------------------------------------------

_VARINT: Final = 0
_LEN: Final = 2

_MESSAGE_CODES: Final = {
    1: "START",
    2: "PENDING",
    3: DATA,
    4: "RESOLVED",
    5: INVALIDATE,
    6: COMPLETE,
    7: ERROR,
}
_CODE_OF: Final = {DATA: 3, INVALIDATE: 5, COMPLETE: 6, ERROR: 7}
_CROSSING: Final = frozenset({DATA, INVALIDATE, COMPLETE, ERROR})


@dataclass(frozen=True, slots=True)
class _Field:
    no: int
    value: int | bytes


def _varint(data: bytes, at: int) -> tuple[int, int]:
    result = 0
    shift = 0
    for _ in range(10):
        if at >= len(data):
            raise ProtobufError("malformed", "truncated varint")
        byte = data[at]
        at += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if result >= 1 << 64:
                raise ProtobufError("malformed", "varint exceeds 64 bits")
            return result, at
        shift += 7
    raise ProtobufError("malformed", "varint is too long")


def _read(
    data: bytes,
    allowed: dict[int, int],
    name: str,
    repeated: frozenset[int] = frozenset(),
) -> list[_Field]:
    fields: list[_Field] = []
    seen: set[int] = set()
    at = 0
    while at < len(data):
        key, at = _varint(data, at)
        no, wire_type = key >> 3, key & 7
        if no not in allowed:
            raise ProtobufError("unknown_field", f"{name} contains unknown field {no}")
        if wire_type != allowed[no]:
            raise ProtobufError("malformed", f"{name} field {no} has wrong wire type")
        if no in seen and no not in repeated:
            raise ProtobufError("duplicate_singular", f"{name} field {no} is duplicated")
        seen.add(no)
        if wire_type == _VARINT:
            value, at = _varint(data, at)
            fields.append(_Field(no, value))
        else:
            length, at = _varint(data, at)
            if at + length > len(data):
                raise ProtobufError("malformed", f"{name} field {no} is truncated")
            fields.append(_Field(no, bytes(data[at : at + length])))
            at += length
    return fields


def _one(fields: list[_Field], no: int) -> int | bytes | None:
    for field in fields:
        if field.no == no:
            return field.value
    return None


def _all(fields: list[_Field], no: int) -> list[bytes]:
    return [f.value for f in fields if f.no == no and isinstance(f.value, bytes)]


def _text(raw: bytes, field: str) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtobufError("malformed", f"{field} is not valid UTF-8") from None
    if not value:
        raise ProtobufError("missing_required", f"{field} must be non-empty")
    return value


# ---- wire writing ------------------------------------------------------------------------


def _put_varint(out: bytearray, value: int) -> None:
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _put_uint(out: bytearray, no: int, value: int) -> None:
    _put_varint(out, no << 3 | _VARINT)
    _put_varint(out, value)


def _put_bytes(out: bytearray, no: int, value: bytes) -> None:
    _put_varint(out, no << 3 | _LEN)
    _put_varint(out, len(value))
    out += value


# ---- WireEdgeCause -------------------------------------------------------------------------


def _decode_message(raw: bytes) -> Message:
    fields = _read(raw, {1: _VARINT, 2: _LEN}, "Message")
    code = _one(fields, 1)
    data = _one(fields, 2)
    kind = _MESSAGE_CODES.get(code) if isinstance(code, int) else None
    if kind is None:
        raise ProtobufError("invalid_wire_edge", f"message type {code!r} is not a message")
    if kind not in _CROSSING:
        raise ProtobufError("invalid_wire_edge", f"{kind} is the kernel's own and does not cross")
    if kind in (DATA, ERROR):
        if not isinstance(data, bytes):
            raise ProtobufError("invalid_wire_edge", f"{kind} requires data bytes")
        return (kind, data)
    if data is not None:
        raise ProtobufError("invalid_wire_edge", f"{kind} must not carry data")
    return (kind,)


def _decode_arrays(raw: bytes) -> WireEdgeArrays:
    fields = _read(raw, {1: _LEN, 2: _LEN}, "WireEdgeArrays", frozenset({2}))
    edge = _one(fields, 1)
    if not isinstance(edge, bytes):
        raise ProtobufError("missing_required", "an edge has no edge_id")
    edge_id = _text(edge, "edge_id")
    arrays: list[tuple[Message, ...]] = []
    for wave in _all(fields, 2):
        wave_fields = _read(wave, {1: _LEN}, "Wave", frozenset({1}))
        arrays.append(tuple(_decode_message(m) for m in _all(wave_fields, 1)))
    return WireEdgeArrays(edge_id, tuple(arrays))


def validate_cause(cause: WireEdgeCause) -> None:
    """Check a cause against R-bridge's frame shape; raise ProtobufError otherwise."""

    if not cause.group:
        raise ProtobufError("missing_required", "WireEdgeCause has no group")
    if not cause.session:
        raise ProtobufError("missing_required", "WireEdgeCause has no session")
    if cause.cause < 1:
        raise ProtobufError("missing_required", "WireEdgeCause has no cause number")
    if not cause.edges:
        raise ProtobufError("invalid_wire_edge", "a cause carries no edge")
    seen: set[str] = set()
    for edge in cause.edges:
        if not edge.edge_id:
            raise ProtobufError("missing_required", "an edge has no edge_id")
        if edge.edge_id in seen:
            raise ProtobufError("invalid_wire_edge", f"edge {edge.edge_id} twice")
        seen.add(edge.edge_id)
        if not edge.arrays:
            raise ProtobufError("invalid_wire_edge", f"edge {edge.edge_id} carries no array")
        ended = False
        for array in edge.arrays:
            if not array:
                raise ProtobufError("invalid_wire_edge", f"edge {edge.edge_id}: an empty array")
            if ended:
                raise ProtobufError(
                    "invalid_wire_edge", f"edge {edge.edge_id}: an array after its terminal"
                )
            for index, message in enumerate(array):
                kind = message[0]
                if kind not in _CROSSING:
                    raise ProtobufError("invalid_wire_edge", f"{kind} does not cross")
                if kind in (DATA, ERROR) and not isinstance(message[1], bytes):
                    raise ProtobufError("invalid_wire_edge", f"{kind} without bytes")
                if kind in (COMPLETE, ERROR):
                    if index != len(array) - 1:
                        raise ProtobufError(
                            "invalid_wire_edge", f"edge {edge.edge_id}: a terminal not last"
                        )
                    ended = True


def _parse_cause(raw: bytes) -> WireEdgeCause:
    fields = _read(raw, {1: _LEN, 2: _LEN, 3: _VARINT, 4: _LEN}, "WireEdgeCause", frozenset({4}))
    group = _one(fields, 1)
    session = _one(fields, 2)
    number = _one(fields, 3)
    if not isinstance(group, bytes):
        raise ProtobufError("missing_required", "WireEdgeCause has no group")
    if not isinstance(session, bytes):
        raise ProtobufError("missing_required", "WireEdgeCause has no session")
    if not isinstance(number, int):
        raise ProtobufError("missing_required", "WireEdgeCause has no cause number")
    return WireEdgeCause(
        _text(group, "group"),
        _text(session, "session"),
        number,
        tuple(_decode_arrays(e) for e in _all(fields, 4)),
    )


def _encode_cause(cause: WireEdgeCause) -> bytes:
    out = bytearray()
    _put_bytes(out, 1, cause.group.encode())
    _put_bytes(out, 2, cause.session.encode())
    _put_uint(out, 3, cause.cause)
    for edge in cause.edges:
        edge_out = bytearray()
        _put_bytes(edge_out, 1, edge.edge_id.encode())
        for array in edge.arrays:
            wave = bytearray()
            for message in array:
                m = bytearray()
                _put_uint(m, 1, _CODE_OF[message[0]])
                if message[0] in (DATA, ERROR):
                    _put_bytes(m, 2, message[1])
                _put_bytes(wave, 1, bytes(m))
            _put_bytes(edge_out, 2, bytes(wave))
        _put_bytes(out, 4, bytes(edge_out))
    return bytes(out)


def encode_cause(cause: WireEdgeCause) -> bytes:
    """Encode a cause as canonical ``WireEdgeCause`` bytes."""

    validate_cause(cause)
    return _encode_cause(cause)


def decode_cause(raw: bytes) -> WireEdgeCause:
    """Decode canonical ``WireEdgeCause`` bytes, refusing anything else."""

    cause = _parse_cause(bytes(raw))
    validate_cause(cause)
    if _encode_cause(cause) != raw:
        raise ProtobufError("noncanonical_bytes", "WireEdgeCause bytes are not canonical")
    return cause


# ---- WireBridgeEnvelope ------------------------------------------------------------------


def _parse_metadata(raw: bytes) -> Metadata:
    fields = _read(
        raw,
        {1: _VARINT, 2: _VARINT, 3: _LEN, 4: _VARINT, 5: _VARINT, 6: _VARINT, 7: _VARINT, 8: _LEN},
        "WireBridgeMetadata",
    )
    seq, cursor, key, attempt, max_attempts = (_one(fields, n) for n in (1, 2, 3, 4, 5))
    if not (
        isinstance(seq, int)
        and isinstance(cursor, int)
        and isinstance(key, bytes)
        and isinstance(attempt, int)
        and isinstance(max_attempts, int)
    ):
        raise ProtobufError("missing_required", "WireBridgeMetadata missing required fields")
    timestamp, ack_for, request = _one(fields, 6), _one(fields, 7), _one(fields, 8)
    if timestamp == 0 or ack_for == 0 or request == b"":
        raise ProtobufError("default_emission", "optional metadata default value was emitted")
    for label, value in (("attempt", attempt), ("max_attempts", max_attempts)):
        if value >= 1 << 32:
            raise ProtobufError("malformed", f"{label} exceeds uint32")
    return Metadata(
        seq,
        cursor,
        _text(key, "idempotency_key"),
        attempt,
        max_attempts,
        timestamp if isinstance(timestamp, int) else None,
        ack_for if isinstance(ack_for, int) else None,
        _text(request, "request_id") if isinstance(request, bytes) else None,
    )


def _optional_detail(raw: bytes, name: str) -> bytes | None:
    fields = _read(raw, {1: _LEN}, f"WireBridge{name}Payload")
    value = _one(fields, 1)
    if value == b"":
        raise ProtobufError("default_emission", f"{name} optional bytes default value was emitted")
    return value if isinstance(value, bytes) else None


def _required_detail(raw: bytes, name: str) -> bytes:
    fields = _read(raw, {1: _LEN}, f"WireBridge{name}Payload")
    value = _one(fields, 1)
    if not isinstance(value, bytes) or not value:
        raise ProtobufError("missing_required", f"{name} payload bytes must be non-empty")
    return value


_PAYLOADS: Final[dict[int, PayloadKind]] = {
    3: "start",
    4: "data",
    5: "ack",
    6: "nack",
    7: "status",
    8: "error",
    9: "close",
}
_PAYLOAD_NO: Final = {kind: no for no, kind in _PAYLOADS.items()}


def _parse_envelope(raw: bytes) -> Envelope:
    fields = _read(raw, {n: _LEN for n in range(1, 10)}, "WireBridgeEnvelope")
    session, metadata = _one(fields, 1), _one(fields, 2)
    payloads = [f for f in fields if 3 <= f.no <= 9]
    if not isinstance(session, bytes) or not isinstance(metadata, bytes) or not payloads:
        raise ProtobufError("missing_required", "WireBridgeEnvelope missing required fields")
    if len(payloads) != 1:
        raise ProtobufError("invalid_oneof", "WireBridgeEnvelope payload oneof has several cases")
    field = payloads[0]
    body = field.value
    assert isinstance(body, bytes)
    kind = _PAYLOADS[field.no]
    session_id = _text(session, "session_id")
    meta = _parse_metadata(metadata)
    if kind in ("start", "ack"):
        if body:
            raise ProtobufError("unknown_field", f"{kind} payload must be empty")
        return Envelope(session_id, meta, kind)
    if kind == "data":
        data_fields = _read(body, {1: _LEN, 3: _LEN}, "WireBridgeDataPayload")
        value, edge_cause = _one(data_fields, 1), _one(data_fields, 3)
        if value is not None and edge_cause is not None:
            raise ProtobufError("invalid_oneof", "WireBridgeDataPayload body has several cases")
        if isinstance(value, bytes):
            return Envelope(session_id, meta, kind, value=value)
        if isinstance(edge_cause, bytes):
            return Envelope(session_id, meta, kind, edge_cause=_parse_cause(edge_cause))
        raise ProtobufError("missing_required", "WireBridgeDataPayload has no body")
    if kind in ("nack", "close"):
        return Envelope(session_id, meta, kind, detail=_optional_detail(body, kind))
    return Envelope(session_id, meta, kind, detail=_required_detail(body, kind))


def validate_envelope(envelope: Envelope) -> None:
    """Check an envelope's semantics (D497); raise ProtobufError otherwise."""

    meta = envelope.metadata
    if (
        not envelope.session_id
        or meta.seq == 0
        or meta.attempt == 0
        or meta.max_attempts < meta.attempt
        or not meta.idempotency_key
    ):
        raise ProtobufError("missing_required", "WireBridgeEnvelope required semantics are invalid")
    if envelope.payload in ("ack", "nack") and meta.ack_for_seq is None:
        raise ProtobufError("missing_required", "ACK/NACK requires metadata.ack_for_seq")
    if meta.timestamp_ms == 0 or meta.ack_for_seq == 0 or meta.request_id == "":
        raise ProtobufError("default_emission", "optional metadata default value was emitted")
    if envelope.payload == "data":
        if (envelope.value is None) == (envelope.edge_cause is None):
            raise ProtobufError("invalid_oneof", "a DATA envelope carries exactly one body")
        if envelope.edge_cause is not None:
            validate_cause(envelope.edge_cause)
    if envelope.payload in ("nack", "close") and envelope.detail == b"":
        raise ProtobufError("default_emission", f"{envelope.payload} optional bytes are empty")
    if envelope.payload in ("status", "error") and not envelope.detail:
        raise ProtobufError(
            "missing_required", f"{envelope.payload} payload bytes must be non-empty"
        )


def _encode_envelope(envelope: Envelope) -> bytes:
    meta = envelope.metadata
    m = bytearray()
    _put_uint(m, 1, meta.seq)
    _put_uint(m, 2, meta.cursor)
    _put_bytes(m, 3, meta.idempotency_key.encode())
    _put_uint(m, 4, meta.attempt)
    _put_uint(m, 5, meta.max_attempts)
    if meta.timestamp_ms is not None:
        _put_uint(m, 6, meta.timestamp_ms)
    if meta.ack_for_seq is not None:
        _put_uint(m, 7, meta.ack_for_seq)
    if meta.request_id is not None:
        _put_bytes(m, 8, meta.request_id.encode())
    body = bytearray()
    if envelope.payload == "data":
        if envelope.value is not None:
            _put_bytes(body, 1, envelope.value)
        else:
            assert envelope.edge_cause is not None
            _put_bytes(body, 3, _encode_cause(envelope.edge_cause))
    elif envelope.detail is not None:
        _put_bytes(body, 1, envelope.detail)
    out = bytearray()
    _put_bytes(out, 1, envelope.session_id.encode())
    _put_bytes(out, 2, bytes(m))
    _put_bytes(out, _PAYLOAD_NO[envelope.payload], bytes(body))
    return bytes(out)


def encode_envelope(envelope: Envelope) -> bytes:
    """Encode an envelope as canonical ``WireBridgeEnvelope`` bytes."""

    validate_envelope(envelope)
    return _encode_envelope(envelope)


def decode_envelope(raw: bytes | bytearray | Sequence[int]) -> Envelope:
    """Decode canonical ``WireBridgeEnvelope`` bytes, refusing anything else."""

    data = bytes(raw)
    envelope = _parse_envelope(data)
    validate_envelope(envelope)
    if _encode_envelope(envelope) != data:
        raise ProtobufError("noncanonical_bytes", "WireBridgeEnvelope bytes are not canonical")
    return envelope
