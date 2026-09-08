#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lxml"]
# ///
"""Regression harness: does `scan --auto` still reproduce the known-good fixes?

The heuristics in epubfix.py were derived from three books that were repaired
by hand, and the repaired files are the only ground truth there is. Every
change to scoring is therefore checked here: run the automatic pipeline over
each original and require the resulting navigation to match, entry for entry,
the one a human produced.

The books are not in the repository - they are purchased files. Point the
harness at a directory holding them:

    uv run tests/regression.py --corpus ../epubs

Each case names an original, the hand-fixed result to match, and the spine and
entry counts, so a failure says what changed rather than merely that something
did.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
SCRIPT = Path(__file__).resolve().parent.parent / "epubfix.py"

CASES = [
    # original, hand-fixed reference, spine docs, toc entries, what it exercises
    ("Dune Messiah - Frank Herbert.epub", "Dune Messiah - fixed.epub", 31, 31,
     "one 658KB file split on attributed epigraphs; titles synthesised"),
    ("Children of Dune.epub", "Children of Dune - fixed.epub", 71, 71,
     "already one file per chapter; navigation and titles only"),
    ("Gallipoli Soup - Tim Knight.epub", "Gallipoli Soup - fixed.epub", 116, 114,
     "InDesign export, 2 files -> 116; parts nested under level 1"),
]

# A discarded intermediate attempt kept as a negative case: `verify` must
# reject it, or the safety net is not catching anything.
KNOWN_BAD = ("Children of Dune2.epub", "Children of Dune.epub")


def toc_of(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as z:
        ncx = next((n for n in z.namelist() if n.lower().endswith(".ncx")), None)
        if ncx is None:
            return []
        root = ET.fromstring(z.read(ncx))
    out = []
    for np in root.iter(NCX + "navPoint"):
        label = np.find(NCX + "navLabel/" + NCX + "text")
        content = np.find(NCX + "content")
        out.append((
            re.sub(r"\s+", " ", "".join(label.itertext())).strip() if label is not None else "",
            content.get("src", "") if content is not None else "",
        ))
    return out


def spine_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        opf = next(n for n in z.namelist() if n.lower().endswith(".opf"))
        root = ET.fromstring(z.read(opf))
    return len(list(root.iter("{http://www.idpf.org/2007/opf}itemref")))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path,
                    help="directory holding the originals and their hand-fixed results")
    args = ap.parse_args()

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for original, reference, want_spine, want_toc, what in CASES:
            src, ref = args.corpus / original, args.corpus / reference
            if not src.exists() or not ref.exists():
                print(f"SKIP  {original}\n      not found in {args.corpus}")
                continue

            plan = tmp / (src.stem + ".json")
            out = tmp / (src.stem + "-auto.epub")
            r = run("scan", "--auto", str(src), "-o", str(plan))
            if r.returncode:
                print(f"FAIL  {original}\n      scan failed: {r.stderr.strip()[:200]}")
                failures += 1
                continue
            r = run("apply", str(src), str(plan), "-o", str(out))
            if r.returncode:
                print(f"FAIL  {original}\n      apply failed: {r.stderr.strip()[:200]}")
                failures += 1
                continue

            problems = []
            got_toc, ref_toc = toc_of(out), toc_of(ref)
            if got_toc != ref_toc:
                problems.append(f"navigation differs from the hand-made fix "
                                f"({len(got_toc)} entries vs {len(ref_toc)})")
                for i in range(max(len(got_toc), len(ref_toc))):
                    a = got_toc[i] if i < len(got_toc) else None
                    b = ref_toc[i] if i < len(ref_toc) else None
                    if a != b:
                        problems.append(f"  first difference at {i}: {a} vs {b}")
                        break
            got_spine = spine_count(out)
            if got_spine != want_spine:
                problems.append(f"spine {got_spine}, expected {want_spine}")
            if len(got_toc) != want_toc:
                problems.append(f"toc {len(got_toc)}, expected {want_toc}")

            v = run("verify", str(out), str(src))
            if v.returncode:
                problems.append("verify failed: " + v.stdout.strip().splitlines()[-1])

            if problems:
                failures += 1
                print(f"FAIL  {original}  ({what})")
                for p in problems:
                    print(f"      {p}")
            else:
                print(f"ok    {original}  ({what})")
                print(f"      spine {got_spine}, {len(got_toc)} entries, "
                      f"identical to {reference}, verify clean")

        bad, bad_src = args.corpus / KNOWN_BAD[0], args.corpus / KNOWN_BAD[1]
        if bad.exists() and bad_src.exists():
            v = run("verify", str(bad), str(bad_src))
            if v.returncode:
                print(f"ok    {KNOWN_BAD[0]}  (known-bad; verify rejects it as it should)")
            else:
                failures += 1
                print(f"FAIL  {KNOWN_BAD[0]}  verify passed a book known to be broken")

    print()
    print("all good" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
