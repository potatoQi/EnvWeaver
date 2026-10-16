from pathlib import Path

import pytest

from envweaver import Instance, verifier
from envweaver.errors import VerifierError


@pytest.mark.parametrize(
    "output", ['{"score": 0}', '{"score": 1}', '{"score": 0.5, "details": "partial"}']
)
def test_valid_scores(tmp_path, output):
    entry = tmp_path / "run.py"
    entry.write_text(f"print({output!r})")
    assert 0 <= verifier.run(entry, Instance("a", "test", str(tmp_path)))["score"] <= 1


@pytest.mark.parametrize(
    "output",
    [
        '{"score": true}',
        '{"score": -1}',
        '{"score": 1.1}',
        '{"score": NaN}',
        '{"score": Infinity}',
        '{"score": "1"}',
        "{}",
        "[]",
        'logs\n{"score": 1}',
        '{"score": 1, "other": NaN}',
    ],
)
def test_invalid_results_are_errors(tmp_path, output):
    entry = tmp_path / "run.py"
    entry.write_text(f"print({output!r})")
    with pytest.raises(VerifierError):
        verifier.run(entry, Instance("a", "test", str(tmp_path)))


def test_execution_error_timeout_and_descriptor_cleanup(tmp_path):
    entry = tmp_path / "run.py"
    entry.write_text(
        "import sys\nfrom pathlib import Path\nPath('seen').write_text(sys.argv[2])\nraise RuntimeError('broken checker')"
    )
    with pytest.raises(VerifierError, match="broken checker"):
        verifier.run(entry, Instance("a", "test", str(tmp_path)))
    assert not Path((tmp_path / "seen").read_text()).exists()
    entry.write_text("import time\ntime.sleep(30)")
    with pytest.raises(VerifierError, match="timed out"):
        verifier.run(entry, Instance("a", "test", str(tmp_path)), timeout=0.1)


def test_verifier_reconnects_to_supplied_instance(tmp_path, guest):
    entry = tmp_path / "run.py"
    entry.write_text("""import argparse, json
from envweaver import runtime
p = argparse.ArgumentParser()
p.add_argument('--instance')
i = runtime.connect(p.parse_args().instance)
code, out, err = runtime.exec(i, 'echo verified')
print(json.dumps({'score': float(code == 0 and out.strip() == 'verified')}))
""")
    assert verifier.run(entry, guest) == {"score": 1.0}
