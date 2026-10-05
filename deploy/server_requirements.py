"""Print the server image's Python requirements: pyproject's dependencies without the local OCR stack.

The server reads pages with Qari on Runpod (OCR_BACKEND=runpod), so torch, torchvision, transformers,
accelerate, safetensors and huggingface_hub are not installed there (their CUDA wheels are several GB). Every
one of them is imported lazily, inside the local engines (`ocr/engines/qari_torch.py`, `qari.py`), and
nothing imports them at startup. The Dockerfile runs this script, so the list follows pyproject.toml.

    python deploy/server_requirements.py > /tmp/requirements.txt
"""

import re
import sys
import tomllib
from pathlib import Path

# the packages only the local PyTorch / MLX engines (OCR_BACKEND=torch|mlx) need
LOCAL_OCR_ONLY = {"torch", "torchvision", "transformers", "accelerate", "safetensors", "huggingface-hub"}


def canonical(requirement: str) -> str:
    name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    return re.sub(r"[-_.]+", "-", name.group(0)).lower() if name else ""


def main() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    for requirement in dependencies:
        if canonical(requirement) not in LOCAL_OCR_ONLY:
            sys.stdout.write(requirement + "\n")


if __name__ == "__main__":
    main()
