"""A small user-owned pipeline, not a production algorithm built into EnvWeaver."""

import argparse
import os
import uuid
from pathlib import Path

from envweaver import Runtime, RuntimeRef, TaskPool, agent, verifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="outputs/coding")
    parser.add_argument("--image", default="python:3.11-slim-bookworm")
    parser.add_argument(
        "--agent", action="store_true", help="Use pi instead of the known reference fix"
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    runtime = Runtime(output / "runtimes")
    task = TaskPool(output / "tasks", runtime=runtime)
    source = Path(__file__).parent
    initial = runtime.start(RuntimeRef("docker", args.image, {"command": ["sleep", "infinity"]}))
    instances = [initial]
    try:
        code, _, error = runtime.exec(initial, "python3 -m pip install pytest")
        if code:
            raise RuntimeError(error)
        runtime.upload(initial, source / "repo", "/work/repo")
        goal = [
            {
                "type": "text",
                "text": "Fix add() in /work/repo/calculator.py without changing tests.",
            }
        ]
        assert verifier.run(source / "verifier/run.py", initial)["score"] == 0
        task_ref = task.save(
            initial,
            f"calculator-{uuid.uuid4().hex[:8]}",
            goal,
            source / "verifier/run.py",
            metadata={"domain": "coding"},
        )
        saved = task.load(task_ref)
        candidate = runtime.start(saved.runtime, saved.context)
        instances.append(candidate)
        if args.agent:
            agent.run(
                "pi",
                goal[0]["text"],
                output / "agent",
                instance=candidate,
                config={
                    "base_url": os.environ["MODEL_BASE_URL"],
                    "model_name": os.environ["MODEL_NAME"],
                    "api_key": os.environ.get("MODEL_API_KEY", "local"),
                },
            )
        else:
            runtime.exec(
                candidate, "sed -i 's/left - right/left + right/' /work/repo/calculator.py"
            )
        print("Task:", task_ref)
        print("Candidate result:", verifier.run(saved.verifier, candidate))
    finally:
        for instance in instances:
            success, error = runtime.close(instance)
            if not success:
                print("Cleanup failed:", error)


if __name__ == "__main__":
    main()
