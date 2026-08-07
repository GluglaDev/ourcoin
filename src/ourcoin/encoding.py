"""Deterministic binary primitives used by future consensus structures."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from unicodedata import normalize

U8_MAX = (1 << 8) - 1
U16_MAX = (1 << 16) - 1
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1


class EncodingError(ValueError):
    """Raised when data has no valid canonical representation."""


def _encode_unsigned(value: int, width: int, maximum: int) -> bytes:
    if type(value) is not int:
        raise EncodingError("unsigned integer must be an int, not bool or another type")
    if not 0 <= value <= maximum:
        raise EncodingError(f"unsigned integer does not fit in {width} bytes")
    return value.to_bytes(width, "big")


def encode_u8(value: int) -> bytes:
    return _encode_unsigned(value, 1, U8_MAX)


def encode_u16(value: int) -> bytes:
    return _encode_unsigned(value, 2, U16_MAX)


def encode_u32(value: int) -> bytes:
    return _encode_unsigned(value, 4, U32_MAX)


def encode_u64(value: int) -> bytes:
    return _encode_unsigned(value, 8, U64_MAX)


def encode_bytes(value: bytes) -> bytes:
    """Encode bytes with an unsigned 32-bit length prefix."""

    if type(value) is not bytes:
        raise EncodingError("byte field must be bytes")
    if len(value) > U32_MAX:
        raise EncodingError("byte field is too long")
    return encode_u32(len(value)) + value


def encode_text(value: str) -> bytes:
    """Encode text as NFC-normalized UTF-8 with a byte-length prefix."""

    if type(value) is not str:
        raise EncodingError("text field must be str")
    canonical = normalize("NFC", value)
    return encode_bytes(canonical.encode("utf-8"))


def encode_sequence(values: Sequence[bytes]) -> bytes:
    """Encode an ordered sequence of length-prefixed byte fields."""

    if len(values) > U32_MAX:
        raise EncodingError("sequence contains too many items")
    return encode_u32(len(values)) + b"".join(encode_bytes(value) for value in values)


def _validate_optional_limit(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise EncodingError(f"{name} must be a non-negative int or None")


@dataclass(slots=True)
class CanonicalReader:
    """Strict, forward-only reader for canonical binary primitives."""

    data: bytes
    _offset: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.data) is not bytes:
            raise EncodingError("reader input must be bytes")

    @property
    def remaining(self) -> int:
        return len(self.data) - self._offset

    def _take(self, length: int) -> bytes:
        if length < 0 or length > self.remaining:
            raise EncodingError("truncated canonical data")
        start = self._offset
        self._offset += length
        return self.data[start : self._offset]

    def _read_unsigned(self, width: int) -> int:
        return int.from_bytes(self._take(width), "big")

    def read_u8(self) -> int:
        return self._read_unsigned(1)

    def read_u16(self) -> int:
        return self._read_unsigned(2)

    def read_u32(self) -> int:
        return self._read_unsigned(4)

    def read_u64(self) -> int:
        return self._read_unsigned(8)

    def read_bytes(self, *, max_length: int | None = None) -> bytes:
        _validate_optional_limit(max_length, "max_length")
        length = self.read_u32()
        if max_length is not None and length > max_length:
            raise EncodingError("byte field exceeds its allowed length")
        return self._take(length)

    def read_text(self, *, max_length: int | None = None) -> str:
        encoded = self.read_bytes(max_length=max_length)
        try:
            value = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise EncodingError("text field is not valid UTF-8") from error
        if normalize("NFC", value) != value:
            raise EncodingError("text field is not in canonical NFC form")
        return value

    def read_sequence(
        self,
        *,
        max_items: int | None = None,
        max_item_length: int | None = None,
    ) -> tuple[bytes, ...]:
        _validate_optional_limit(max_items, "max_items")
        _validate_optional_limit(max_item_length, "max_item_length")
        count = self.read_u32()
        if max_items is not None and count > max_items:
            raise EncodingError("sequence exceeds its allowed item count")
        return tuple(self.read_bytes(max_length=max_item_length) for _ in range(count))

    def ensure_finished(self) -> None:
        if self.remaining != 0:
            raise EncodingError("trailing bytes after canonical value")
