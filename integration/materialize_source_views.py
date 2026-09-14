#!/usr/bin/env python3
"""Create disposable hard-link views expected by the suhyeon extractors.

No source document is copied or modified.  The generated data/products and
data/institution trees are ignored by Git and may be recreated at any time.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SOURCE = ROOT / "data" / "투자설명서"
INSTITUTION_SOURCE = ROOT / "data" / "docs"
PRODUCT_VIEW = ROOT / "data" / "products"
INSTITUTION_VIEW = ROOT / "data" / "institution"


def reset_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def hardlink(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError as exc:
        raise RuntimeError(
            f"Hard-link creation failed for {source}. Refusing to copy raw data: {exc}"
        ) from exc


def main():
    reset_dir(PRODUCT_VIEW)
    reset_dir(INSTITUTION_VIEW)
    product_count = 0
    for source in PRODUCT_SOURCE.rglob("*.pdf"):
        match = re.fullmatch(r"R2_(.+)\.pdf", source.name, re.I)
        if not match:
            continue
        code = match.group(1)
        hardlink(source, PRODUCT_VIEW / code / source.name)
        product_count += 1
    institution_count = 0
    for source in INSTITUTION_SOURCE.iterdir():
        if not source.is_file():
            continue
        hardlink(source, INSTITUTION_VIEW / source.name)
        institution_count += 1
    print({"products": product_count, "institution_documents": institution_count})


if __name__ == "__main__":
    main()
