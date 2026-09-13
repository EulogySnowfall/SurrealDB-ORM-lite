def test_sdk_shim_exports() -> None:
    from surreal_orm_lite import _sdk

    assert _sdk.RecordID is not None
    assert _sdk.AsyncSurreal is not None
    assert issubclass(_sdk.AlreadyExistsError, _sdk.SurrealError)
