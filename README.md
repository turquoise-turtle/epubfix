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
- [The plan file](#the-plan-file)
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
# 1. Look at the book's structure and propose chapter boundaries
uv run epubfix.py scan "book.epub" -o plan.json

# 2. Open plan.json and review. Disable false positives, fix titles, set levels.

# 3. Apply
uv run epubfix.py apply "book.epub" plan.json -o "book-fixed.epub"
```

If step 1 finds nothing useful, the book probably has no real headings. Use [`classes`](#classes) to discover how its chapters are actually marked up, then re-scan with a selector.

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

The header output tells you the spine document count, whether a page map is present, and the current table of contents. Then each spine document is listed with its candidates.

Lines prefixed `-` are candidates that failed `--require-following`; they are written to the plan as `"split": false` rather than dropped, so you can review and re-enable them. A `<-N` marker means `--include-preceding` moved that split point back N positions.

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
| `split` | **Edit this.** `false` leaves the text in place and creates no entry. |
| `level` | **Edit this.** `1` for top-level, `2` to nest under the preceding level-1 entry. |
| `title` | **Edit this.** The table-of-contents label. |

Because `class` is included in every record, disabling a whole category of false positives is usually a single find-and-replace in your editor rather than 50 individual edits.

---

## Recipes

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

**What is never modified:** the prose. No reflowing, restyling, or rewording. The only mutations are file boundaries, href targets, the OPF manifest and spine, and the two navigation documents. A document that does not need dividing is passed through byte-for-byte rather than reserialized.

**What is modified:** file names of split documents (`chapter001.html` becomes `chapter001_000.html` and so on), internal link targets that crossed a split, manifest and spine entries, `nav.xhtml`, `toc.ncx`, and — unless you pass `--keep-page-list` — the print page map.

**Limitations:**

- No DRM handling of any kind. The book must open in Calibre's editor.
- Chapter detection is not automatic and is not meant to be. Publishers are too inconsistent, and a wrong split silently corrupts structure. The plan file exists so a human makes the call.
- Split points must be siblings within the same parent for `--include-preceding` and `--require-following` to work, which is the normal case but not guaranteed.
- `image_size` understands PNG, JPEG, and GIF. A WebP or SVG cover will be swapped in but its SVG wrapper will not be resized.
- Tested against InDesign exports, Gollancz SF Gateway titles, and hand-built fixtures covering nested wrappers, endnote round-trips, mixed inline content, and both cover-declaration conventions. Not exhaustively tested against every producer in existence — always run Check Book on the output.
