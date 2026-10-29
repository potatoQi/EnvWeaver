"""Task producers and Agent execution reject the same malformed goal inputs."""

from unittest.mock import Mock

import pytest

from envweaver import Agent, Instance, Runtime, RuntimeRef, TaskPool, Verifier
from envweaver.agents.execution import AgentExecution


class ProbeAgent(Agent):
    def preflight(self, goal):
        pytest.fail("Malformed goals must not reach the adapter")

    def run(self, **kwargs):
        pytest.fail("Malformed goals must not run")


@pytest.mark.parametrize("goal", [
    None, [], {}, "task", [None], [1], ["text"],
    [{}], [{"type": "audio", "path": "input.wav"}],
    [{"type": "text"}], [{"type": "text", "text": 1}],
    [{"type": "image"}], [{"type": "image", "path": None}],
])
def test_invalid_goal_rejected_before_saving_or_calling_adapter(tmp_path, goal):
    runtime = Mock(spec=Runtime)
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    verifier = Verifier(entry=tmp_path / "run.py", runtime=runtime, timeout=30)
    with pytest.raises(ValueError) as saved:
        pool.save(
            instance=Instance("unused", "test"), name="invalid", goal=goal,
            verifier=verifier, verifier_runtime_ref=RuntimeRef("test", "verifier"), cwd="/",
        )
    with pytest.raises(ValueError) as executed:
        AgentExecution(ProbeAgent()).preflight(goal=goal)

    assert str(saved.value) == str(executed.value)
    runtime.save.assert_not_called()
    assert not pool.root.exists()
