import pytest

from envweaver.errors import VerifierError
from envweaver.verifier import _result


@pytest.mark.parametrize(
    "output", ['{"score": 0}', '{"score": 1}', '{"score": 0.5, "details": "partial"}']
)
def test_valid_scores(output):
    assert 0 <= _result(0, output, "")["score"] <= 1


@pytest.mark.parametrize(
    "output",
    [
        '{"score": true}',
        '{"score": -1}',
        '{"score": 1.1}',
        '{"score": NaN}',
        '{"score": Infinity}',
        '{"score": -Infinity}',
        '{"score": 1e400}',
        '{"score": ' + str(10 ** 400) + '}',
        '{"score": ' + str(-(10 ** 400)) + '}',
        '{"score": "1"}',
        "{}",
        "[]",
        'logs\n{"score": 1}',
        '{"score": 1, "other": NaN}',
    ],
)
def test_invalid_results_are_errors(output):
    with pytest.raises(VerifierError):
        _result(0, output, "")


def test_execution_error_is_not_a_score():
    with pytest.raises(VerifierError, match="broken checker"):
        _result(1, "", "broken checker")
