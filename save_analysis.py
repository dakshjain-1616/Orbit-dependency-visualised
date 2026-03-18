"""
Run Orbis analysis on psf/requests and save to architecture_analysis.json
Usage: python save_analysis.py [repo_url]
"""
import json
import tempfile
import os
import sys
from pathlib import Path

import git
from neo_parser import analyze_codebase

REPO_URL = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/psf/requests"
OUTPUT_PATH = Path(__file__).parent / "architecture_analysis.json"

print(f"Cloning {REPO_URL} ...")
tmp_dir = tempfile.mkdtemp(prefix="neo_save_")
try:
    git.Repo.clone_from(REPO_URL, tmp_dir, depth=1, single_branch=True)
    print(f"Cloned to {tmp_dir}")

    print("Running NEO analysis ...")
    result = analyze_codebase(tmp_dir)

    print(f"Nodes: {len(result['nodes'])}")
    print(f"Edges: {len(result['edges'])}")
    print(f"Insights: {len(result['insights'])}")
    print(f"Architecture: {result['architecture_type']}")
    print(f"DAG verified: {result['dependency_graph_stats']['dag_verified']}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved to {OUTPUT_PATH}")
    size = os.path.getsize(OUTPUT_PATH)
    print(f"File size: {size:,} bytes")

finally:
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("Temp dir cleaned up.")
