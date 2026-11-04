"""Version gating does not import Harbor on the host."""

import pytest

from envweaver.agents.adapters.terminus import monkey_patches
from envweaver.agents.adapters.terminus.monkey_patches.v0_25_0 import context_recovery


@pytest.mark.parametrize("version", ["0.24.0", "0.25.1", "0.25.1.dev202611030558"])
def test_unverified_version_cannot_silently_skip_or_reuse_patch(monkeypatch, version):
    monkeypatch.setattr(monkey_patches, "version", lambda name: version)
    monkeypatch.setattr(context_recovery, "install", lambda: pytest.fail("wrong version"))
    with pytest.raises(ValueError, match=f"No verified Terminus monkey patches for Harbor {version}"):
        monkey_patches.install()


def test_pinned_version_dispatches_to_its_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(monkey_patches, "version", lambda name: "0.25.0")
    monkeypatch.setattr(context_recovery, "install", lambda: calls.append("0.25.0"))
    monkey_patches.install()
    assert calls == ["0.25.0"]
