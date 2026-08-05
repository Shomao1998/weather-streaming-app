"""Inspect how a knowledge document will be split, without building an index.

    python scripts/chunk_documents.py knowledge/raw/nws-heat-during.md

The splitting logic itself lives in `weather.advice.chunking` so it ships with
the package and is covered by the test suite; this is the CLI over it, for
eyeballing a new source before it is ingested.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "functions"))

from weather.advice.chunking import chunk_document  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--show-text", action="store_true")
    args = parser.parse_args()

    for raw in args.paths:
        path = Path(raw)
        chunks = chunk_document(path.read_text(encoding="utf-8"))
        print(f"\n{path.name}: {len(chunks)} chunks")
        for index, section in enumerate(chunks):
            print(f"  [{index:02}] {len(section.text):4} chars  heading={section.heading!r}")
            if args.show_text:
                print("       " + section.text.replace("\n", "\n       "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
