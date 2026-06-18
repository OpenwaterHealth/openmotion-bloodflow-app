import pytest
from motion_connector import _update_decision


@pytest.mark.unit
def test_valid_signature_launches():
    assert _update_decision("Valid", require_signed=True) == (True, None)


@pytest.mark.unit
def test_invalid_signature_aborts_with_message():
    launch, err = _update_decision("HashMismatch", require_signed=False)
    assert launch is False
    assert "HashMismatch" in err


@pytest.mark.unit
def test_unsigned_allowed_when_not_required():
    assert _update_decision("NotSigned", require_signed=False) == (True, None)


@pytest.mark.unit
def test_unsigned_rejected_when_required():
    launch, err = _update_decision("NotSigned", require_signed=True)
    assert launch is False
    assert "not signed" in err.lower()
