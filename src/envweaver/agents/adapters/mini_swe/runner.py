"""Run mini with model-boundary capture enabled."""

import runpy
import sys
from pathlib import Path

from model_capture import install

install(Path(__file__).with_name("model-calls.jsonl"))
sys.argv = ["mini", *sys.argv[1:]]
runpy.run_module("minisweagent.run.mini", run_name="__main__")
