"""Internal: the boundary between the ORM and the SDK (v0.23.0).

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

``sdk_decodes_zero_duration()`` is the one local SDK capability the rich field types depend on:
SDK 2.0.0 cannot decode a zero duration (CBOR tag 14 with an empty compact form raises
``IndexError``), so the ORM refuses to send one — in a bound parameter or a live-query literal —
while the probe answers ``False``. A fixed SDK lifts the refusal by itself. Call it through this
module (``encoding.sdk_decodes_zero_duration()``) so a test can replace it.
"""

from __future__ import annotations

import functools

from . import _sdk

__all__ = ["sdk_decodes_zero_duration"]


@functools.cache
def sdk_decodes_zero_duration() -> bool:
    """Whether the installed SDK reads a zero duration back (computed once per process, no I/O).

    Round-trips ``CBORTag(TAG_DURATION_COMPACT, [])`` — the server's compact form of ``0ns`` —
    through the SDK's own CBOR codec and answers ``True`` only when that gives an SDK ``Duration``
    equal to zero. ``False`` on any exception, and when the shim found no codec in the installed SDK
    (``_sdk.cbor_encode``/``_sdk.cbor_decode`` is ``None``); both are read at call time.
    """
    encode = _sdk.cbor_encode
    decode = _sdk.cbor_decode
    if encode is None or decode is None:
        return False
    try:
        value = decode(encode(_sdk.CBORTag(_sdk.TAG_DURATION_COMPACT, [])))
        return bool(isinstance(value, _sdk.SdkDuration) and value.elapsed == 0)
    except Exception:  # any failure means "cannot read it back"
        return False
