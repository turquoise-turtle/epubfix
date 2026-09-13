# epubfix

<a href="https://madebyhuman.iamjarl.com"><img src="https://madebyhuman.iamjarl.com/badges/loop-white.svg" alt="Human in the Loop" width="120" height="40"></a>

Repair the chapter structure and navigation of an EPUB without altering a word of the text.

Built for a specific annoyance: you buy an ebook, load it onto a Kobo, and the page numbers are nonsense while the table of contents lists four entries for a book with a hundred chapters. `epubfix` fixes both, with you reviewing every structural decision before it is applied.

---

## Contents

- [What it fixes](#what-it-fixes)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Commands](#commands)
  - [`scan`](#scan)
  - [`apply`](#apply)
  - [`classes`](#classes)
  - [`outline`](#outline)
  - [`cover`](#cover)
  - [`verify`](#verify)
- [The plan file](#the-plan-file)
- [Producer recipes](#producer-recipes)
- [Recipes](#recipes)
- [Verifying the result](#verifying-the-result)
- [Getting it onto a Kobo](#getting-it-onto-a-kobo)
- [Guarantees and limitations](#guarantees-and-limitations)

---

## What it fixes

**Broken page numbering.** Many retail EPUBs ship a print page map — an EPUB 3 `page-list` nav, an NCX `<pageList>`, or an Adobe `page-map.xml`. Kobo honours that map instead of computing its own pagination. When the map is misaligned with the spine you get front matter reporting page 200-something and chapter 1 reporting page 9. Removing the map makes Kobo fall back to its own consistent count.

**Coarse chapter structure.** Publishers often ship an entire novel as one or two enormous XHTML files. Kobo's "X minutes left in this chapter" and its progress-bar segmentation track *spine documents*, not table-of-contents anchors — so adding anchor-based entries gives you navigation but leaves progress tracking useless. `epubfix` splits the files into one document per chapter and rebuilds `nav.xhtml` and `toc.ncx` to match.

**Collapsed tables of contents.** Some books already have one file per chapter but list them all under a single entry. `epubfix` detects this and only rewrites the navigation, leaving the content files untouched byte-for-byte.

**Covers.** A separate, deliberately opt-in command replaces the cover image and keeps the thumbnail pointer and the in-book cover page in agreement.

---

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/) — recommended, and handles the one dependency (`lxml`) for you via inline script metadata

```bash
uv run epubfix.py --help
```

Without `uv`, install `lxml` yourself and run with `python`.

Optional but strongly recommended: **Calibre**, for its `ebook-edit` book editor and its Check Book validator.

---

## Quick start

```bash
# 1. Work out how this book marks its chapters, and propose boundaries
uv run epubfix.py scan "book.epub" --auto -o plan.json

# 2. Open plan.json and review. Everything carries a confidence score and a
#    reason; the console listing puts the doubtful cases first.

# 3. Apply, then check the result against the original
uv run epubfix.py apply "book.epub" plan.json -o "book-fixed.epub"
uv run epubfix.py verify "book-fixed.epub" "book.epub"
```

`--auto` looks at which paragraph style tends to be followed by the first
paragraph of a chapter, and proposes the two or three markers that behave that
way. It prints each proposal as the explicit command line that would reproduce
it, so you can take one, adjust a regex and re-run by hand.

Without `--auto`, `scan` behaves as it always has: `h1`-`h4`, or whatever you
pass to `--select`, `--class-regex` and `--id-regex`.

---

## Commands

### `scan`

Inspects the book and writes a plan file of proposed chapter boundaries.

```bash
uv run epubfix.py scan BOOK.epub -o plan.json [options]
```

| Option | Purpose |
| --- | --- |
| `-o, --out` | Plan file to write. Default `plan.json`. |
| `--select` | Pipe-separated tag names treated as headings. Default `h1\|h2\|h3\|h4`. |
| `--no-tags` | Ignore `--select` entirely; match only on class or id. |
| `--class-regex` | Treat elements whose `@class` matches as headings. |
| `--id-regex` | Treat elements carrying (or containing) a matching `@id` as headings. |
| `--require-following` | Mark a candidate `"split": false` unless the very next block's class matches. |
| `--include-preceding` | Pull matching blocks immediately *before* a split point into its file. |
| `--include-preceding-into` | Restrict `--include-preceding` to split points whose own class matches. |
| `--title-extend` | Fold following sibling blocks' text into the ToC title. |
| `--title-sep` | Separator used by `--title-extend`. Default `" - "`. |
| `--auto` | Discover the chapter marker and write a plan using it. |
| `--propose` | Print ranked marker proposals without committing to one. |
| `--from-toc` | Build the plan from the book's own navigation anchors instead of detecting headings. |
| `--threshold` | Confidence at or above which a candidate is `"split": true`. Default `0.5`. |
| `--verbose` / `--show-above` | Show every candidate, rather than only those below `0.95` confidence. |
| `--recipes` / `--save-recipe` | Reuse, or record, a per-producer recipe. See [Producer recipes](#producer-recipes). |

The header output tells you the spine document count, whether a page map is present, and the current table of contents. Then each spine document is listed with its candidates.

Lines prefixed `-` are candidates that failed `--require-following`; they are written to the plan as `"split": false` rather than dropped, so you can review and re-enable them. A `<-N` marker means `--include-preceding` moved that split point back N positions.

Each candidate carries a confidence score and the reasoning behind it:

```
  - 0.15 pos=1506   L1 div class=blockquote  '“A wind has blown the land away…'
        [prose follows, but not a chapter-opening style; no attribution — reads as an in-scene quotation]
    1.00 pos=1959   L1 div class=blockquote  'Empires do not suffer emptiness of purpose…'
        [opens with p.noindent; run of body text follows; carries an attribution]
```

What the score is built from, in rough order of how much it decides:

- **What follows the marker.** A chapter opener is followed by the chapter's
  first paragraph. Where a book has a distinct style for that paragraph, prose
  in any *other* style is positive evidence that the marker sits mid-scene.
- **Attribution.** A chapter epigraph is quoted from somewhere and says so
  (`—Ancient Fremen Saying`); a song a character overhears is not attributed.
  This separates the two where the paragraph-style test cannot, because both
  are followed by ordinary prose. Only used when the marker style is
  sometimes-but-not-always attributed, since a constant tells you nothing.
- **`page-break-before` in the book's own stylesheet.** A publisher forcing a
  page break is stating that a section begins. It applies to every occurrence
  of the style equally, so it acts as a floor rather than a bonus — it is what
  keeps front matter whose body is a list or a poem rather than prose
  ("Cast", "Contents") in the table of contents.
- **Text shape.** `Chapter 7`, a bare numeral or a roman numeral.
- **Ornaments and empty markers** are rejected outright.

Deliberately *not* used: how evenly spaced the candidates are. It is the
obvious idea and it does not work — measured against a book fixed by hand,
real chapters ranged from 4.5K to 26K characters, and a "too close together"
rule flagged two real chapters while missing two of the three interior songs.

> **PowerShell note.** PowerShell discards empty-string arguments before the script sees them, so `--select ""` fails. Use `--no-tags` instead (or `--select none`).

### `apply`

Splits, relinks, and rebuilds navigation according to the plan.

```bash
uv run epubfix.py apply BOOK.epub plan.json -o OUTPUT.epub [options]
```

| Option | Purpose |
| --- | --- |
| `-o, --out` | Output EPUB. Default `fixed.epub`. |
| `--title-format` | Rename generated entries, e.g. `"Chapter {n}"`. `{n}` numbers them in reading order; `{text}` is the detected title. |
| `--keep-page-list` | Keep the print page map instead of removing it. |
| `--rebuild-toc-only` | Discard existing ToC entries instead of carrying over those whose target file was not split. |

By default, entries for files that were **not** split (Cover, Copyright, Epilogue and so on) are carried over and merged into the new table of contents in spine order, so front and back matter keep their proper names.

`apply` warns about any link pointing at an id that exists nowhere in the book. Those are almost always defects that were already present in the publisher's file — but they are worth knowing about, because after splitting they resolve to the first segment of the target rather than failing quietly in place.

### `classes`

Tallies every `(tag, class)` pair in use, with sample text, sorted by frequency.

```bash
uv run epubfix.py classes BOOK.epub [--all] [--max-count N] [--id-regex RE] [--show-ids N]
```

This is the diagnostic to reach for when `scan` finds nothing. Body text will have thousands of uses; your chapter style is the one whose count roughly matches the number of chapters and whose samples read like chapter openings. `--id-regex` additionally lists every matching id and the paragraph it sits in — useful for InDesign exports, where `_idParaDest-N` anchors sometimes mark chapter starts.

### `outline`

Shows each matching element together with its neighbouring blocks, in reading order.

```bash
uv run epubfix.py outline BOOK.epub --class-regex RE [--context N] [--doc SUBSTR] [--limit N]
```

`classes` tells you which styles exist; it cannot tell you whether a particular occurrence opens a chapter. That is only visible from context. A chapter epigraph is followed by the chapter's first paragraph; a song a character overhears is followed by more of the same scene. `outline` prints exactly the surrounding blocks you need to tell them apart.

### `cover`

Reports on, or replaces, the cover image. **Reports by default** — it writes nothing unless you pass `-o`.

```bash
# Report only
uv run epubfix.py cover BOOK.epub --image new-cover.jpg

# Apply
uv run epubfix.py cover BOOK.epub --image new-cover.jpg -o OUTPUT.epub
```

The report tells you where the current cover lives, *how* it was identified (EPUB 3 `properties="cover-image"`, EPUB 2 `<meta name="cover">`, or by following the first spine page), the old and new dimensions, and which page displays it. Check that before writing.

Applying it:

- replaces the image bytes
- sets both the EPUB 3 and EPUB 2 cover pointers, so readers agree on the thumbnail
- corrects the media type, renaming the file if the format changed
- updates the cover page, including resizing an SVG wrapper's `viewBox`

Responsive values such as `width="100%"` are left alone; only absolute pixel dimensions are rewritten.

> **This only fixes the cover inside the EPUB.** Calibre keeps its own library cover, and the Kobo driver builds the device thumbnail from *that*. After running this, also set the cover in Calibre via **Edit metadata**, or the home-screen tile will still show the old art.

### `verify`

Checks a fixed book against the guarantees this tool claims, and against the
book it came from.

```bash
uv run epubfix.py verify BOOK-FIXED.epub [BOOK.epub]
```

```
prose unchanged  424,253 characters, identical
links resolve    every internal href found its target
toc              31 entries, all resolve
spine            31 documents, each present exactly once
page map         gone
archive          mimetype first and stored

OK - all checks passed.
```

Give it the original as a second argument and it also confirms that not one
word moved: all body text in spine order, whitespace-normalised, must be
identical. Exit status is non-zero on failure, so it drops into a script.

This does not replace Calibre's Check Book — that knows about EPUB conformance
in general, this knows what *this tool* was supposed to do and did not.

---

## Producer recipes

Books from one imprint come out of one toolchain, so the marker that worked for
the last one usually works for the next.

```bash
# after reviewing and correcting a plan
uv run epubfix.py scan "book.epub" --auto -o plan.json --save-recipe recipes.json

# next book from the same publisher
uv run epubfix.py scan "other.epub" --auto -o plan.json --recipes recipes.json
```

```
Fingerprint  : unknown/6d426219503d  (62 styles)
               matches Dune Messiah.epub (93% style overlap), saved 2026-09-07:
               split on div.blockquote
```

The recipe is distilled from the *plan*, not from the proposal, so what gets
stored is whatever you decided — including decisions no heuristic made.

Matching is by similarity rather than by hash. Two titles from one imprint are
built from the same template but rarely ship identical stylesheets: the two
Gollancz books this was tested on declare 67 and 62 classes. Their class
vocabularies overlap 93% while sharing 1% with an InDesign export, so set
overlap identifies the family reliably where equality would never match at all.

---

## The plan file

Plain JSON, meant to be read and edited. One entry per candidate:

```json
{
  "pos": 8659,
  "heading_pos": 8661,
  "tag": "p",
  "class": "Part-Number",
  "id": "",
  "depth": 1,
  "text": "Part II",
  "split": true,
  "level": 1,
  "title": "Part II - Into the Pot"
}
```

| Field | Meaning |
| --- | --- |
| `pos` | Where the file will be cut, in document order. |
| `heading_pos` | Where the heading itself is. Differs from `pos` when `--include-preceding` moved the cut earlier. |
| `class`, `tag`, `id`, `depth` | Identifying context, so you can bulk-edit by find-and-replace. |
| `score`, `why` | Confidence in [0,1] and the reasoning behind it. Informational — `apply` reads `split`. |
| `split` | **Edit this.** `false` leaves the text in place and creates no entry. |
| `level` | **Edit this.** `1` for top-level, `2` to nest under the preceding level-1 entry. |
| `title` | **Edit this.** The table-of-contents label. |

Because `class` is included in every record, disabling a whole category of false positives is usually a single find-and-replace in your editor rather than 50 individual edits.

---

## Recipes

**Try this first, whatever the book**

```bash
uv run epubfix.py scan "book.epub" --auto -o plan.json
```

On the three books this was developed against — a Gollancz SF Gateway title
split on epigraphs, one already split but misnamed, and an InDesign export
needing 114 splits and two ToC levels — `--auto` followed by `apply` reproduces
the hand-made fix exactly, with no flags. That will not hold for every book,
which is why the plan file still exists.

**Real headings, straightforward book**

```bash
uv run epubfix.py scan "book.epub" -o plan.json
```

**InDesign export** — styled paragraphs, no headings, `_idParaDest` anchors

```bash
uv run epubfix.py classes "book.epub" --id-regex "_idParaDest" --show-ids 200

uv run epubfix.py scan "book.epub" -o plan.json \
  --no-tags \
  --class-regex "^Part-Number$|^_-Chapter-Number$" \
  --title-extend "^Part-Name$|^_-Chapter-Name$|^_-Chapter-Date$" \
  --include-preceding "Intro-quote" \
  --include-preceding-into "^Part-Number$"
```

Here the chapter title is split across three sibling paragraphs (number, POV character, date), so `--title-extend` folds them into one label. Part epigraphs are printed above their part heading, so `--include-preceding` pulls them into the part's file — scoped with `--include-preceding-into` so a closing epigraph elsewhere in the book stays where it is.

**Gollancz / SF Gateway** — chapters marked only by an epigraph `div`

```bash
uv run epubfix.py outline "book.epub" --class-regex "^blockquote$" --context 2

uv run epubfix.py scan "book.epub" -o plan.json \
  --no-tags --class-regex "^blockquote$" --require-following "^noindent$"

uv run epubfix.py apply "book.epub" plan.json -o "fixed.epub" --title-format "Chapter {n}"
```

The anchors in `^blockquote$` matter: without them the regex would also match `blockquote1`, the verse styling used *inside* epigraphs. `--require-following "^noindent$"` encodes the observation that a real chapter opener is followed by the chapter's first paragraph, while an interior song is not.

**Chapters already in separate files, but one collapsed ToC entry**

Same as above. `apply` detects that no file actually needs dividing, passes the content through untouched, and only rewrites the navigation.

---

## Verifying the result

Open the output in Calibre's editor and run **Tools → Check Book**:

```bash
ebook-edit "book-fixed.epub"
```

Run Check Book on the **original** first, so you know which errors were already there. Font MIME-type warnings and unreferenced leftovers like `calibre_bookmarks.txt` are cosmetic. What matters is broken links, missing resources, and anything mentioning the spine or nav document.

Then, in the viewer:

- follow a footnote marker through and back again
- check a part or chapter boundary reads correctly
- confirm any in-book printed contents page still renders

**Tools → Table of Contents → Edit Table of Contents** gives a tree view, which is a faster way to check a hundred entries than scrolling the nav file.

---

## Getting it onto a Kobo

Two things will otherwise make you think the fix failed.

Kobo caches chapter and pagination records in `KoboReader.sqlite`, keyed by the book. Re-sending a file with the same identifier can leave the old structure in place. **Delete the book from the device** through Calibre's device view, eject, then send the fixed file fresh.

If you use the KoboTouchExtended driver, turn on its option to copy the generated KEPUB to a directory for the first run. That lets you unzip what actually landed on the device and confirm the spine and navigation are what you expect, rather than guessing from the reader UI.

---

## Guarantees and limitations

**What is never modified:** the prose. No reflowing, restyling, or rewording. The only mutations are file boundaries, href targets, the OPF manifest and spine, and the two navigation documents. A document that does not need dividing is passed through byte-for-byte rather than reserialised.

**What is modified:** file names of split documents (`chapter001.html` becomes `chapter001_000.html` and so on), internal link targets that crossed a split, manifest and spine entries, `nav.xhtml`, `toc.ncx`, and — unless you pass `--keep-page-list` — the print page map.

**Limitations:**

- No DRM handling of any kind. The book must open in Calibre's editor.
- Chapter detection proposes; it does not decide. `--auto` is good enough to reproduce three hand-made fixes exactly, but publishers are inconsistent and a wrong split silently corrupts structure, so the plan file still exists and is still worth reading. The confidence scores exist to tell you *where* to look, not to spare you looking.
- The scoring was tuned against three books. It is a considered set of features rather than a fitted model, but three books is three books — run `verify`, and read the low-confidence end of the plan.
- Split points must be siblings within the same parent for `--include-preceding` and `--require-following` to work, which is the normal case but not guaranteed.
- `image_size` understands PNG, JPEG, and GIF. A WebP or SVG cover will be swapped in but its SVG wrapper will not be resized.
- Tested against InDesign exports, Gollancz SF Gateway titles, and hand-built fixtures covering nested wrappers, endnote round-trips, mixed inline content, and both cover-declaration conventions. Not exhaustively tested against every producer in existence — always run `verify`, and Check Book, on the output.

## Regression tests

`tests/regression.py` runs the automatic pipeline over a corpus of books that
were repaired by hand, and requires the result to match the hand-made fix entry
for entry. The books are purchased files and are not in the repository, so
point it at a directory holding them:

```bash
uv run tests/regression.py --corpus ../epubs
```

It also keeps one known-bad file — a discarded intermediate run — and requires
`verify` to reject it, so the safety net is itself checked.
