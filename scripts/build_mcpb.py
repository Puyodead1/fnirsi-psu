"""Build the Claude Desktop extension bundle (dist/fnirsi-psu-<version>.mcpb).

Steps: stage extension/ into build/extension, copy the shared driver
(fnirsi_psu.py) next to the server, vendor pure-Python pyserial into
server/lib, then validate and pack with the official mcpb CLI
(`npx @anthropic-ai/mcpb`, needs Node.js).

Usage:
    python scripts/build_mcpb.py            # build + pack
    python scripts/build_mcpb.py --no-pack  # stage only (no Node needed)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "extension")
STAGE = os.path.join(ROOT, "build", "extension")
DIST = os.path.join(ROOT, "dist")
PYSERIAL = "pyserial==3.5"


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def npx():
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        sys.exit("npx not found: install Node.js (or use --no-pack)")
    return exe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pack", action="store_true", help="only stage build/extension")
    args = ap.parse_args()

    shutil.rmtree(STAGE, ignore_errors=True)
    shutil.copytree(SRC, STAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "lib"))
    shutil.copy2(os.path.join(ROOT, "fnirsi_psu.py"), os.path.join(STAGE, "server"))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), STAGE)

    lib = os.path.join(STAGE, "server", "lib")
    run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--no-deps",
         "--disable-pip-version-check", "--target", lib, PYSERIAL])
    shutil.rmtree(os.path.join(lib, "bin"), ignore_errors=True)

    with open(os.path.join(STAGE, "manifest.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    print(f"staged {STAGE} (v{version})")
    if args.no_pack:
        return

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"fnirsi-psu-{version}.mcpb")
    run([npx(), "--yes", "@anthropic-ai/mcpb", "validate", os.path.join(STAGE, "manifest.json")])
    run([npx(), "--yes", "@anthropic-ai/mcpb", "pack", STAGE, out])
    print(f"built {out}")


if __name__ == "__main__":
    main()
