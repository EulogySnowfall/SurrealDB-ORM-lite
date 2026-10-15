import subprocess
import sys
import textwrap
import types

import pytest


def test_sdk_shim_exports() -> None:
    from surreal_orm_lite import _sdk

    assert _sdk.RecordID is not None
    assert _sdk.AsyncSurreal is not None
    assert issubclass(_sdk.AlreadyExistsError, _sdk.SurrealError)


def test_sdk_shim_rich_exports() -> None:
    """v0.23.0: every SDK symbol the rich field types use enters through ``_sdk.py``."""
    import surrealdb
    import surrealdb.cbor
    import surrealdb.data.cbor
    import surrealdb.data.types.range

    from surreal_orm_lite import _sdk

    assert _sdk.SdkDuration is surrealdb.Duration
    assert _sdk.SdkRange is surrealdb.Range
    assert _sdk.TAG_DURATION_COMPACT == 14
    assert _sdk.TAG_DECIMAL_STRING == 10
    assert _sdk.undefined is surrealdb.cbor.undefined
    assert _sdk.CBORTag is surrealdb.cbor.CBORTag
    assert _sdk.CBORSimpleValue is surrealdb.cbor.CBORSimpleValue
    assert _sdk.Bound is surrealdb.data.types.range.Bound
    assert _sdk.BoundIncluded is surrealdb.data.types.range.BoundIncluded
    assert _sdk.BoundExcluded is surrealdb.data.types.range.BoundExcluded
    assert _sdk.cbor_encode is surrealdb.data.cbor.encode
    assert _sdk.cbor_decode is surrealdb.data.cbor.decode
    assert _sdk.SdkInvalidDurationError is surrealdb.InvalidDurationError
    for name in [
        "SdkDuration",
        "SdkRange",
        "TAG_DURATION_COMPACT",
        "TAG_DECIMAL_STRING",
        "CBORTag",
        "CBORSimpleValue",
        "CBOR_NULL",
        "undefined",
        "Bound",
        "BoundIncluded",
        "BoundExcluded",
        "cbor_encode",
        "cbor_decode",
        "SdkInvalidDurationError",
    ]:
        assert name in _sdk.__all__, name


def test_cbor_null_constant() -> None:
    """AC-3 (spec §6.7): ``CBOR_NULL`` is the plain CBOR null, SurrealDB's open range bound."""
    import surrealdb.cbor

    from surreal_orm_lite import _sdk

    assert type(_sdk.CBOR_NULL) is surrealdb.cbor.CBORSimpleValue
    assert _sdk.CBOR_NULL.value == 22
    assert _sdk.CBORSimpleValue is surrealdb.cbor.CBORSimpleValue
    assert _sdk.cbor_encode is not None
    assert _sdk.cbor_decode is not None
    canary = "the SDK changed how it encodes a CBOR null or a Range: re-check spec §3.1/§3.9 (open range sides)"
    # f6, not c6 f6 (the tag-6 NONE that ``None`` gives).
    assert _sdk.cbor_encode(_sdk.CBOR_NULL) == b"\xf6", canary
    assert _sdk.cbor_decode(b"\xf6") is None, canary
    open_begin = _sdk.SdkRange(_sdk.CBOR_NULL, _sdk.BoundExcluded(5))
    assert _sdk.cbor_encode(open_begin) == bytes.fromhex("d83182f6d83305"), canary


def test_first_of_prefers_the_root_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3: a symbol is taken from the SDK's top level when present, else from its submodule."""
    from surreal_orm_lite import _sdk

    root, sub = types.ModuleType("fake_root"), types.ModuleType("fake_sub")
    monkeypatch.setitem(sys.modules, "fake_root", root)
    monkeypatch.setitem(sys.modules, "fake_sub", sub)
    in_root, in_sub = object(), object()

    sub.X = in_sub  # type: ignore[attr-defined]
    assert _sdk._first_of("X", "fake_root", "fake_sub") is in_sub

    root.X = in_root  # type: ignore[attr-defined]
    assert _sdk._first_of("X", "fake_root", "fake_sub") is in_root

    with pytest.raises(ImportError, match=r"fake_root.*fake_sub"):
        _sdk._first_of("Y", "fake_root", "fake_sub")
    assert _sdk._first_of("Y", "fake_root", "fake_sub", required=False) is None
    # A location that does not exist (a submodule a later SDK removed) is skipped, not fatal.
    assert _sdk._first_of("X", "fake_missing_module_for_orm_lite", "fake_root") is in_root


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh interpreter (``-I``), so the ORM's module objects are built anew.

    Never an in-process reload: importing ``surreal_orm_lite.exceptions`` a second time would create a
    second module object and second exception classes (``tests/test_import_identity.py``).
    """
    return subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_codec_comes_from_data_cbor_only() -> None:
    """Spec §6.7: ``encode``/``decode`` are never taken from the SDK's package root.

    Names that generic could be an unrelated symbol in a later SDK release: the codec is read from
    ``surrealdb.data.cbor`` alone, even when the root defines both names.
    """
    result = _run_isolated(
        """
        import surrealdb
        surrealdb.encode = surrealdb.decode = object()
        from surreal_orm_lite import _sdk
        import surrealdb.data.cbor as c
        assert _sdk.cbor_encode is c.encode, _sdk.cbor_encode
        assert _sdk.cbor_decode is c.decode, _sdk.cbor_decode
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


def test_invalid_duration_error_without_sdk_class() -> None:
    """AC-3: an SDK without ``InvalidDurationError`` still imports; the ORM's class is a ``ValueError``."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.errors
        del surrealdb.InvalidDurationError, surrealdb.errors.InvalidDurationError
        import surreal_orm_lite
        from surreal_orm_lite import Duration, InvalidDurationError
        assert InvalidDurationError.__bases__ == (ValueError,), InvalidDurationError.__bases__
        try:
            Duration.parse("1.5h")
        except InvalidDurationError as exc:
            assert isinstance(exc, ValueError)
        else:
            raise AssertionError("Duration.parse('1.5h') did not raise")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


def test_invalid_duration_error_with_value_error_sdk_class() -> None:
    """AC-3: an SDK whose class is already a ``ValueError`` — the SDK's base first keeps the MRO valid."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.errors
        Fake = type("InvalidDurationError", (surrealdb.errors.SurrealError, ValueError), {})
        surrealdb.InvalidDurationError = Fake
        surrealdb.errors.InvalidDurationError = Fake
        import surreal_orm_lite
        assert issubclass(surreal_orm_lite.InvalidDurationError, Fake)
        assert issubclass(surreal_orm_lite.InvalidDurationError, ValueError)
        try:
            surreal_orm_lite.Duration.parse("1.5h")
        except Fake:
            pass
        else:
            raise AssertionError("not caught by the SDK class")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


_SDK_GEOMETRY_CLASSES = [
    "GeometryPoint",
    "GeometryLine",
    "GeometryPolygon",
    "GeometryMultiPoint",
    "GeometryMultiLine",
    "GeometryMultiPolygon",
    "GeometryCollection",
]


def test_sdk_shim_geometry_exports() -> None:
    """v0.24.0 AC-1: the SDK's geometry classes, base class and error enter through ``_sdk.py``."""
    import surrealdb
    import surrealdb.data

    from surreal_orm_lite import _sdk

    for name in _SDK_GEOMETRY_CLASSES:
        assert getattr(_sdk, f"Sdk{name}") is getattr(surrealdb.data, name), name
        assert f"Sdk{name}" in _sdk.__all__, name
    assert _sdk.SdkGeometry is surrealdb.Geometry
    assert _sdk.SdkInvalidGeometryError is surrealdb.InvalidGeometryError
    assert "SdkGeometry" in _sdk.__all__
    assert "SdkInvalidGeometryError" in _sdk.__all__


def test_geometry_classes_prefer_the_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.24.0 AC-1: a geometry class the SDK's top level exports wins over its submodule."""
    import surrealdb.data.types.geometry

    from surreal_orm_lite import _sdk

    submodule = "surrealdb.data.types.geometry"
    root = types.ModuleType("fake_geometry_root")
    monkeypatch.setitem(sys.modules, "fake_geometry_root", root)
    for name in _SDK_GEOMETRY_CLASSES:
        # SDK 2.0.0 layout: the root lacks the class, the submodule has it.
        assert _sdk._first_of(name, "fake_geometry_root", submodule) is getattr(surrealdb.data.types.geometry, name)
        in_root = type(name, (), {})
        setattr(root, name, in_root)
        assert _sdk._first_of(name, "fake_geometry_root", submodule) is in_root, name


def test_geometry_classes_taken_from_the_root_when_exported() -> None:
    """v0.24.0 AC-1: when the SDK's top level exports the classes (SDK 3.0 layout), the shim uses them."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.data.types.geometry as g
        names = ["GeometryPoint", "GeometryLine", "GeometryPolygon", "GeometryMultiPoint",
                 "GeometryMultiLine", "GeometryMultiPolygon", "GeometryCollection"]
        marked = {}
        for name in names:
            marked[name] = type(name, (getattr(g, name),), {})
            setattr(surrealdb, name, marked[name])
        import surreal_orm_lite
        from surreal_orm_lite import _sdk
        for name in names:
            assert getattr(_sdk, "Sdk" + name) is marked[name], name
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


def test_invalid_geometry_error_without_sdk_class() -> None:
    """v0.24.0 AC-2: an SDK without ``InvalidGeometryError`` still imports; the ORM's class is a ``ValueError``."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.errors
        del surrealdb.InvalidGeometryError, surrealdb.errors.InvalidGeometryError
        import surreal_orm_lite
        from surreal_orm_lite import InvalidGeometryError, _sdk
        assert _sdk.SdkInvalidGeometryError is None, _sdk.SdkInvalidGeometryError
        assert InvalidGeometryError.__bases__ == (ValueError,), InvalidGeometryError.__bases__
        try:
            raise InvalidGeometryError("x")
        except ValueError as exc:
            assert str(exc) == "x"
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


def test_invalid_geometry_error_with_value_error_sdk_class() -> None:
    """v0.24.0 AC-2: an SDK whose class is already a ``ValueError`` — the SDK's base first keeps the MRO valid."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.errors
        Fake = type("InvalidGeometryError", (surrealdb.errors.SurrealError, ValueError), {})
        surrealdb.InvalidGeometryError = Fake
        surrealdb.errors.InvalidGeometryError = Fake
        import surreal_orm_lite
        from surreal_orm_lite import InvalidGeometryError
        assert issubclass(InvalidGeometryError, Fake)
        assert issubclass(InvalidGeometryError, ValueError)
        assert InvalidGeometryError.__mro__.index(Fake) < InvalidGeometryError.__mro__.index(ValueError)
        try:
            raise InvalidGeometryError("x")
        except Fake:
            pass
        else:
            raise AssertionError("not caught by the SDK class")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr


_SDK_CONNECTION_CLASSES = {
    "SdkAsyncWsConnection": "AsyncWsSurrealConnection",
    "SdkAsyncHttpConnection": "AsyncHttpSurrealConnection",
    "SdkAsyncEmbeddedConnection": "AsyncEmbeddedSurrealConnection",
}


def test_sdk_shim_read_hook_exports() -> None:
    """#235: what the read hook (``decoding.py``) takes from the SDK enters through ``_sdk.py``."""
    import surrealdb
    import surrealdb.data.cbor

    from surreal_orm_lite import _sdk

    assert _sdk.cbor_tag_decoder is surrealdb.data.cbor.tag_decoder
    assert "cbor_tag_decoder" in _sdk.__all__
    for alias, name in _SDK_CONNECTION_CLASSES.items():
        assert getattr(_sdk, alias) is getattr(surrealdb, name), alias
        assert alias in _sdk.__all__, alias


def test_read_hook_without_the_sdk_symbols() -> None:
    """#235: an SDK without a ``tag_decoder`` or a connection class still imports; the hook is then off."""
    result = _run_isolated(
        """
        import surrealdb, surrealdb.data.cbor
        del surrealdb.data.cbor.tag_decoder
        del surrealdb.AsyncHttpSurrealConnection
        import surrealdb.connections.async_http as http
        del http.AsyncHttpSurrealConnection
        from surreal_orm_lite import _sdk, decoding, encoding
        assert _sdk.cbor_tag_decoder is None and _sdk.SdkAsyncHttpConnection is None
        assert decoding.needed() and not decoding.available() and not decoding.active()
        assert not encoding.reads_zero_duration()
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", result.stderr
