import pytest
from fastapi import HTTPException

from app.auth import get_current_user_id


def test_missing_bearer_prefix_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(authorization="not-a-bearer-token")
    assert exc_info.value.status_code == 401


def test_missing_header_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(authorization=None)
    assert exc_info.value.status_code == 401


def test_garbage_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(authorization="Bearer this.is.garbage")
    assert exc_info.value.status_code == 401


def test_empty_bearer_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(authorization="Bearer ")
    assert exc_info.value.status_code == 401
