import argparse
import json
import sys

from envweaver import runtime

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
instance = runtime.connect(parser.parse_args().instance)
code, stdout, stderr = runtime.exec(instance, "python3 -m pytest -q", cwd="/work/repo")
print(stdout, file=sys.stderr)
print(stderr, file=sys.stderr)
if code not in (0, 1):
    raise RuntimeError(f"pytest infrastructure failed with exit code {code}")
print(json.dumps({"score": 1.0 if code == 0 else 0.0}))
