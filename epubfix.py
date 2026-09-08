#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lxml"]
# ///
"""
epubfix.py - repair EPUB navigation structure without touching the prose.

See README.md for usage. This header explains the design decisions.

WHY THIS EXISTS
    Retail EPUBs frequently ship with two defects that ruin the reading
    experience on a Kobo:

    1. A print page map (EPUB 3 page-list nav, NCX <pageList>, or an Adobe
       page-map.xml). Kobo honours it in preference to computing its own
       pagination, so when the map is misaligned with the spine you get
       nonsense: front matter reporting page 200-something, chapter 1
       reporting page 9. Deleting the map makes Kobo fall back to its own
       consistent count.

    2. A whole novel in one or two enormous XHTML files. Kobo's chapter
       progress ("X minutes left in this chapter") and its progress-bar
       segmentation track *spine documents*, not ToC anchors. So adding 50
       anchor-based ToC entries gives you navigation but leaves progress
       tracking useless. Only splitting the files into one document per
       chapter actually fixes it.

WHY THE WORKFLOW IS TWO-STEP
    Chapter detection cannot be reliably automated across publishers. An
    InDesign export uses styled <p class="_-Chapter-Number">; a Gollancz SF
    Gateway book has no headings at all and marks chapters with an epigraph
    <div class="blockquote">; others use real <h2> elements. Worse, the same
    markup often appears mid-chapter for entirely different reasons - a song
    a character overhears is styled identically to a chapter epigraph.

    No heuristic gets this right unsupervised, and a wrong split silently
    corrupts the book's structure. So `scan` proposes and a human disposes:
    the plan file is deliberately plain JSON so it can be reviewed, bulk
    edited in any text editor, and re-applied. Candidates that fail a filter
    are written as "split": false rather than dropped, so nothing is ever
    silently discarded and every decision stays visible and reversible.

WHY THE TEXT IS GUARANTEED UNTOUCHED
    This operates on files bought and paid for, where silent corruption may
    not surface until someone is 300 pages in. So the only mutations are
    structural: file boundaries, href targets, the OPF manifest/spine, and
    the two navigation documents. No reflowing, restyling, or rewording. A
    document that does not actually need dividing is passed through
    byte-for-byte rather than reserialized, because a needless XML round-trip
    is a needless opportunity to lose something.

WHY SPLITTING IS RISKIER THAN IT LOOKS
    Cutting an XHTML file at a chapter boundary silently breaks every
    internal link that crossed the cut - and endnote-heavy books are full of
    them. The link rewriting in cmd_apply is therefore not an optional extra;
    it is the part most likely to be got wrong by a naive implementation.
    Links that cannot be resolved are reported rather than quietly
    redirected, since those usually indicate a defect that was already
    present in the publisher's file.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import html.entities
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, quote, urldefrag

from lxml import etree

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "epub": "http://www.idpf.org/2007/ops",
    "xlink": "http://www.w3.org/1999/xlink",
    "cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
}
XH = "{%s}" % NS["xhtml"]
OPF = "{%s}" % NS["opf"]
NCX = "{%s}" % NS["ncx"]
EPUB = "{%s}" % NS["epub"]

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

XHTML_MEDIA = {"application/xhtml+xml", "text/html"}

PAGEMAP_MEDIA = "application/oebps-page-map+xml"
NCX_MEDIA = "application/x-dtbncx+xml"


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

_ENT = re.compile(rb"&([a-zA-Z][a-zA-Z0-9]{1,31});")
_KEEP = {b"amp", b"lt", b"gt", b"quot", b"apos"}


def fix_entities(data: bytes) -> bytes:
    """Replace HTML named entities that XML parsers don't know about.

    Publisher XHTML is full of &nbsp;, &mdash;, &rsquo; and friends. These are
    defined by HTML, not XML, so a conforming XML parser rejects the document
    unless the DTD that declares them is fetched - which we will not do offline.
    Substituting the literal characters up front is simpler and lossless.
    """
    def sub(m):
        name = m.group(1)
        if name in _KEEP:
            return m.group(0)
        ch = html.entities.html5.get(name.decode("ascii") + ";")
        if ch is None:
            return m.group(0)
        return ch.encode("utf-8")
    return _ENT.sub(sub, data)


def parse_xml(data: bytes):
    parser = etree.XMLParser(recover=True, resolve_entities=False,
                             load_dtd=False, huge_tree=True)
    return etree.fromstring(fix_entities(data), parser=parser)


def serialize_xhtml(root) -> bytes:
    """Serialize as XHTML, forcing non-void empty elements to a full tag pair.

    lxml writes an empty element as <div/>. That is valid XML, but some
    e-reader engines parse XHTML with an HTML parser, which treats <div/> as an
    unclosed <div> and swallows the rest of the document. Giving such elements
    an empty text node forces <div></div> and sidesteps the whole question.
    """
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el).localname.lower()
        if local not in VOID and len(el) == 0 and el.text is None:
            el.text = ""
    return etree.tostring(root, xml_declaration=True, encoding="utf-8",
                          doctype="<!DOCTYPE html>")


def serialize_xml(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def heading_text(el) -> str:
    """Title for a split point: the element's own text, or its first heading.

    When the split point is a container (a <section> wrapping a chapter, say)
    its full text is the entire chapter, which is useless as a ToC label. The
    first heading inside it is what the reader would recognise.
    """
    local = etree.QName(el).localname.lower()
    if local in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return text_of(el)
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        found = el.find(".//" + XH + tag)
        if found is not None:
            t = text_of(found)
            if t:
                return t
    return text_of(el)


def text_of(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def find_body(root):
    """The document body, with or without the XHTML namespace.

    Publisher files are inconsistent about declaring it, and a namespace-less
    <body> is common enough in older EPUB 2 exports to be worth handling rather
    than skipping the document.
    """
    body = root.find(XH + "body")
    return body if body is not None else root.find("body")


def posix_join(base: str, href: str) -> str:
    href = unquote(urldefrag(href)[0])
    if not href:
        return ""
    p = PurePosixPath(base) / href if base else PurePosixPath(href)
    parts: list[str] = []
    for part in p.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def relpath(target: str, from_dir: str) -> str:
    t = target.split("/")
    b = from_dir.split("/") if from_dir else []
    i = 0
    while i < len(b) and i < len(t) - 1 and b[i] == t[i]:
        i += 1
    return "/".join([".."] * (len(b) - i) + t[i:])


# --------------------------------------------------------------------------
# the book
# --------------------------------------------------------------------------

class Epub:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.files: dict[str, bytes] = {}
        self.order: list[str] = []
        with zipfile.ZipFile(self.path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                self.order.append(info.filename)
                self.files[info.filename] = z.read(info.filename)

        self.opf_path = self._find_opf()
        self.opf_dir = str(PurePosixPath(self.opf_path).parent)
        if self.opf_dir == ".":
            self.opf_dir = ""
        self.opf = parse_xml(self.files[self.opf_path])
        self._read_manifest()

    def _find_opf(self) -> str:
        cont = self.files.get("META-INF/container.xml")
        if cont is not None:
            root = parse_xml(cont)
            for rf in root.iter("{%s}rootfile" % NS["cnt"]):
                fp = rf.get("full-path")
                if fp and fp in self.files:
                    return fp
        for name in self.files:
            if name.lower().endswith(".opf"):
                return name
        raise SystemExit("Could not locate the OPF package document.")

    def _read_manifest(self):
        self.manifest_el = self.opf.find(OPF + "manifest")
        self.spine_el = self.opf.find(OPF + "spine")
        if self.manifest_el is None or self.spine_el is None:
            raise SystemExit("OPF is missing a manifest or spine.")
        self.items: dict[str, dict] = {}
        for it in self.manifest_el.findall(OPF + "item"):
            iid = it.get("id")
            href = it.get("href", "")
            self.items[iid] = {
                "el": it,
                "id": iid,
                "href": href,
                "zip": posix_join(self.opf_dir, href),
                "media": it.get("media-type", ""),
                "properties": it.get("properties", "") or "",
            }
        self.spine: list[str] = []
        for ir in self.spine_el.findall(OPF + "itemref"):
            idref = ir.get("idref")
            if idref in self.items:
                self.spine.append(idref)

    # -- convenience lookups ------------------------------------------------

    def spine_docs(self) -> list[dict]:
        out = []
        for idref in self.spine:
            it = self.items[idref]
            if it["media"] in XHTML_MEDIA:
                out.append(it)
        return out

    def nav_item(self):
        for it in self.items.values():
            if "nav" in it["properties"].split():
                return it
        return None

    def ncx_item(self):
        toc_id = self.spine_el.get("toc")
        if toc_id and toc_id in self.items:
            return self.items[toc_id]
        for it in self.items.values():
            if it["media"] == NCX_MEDIA:
                return it
        return None


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

DEFAULT_SELECT = "h1|h2|h3|h4"


def build_matcher(select: str, class_re: str | None, id_re: str | None = None):
    """Compose a predicate for "is this element a chapter boundary?".

    Three independent axes because no single one covers the corpus: tag name
    for well-formed books, @class for InDesign and Gollancz exports that have
    no headings at all, and @id for exports whose only durable marker is a
    generated anchor. The id test also looks *inside* the element, because
    InDesign emits <p class="x"><a id="_idParaDest-7"/>Title</p> - the anchor
    is a child of the block we actually want to cut at.
    """
    tags = {t.strip().lower() for t in (select or "").split("|") if t.strip()}
    if tags == {"none"}:          # PowerShell swallows empty-string arguments; "none" means "no tags"
        tags = set()
    crx = re.compile(class_re, re.I) if class_re else None
    irx = re.compile(id_re, re.I) if id_re else None

    def match(el) -> bool:
        if not isinstance(el.tag, str):
            return False
        if tags and etree.QName(el).localname.lower() in tags:
            return True
        if crx is not None and crx.search(el.get("class") or ""):
            return True
        if irx is not None:
            if irx.search(el.get("id") or ""):
                return True
            for kid in el.iter():                      # anchor nested inside the block
                if isinstance(kid.tag, str) and irx.search(kid.get("id") or ""):
                    return True
        return False
    return match


# --------------------------------------------------------------------------
# automatic selector discovery
# --------------------------------------------------------------------------
#
# WHY A BIGRAM SCORER
#     The expensive part of using this tool was never editing the plan file;
#     it was reading `classes` output and guessing which of fifteen styles
#     marks a chapter. Frequency alone cannot tell you: in a Gollancz book
#     div.blockquote (16 uses), p.center (17) and p.blockquote1 (18) are the
#     chapter epigraph, its attribution, and the verse styling *inside* it.
#
#     What separates them is what comes NEXT. A chapter opener is followed by
#     the chapter's first paragraph; an interior song is followed by more of
#     the same scene. So we score adjacent style pairs rather than styles:
#
#         div.blockquote(16) -> p.noindent(13)      Gollancz / SF Gateway
#         p._-Chapter-Number(116) -> p._-Chapter-Name(103) -> ...   InDesign
#
#     Those are precisely the two hand-tuned recipes in the README, recovered
#     without being told anything about the book.
#
# WHY NOT SPACING REGULARITY
#     The obvious heuristic - chapters are evenly spaced, so reject candidates
#     that arrive too soon - was measured against a known-good fix and does not
#     work. Real chapters in Dune Messiah run from 4.5K to 26K characters, so a
#     "too close together" rule flagged two real chapters and missed two of the
#     three interior songs. Local context is the signal; rhythm is not.

INLINE_TAGS = {
    "span", "a", "br", "em", "i", "b", "strong", "img", "sup", "sub",
    "small", "u", "code", "cite", "abbr", "q", "s", "mark", "time",
}

# A style whose median text is at least this long is prose, not a label.
BODY_LIKE_LEN = 60
# A style whose median text is at most this long reads as a heading fragment.
TITLE_LIKE_LEN = 45
# Chain links must be near 1:1 in frequency: a two-line chapter heading uses
# both its styles once per chapter. This is what stops a 307-use wrapper <div>
# from swallowing the 116-use chapter number that follows it.
CHAIN_BALANCE = 0.75

# An attribution line opens with a dash: "-Ancient Fremen Saying".
ATTRIB_DASH = re.compile(r"^\s*[\u2014\u2013\u2012-]")

CHAPTERISH = re.compile(
    r"^(chapter|part|book|section|volume|prologue|epilogue|interlude)\b"
    r"|^\d{1,3}$"
    r"|^[ivxlcdm]{1,7}$",
    re.I,
)


def next_block(el):
    """The next sibling that is a real element, skipping comments and PIs."""
    for sib in el.itersiblings():
        if isinstance(sib.tag, str):
            return sib
    return None


def prev_block(el):
    for sib in el.itersiblings(preceding=True):
        if isinstance(sib.tag, str):
            return sib
    return None


def style_of(el) -> tuple[str, str]:
    return (etree.QName(el).localname.lower(), el.get("class") or "")


def style_label(sig: tuple[str, str]) -> str:
    return f"{sig[0]}.{sig[1]}" if sig[1] else sig[0]


def block_elements(body) -> list:
    """Block-level descendants in document order, inline formatting removed.

    Inline elements are dropped because they describe a run of text, not a
    structural unit, and including them swamps every tally with <em> and <a>.
    """
    out = []
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el).localname.lower()
        if local == "body" or local in INLINE_TAGS:
            continue
        out.append(el)
    return out


class StyleStats:
    """Style frequencies, median text lengths and adjacency counts for a book.

    Accumulated across every spine document rather than per document, because a
    book that already ships one file per chapter shows each style exactly once
    per file - individually below any useful threshold, decisive in aggregate.
    """

    def __init__(self):
        self.counts: dict[tuple[str, str], int] = {}
        self.lengths: dict[tuple[str, str], list[int]] = {}
        self.bigrams: dict[tuple[tuple, tuple], int] = {}
        self.samples: dict[tuple[str, str], list[str]] = {}
        # descendant block styles, counted once per occurrence of the outer
        # style: used to ask "does this epigraph carry an attribution line?"
        self.contains: dict[tuple[str, str], dict[tuple[str, str], int]] = {}
        self.total = 0

    def add_document(self, body):
        for el in block_elements(body):
            sig = style_of(el)
            t = text_of(el)
            self.counts[sig] = self.counts.get(sig, 0) + 1
            self.lengths.setdefault(sig, []).append(len(t))
            self.total += 1
            s = self.samples.setdefault(sig, [])
            if t and len(s) < 8:
                s.append(t)
            inner = self.contains.setdefault(sig, {})
            for d in {style_of(k) for k in el.iter()
                      if isinstance(k.tag, str) and k is not el
                      and etree.QName(k).localname.lower() not in INLINE_TAGS}:
                inner[d] = inner.get(d, 0) + 1
            nxt = next_block(el)
            if nxt is not None:
                key = (sig, style_of(nxt))
                self.bigrams[key] = self.bigrams.get(key, 0) + 1

    def median_len(self, sig) -> float:
        vals = sorted(self.lengths.get(sig, [0]))
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    def is_body_like(self, sig) -> bool:
        return self.median_len(sig) >= BODY_LIKE_LEN

    def is_title_like(self, sig) -> bool:
        return self.median_len(sig) <= TITLE_LIKE_LEN

    def dominant(self):
        if not self.counts:
            return None
        return max(self.counts.items(), key=lambda kv: kv[1])[0]

    def has_letters(self, sig) -> bool:
        """False for dingbats and rules - '* * *' is a scene break, not a chapter."""
        return any(re.search(r"[^\W_]", s) for s in self.samples.get(sig, []))

    def attribution_styles(self) -> set:
        """Styles that hold a source line - "-Tleilaxu Epigram" and the like.

        A chapter epigraph is quoted from somewhere and says so; a song a
        character overhears in the middle of a scene is not attributed. That
        turns out to separate the two perfectly where the "what paragraph style
        follows?" test cannot, because both are followed by ordinary prose.
        """
        out = set()
        for sig, texts in self.samples.items():
            if sig[0] in INLINE_TAGS or len(texts) < 3:
                continue
            if sum(1 for t in texts if ATTRIB_DASH.match(t)) / len(texts) > 0.6:
                out.add(sig)
        return out

    def attribution_rate(self, sig, attrib: set) -> float:
        """Share of this style's occurrences that contain an attribution."""
        n = self.counts.get(sig, 0)
        if not n:
            return 0.0
        inner = self.contains.get(sig, {})
        return min(1.0, sum(inner.get(a, 0) for a in attrib) / n)

    def chapterish_fraction(self, sig) -> float:
        s = self.samples.get(sig, [])
        if not s:
            return 0.0
        return sum(1 for x in s if CHAPTERISH.match(x)) / len(s)


def collect_stats(book: Epub) -> StyleStats:
    stats = StyleStats()
    for it in book.spine_docs():
        data = book.files.get(it["zip"])
        if data is None:
            continue
        body = find_body(parse_xml(data))
        if body is not None:
            stats.add_document(body)
    return stats


def _chain_from(opener, stats: StyleStats) -> tuple[list, tuple | None]:
    """Follow the dominant-successor chain from an opener style.

    A multi-line chapter heading ("Chapter 1" / "Jayjay" / "Saturday, 8 August
    1914") appears as a run of distinct styles each used once per chapter. We
    walk while the successor is still label-shaped and still roughly 1:1 with
    the opener, and stop at the first prose style - which is simultaneously the
    title-extend set and the "chapter's first paragraph" test.
    """
    chain: list = []
    follower = None
    cur = opener
    seen = {opener}
    base = stats.counts.get(opener, 0)
    for _ in range(4):
        succs = [(n, b) for (a, b), n in stats.bigrams.items() if a == cur]
        if not succs:
            break
        n, nxt = max(succs)
        if nxt in seen:
            break
        cnt = stats.counts.get(nxt, 0)
        balance = min(base, cnt) / max(base, cnt, 1)
        if stats.is_body_like(nxt):
            follower = nxt
            break
        if balance < CHAIN_BALANCE or not stats.is_title_like(nxt):
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain, follower


def follower_styles(opener, chain: list, stats: StyleStats) -> set:
    """Every prose style that regularly opens the text after this marker.

    One style is usually not enough. An InDesign book alternates Text-Drop and
    Text-Drop-2 depending on whether the drop cap needs a different indent, and
    treating either as "not the expected paragraph" would mark half the real
    chapters as doubtful. Requiring a decent share of the marker's occurrences
    keeps the set to genuine chapter openings. The dominant body style is
    always excluded: "followed by ordinary body text" is the null hypothesis,
    true of every mid-scene quotation in the book, so admitting it would let
    the set match everything and discriminate nothing.
    """
    tail = chain[-1] if chain else opener
    need = max(2, 0.15 * stats.counts.get(opener, 0))
    dominant = stats.dominant()
    return {b for (a, b), n in stats.bigrams.items()
            if a == tail and b != dominant and n >= need and stats.is_body_like(b)}


def propose_selectors(stats: StyleStats, limit: int = 5) -> list[dict]:
    """Rank candidate chapter-opener styles. Highest score first."""
    dominant = stats.dominant()
    max_share = 0.25 * max(stats.total, 1)
    scored: list[dict] = []

    for (a, b), n in stats.bigrams.items():
        if a == b or n < 3:
            continue
        if a == dominant or b == dominant:
            continue
        ca, cb = stats.counts.get(a, 0), stats.counts.get(b, 0)
        if ca > max_share or cb > max_share:
            continue
        coverage = n / max(ca, 1)
        balance = min(ca, cb) / max(ca, cb, 1)
        score = coverage * balance * n

        # A style with no letters in it is a rule or a dingbat. It is followed
        # by body text as faithfully as any chapter heading, and would
        # otherwise outrank the real one.
        if not stats.has_letters(a):
            score *= 0.3
        # "Chapter 7", a bare numeral or a roman numeral is near-conclusive.
        score *= 1.0 + 2.0 * stats.chapterish_fraction(a)

        chain, follower = _chain_from(a, stats)
        scored.append({
            "opener": a,
            "chain": chain,
            "follower": follower,
            "followers": follower_styles(a, chain, stats),
            "n": n,
            "count": ca,
            "score": score,
        })

    scored.sort(key=lambda p: -p["score"])

    # Drop proposals that are merely a link inside a stronger proposal's chain:
    # _-Chapter-Name -> _-Chapter-Date scores well on its own, but it is the
    # tail of the _-Chapter-Number heading, not a separate chapter marker.
    absorbed: set = set()
    kept: list[dict] = []
    for p in scored:
        if p["opener"] in absorbed:
            continue
        kept.append(p)
        absorbed.update(p["chain"])
    return kept[:limit]


def proposal_command(p: dict) -> str:
    """The explicit scan invocation equivalent to a proposal.

    --auto prints this so it stays a suggestion engine rather than a black box:
    you can copy the line, adjust one regex, and re-run by hand.
    """
    def anchored(sig):
        return "^" + re.escape(sig[1]) + "$" if sig[1] else "^$"
    bits = ["--no-tags", f'--class-regex "{anchored(p["opener"])}"']
    if p["chain"]:
        bits.append('--title-extend "%s"' % "|".join(anchored(s) for s in p["chain"]))
    if p["followers"]:
        bits.append('--require-following "%s"'
                    % "|".join(sorted(anchored(f) for f in p["followers"])))
    return " ".join(bits)


# --------------------------------------------------------------------------
# per-candidate confidence
# --------------------------------------------------------------------------

CSS_BREAK = re.compile(
    r"\.([A-Za-z_][\w\-]*)[^{}]*\{[^{}]*?(?:page-)?break-before\s*:\s*(?:always|page|left|right)",
    re.I | re.S,
)


def page_break_classes(book: Epub) -> set[str]:
    """Class names the book's own stylesheets force a page break before.

    A publisher writing page-break-before is stating "this starts a new page",
    which is the closest thing to an explicit chapter declaration that CSS has.
    """
    out: set[str] = set()
    for it in book.items.values():
        if "css" not in it["media"]:
            continue
        data = book.files.get(it["zip"])
        if not data:
            continue
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            continue
        for m in CSS_BREAK.finditer(text):
            out.add(m.group(1))
    return out


def effective_follower(el, chain: set):
    """The first block after the heading that is not part of the heading itself.

    For a three-line InDesign chapter heading the immediate next sibling is
    still the heading (the POV name), so testing it against "is this body
    text?" would reject every real chapter in the book.
    """
    cur = el
    for _ in range(len(chain) + 1):
        nxt = next_block(cur)
        if nxt is None:
            return None
        if style_of(nxt) not in chain:
            return nxt
        cur = nxt
    return None


def score_candidate(el, stats: StyleStats, chain: set, followers: set,
                    breaks: set, attrib: set, use_attrib: bool) -> tuple[float, str]:
    """Confidence in [0,1] that this element opens a chapter, plus why.

    Returns a reason string so a low score can be argued with rather than
    merely obeyed - the plan file is still reviewed by a human.
    """
    reasons: list[str] = []
    text = heading_text(el)
    if not text.strip():
        return 0.0, "no text"

    score = 0.35
    dominant = stats.dominant()
    nxt = effective_follower(el, chain)

    is_prose = nxt is not None and stats.is_body_like(style_of(nxt))
    if followers:
        # The book has a distinct "first paragraph of a chapter" style. Prose in
        # any OTHER style is therefore positive evidence that this marker sits
        # mid-scene - which is exactly what separates a chapter epigraph from a
        # song a character overhears.
        if nxt is not None and style_of(nxt) in followers:
            score += 0.45
            reasons.append(f"opens with {style_label(style_of(nxt))}")
        elif is_prose:
            score -= 0.15
            reasons.append("prose follows, but not a chapter-opening style")
        else:
            # No prose after it at all: whatever this is, it does not open a
            # chapter of text. Weighted to stay decisive even when the element
            # is an attributed epigraph, as a closing epigraph in an afterword
            # is.
            score -= 0.30
            reasons.append("not followed by prose")
    elif is_prose:
        score += 0.40
        reasons.append("followed by prose")
    else:
        reasons.append("not followed by prose")

    # A chapter is a run of body text, not one stray paragraph.
    run, cur = 0, nxt
    while cur is not None and run < 3:
        if style_of(cur) != dominant and not stats.is_body_like(style_of(cur)):
            break
        run += 1
        cur = next_block(cur)
    if run >= 3:
        score += 0.20
        reasons.append("run of body text follows")

    if use_attrib:
        # Only consulted when the marker style is sometimes-but-not-always
        # attributed. A style that always carries a source line, or never does,
        # tells us nothing about which of its occurrences opens a chapter.
        cited = any(style_of(k) in attrib for k in el.iter()
                    if isinstance(k.tag, str) and k is not el)
        if cited:
            score += 0.35
            reasons.append("carries an attribution")
        else:
            score -= 0.25
            reasons.append("no attribution - reads as an in-scene quotation")

    if CHAPTERISH.match(text):
        score += 0.25
        reasons.append("reads as a chapter label")

    if not re.search(r"[^\W_]", text):
        score -= 0.50
        reasons.append("no letters - looks like an ornament")

    # The publisher's own stylesheet forcing a page break before this style is
    # a statement that a new section begins here, and it outranks anything
    # inferred from the surrounding text. It applies to every occurrence of the
    # style equally, so it is a floor rather than a bonus - adding a constant
    # to every candidate would change no ordering at all. This is what keeps
    # front matter whose body is a list or a poem, rather than prose ("Cast",
    # "Contents", "Let's Remember"), in the table of contents.
    cls = el.get("class") or ""
    if cls and cls in breaks:
        if score < 0.55:
            reasons.append("stylesheet forces a page break before this style")
        score = max(score, 0.55)

    return max(0.0, min(1.0, score)), "; ".join(reasons)


# --------------------------------------------------------------------------
# titles and levels
# --------------------------------------------------------------------------

DINGBATS = "•⁃▪●∗*~-–—_=+#·∙ \t"


def clean_title(text: str) -> str:
    """Tidy a detected title without changing what it says.

    Publisher headings arrive wrapped in decorative rules and, from InDesign,
    frequently in full capitals because the typeface did the styling. Both are
    presentation, and neither belongs in a navigation label.
    """
    t = re.sub(r"\s+", " ", text.replace(" ", " ")).strip()
    t = t.strip(DINGBATS).strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        t = t.title()
        # Roman numerals and single letters lose to .title(); put them back.
        t = re.sub(r"\b([IVXLCDM]+)\b",
                   lambda m: m.group(1).upper(),
                   t, flags=re.I) if re.search(r"\b[ivxlcdm]+\b", t, re.I) else t
    return t


def titles_are_prose(titles: list[str]) -> bool:
    """True when the detected 'titles' are really the opening prose.

    Some books mark chapters only with an epigraph and carry no chapter title
    anywhere in the text. Emitting the epigraph as a navigation label is what
    produced the shipped table of contents this tool exists to replace, so it
    is worth detecting and numbering instead.
    """
    real = [t for t in titles if t.strip()]
    if len(real) < 3:
        return False
    long_ones = sum(1 for t in real if len(t) > 60)
    return long_ones / len(real) > 0.5


def infer_levels(cands: list[dict]) -> None:
    """Assign level 1/2 in place, from how the marker styles interleave.

    A book with parts uses a rare marker (3 uses) and a dense one (116). The
    rare one is the shallower level, but only if it actually behaves like a
    parent - each occurrence followed by a run of the denser marker. Otherwise
    the two are siblings and everything stays level 1.
    """
    groups: dict[tuple, list[dict]] = {}
    for c in cands:
        groups.setdefault((c["tag"], c["class"]), []).append(c)
    if len(groups) < 2:
        return
    ranked = sorted(groups.items(), key=lambda kv: len(kv[1]))
    rare_key, rare = ranked[0]
    dense_total = sum(len(v) for k, v in ranked[1:])
    if len(rare) < 2 or dense_total < 3 * len(rare):
        return
    order = sorted(cands, key=lambda c: (c["doc_index"], c["pos"]))
    rare_ids = {id(c) for c in rare}
    # every rare marker must be followed by at least one dense marker
    parented, pending = 0, False
    for c in order:
        if id(c) in rare_ids:
            pending = True
        elif pending:
            parented += 1
            pending = False
    if parented < len(rare):
        return
    for c in cands:
        c["level"] = 1 if id(c) in rare_ids else 2


def existing_toc(book: Epub) -> list[dict]:
    entries = []
    nav = book.nav_item()
    if nav is not None and nav["zip"] in book.files:
        root = parse_xml(book.files[nav["zip"]])
        for n in root.iter(XH + "nav"):
            if (n.get(EPUB + "type") or "") == "toc":
                for a in n.iter(XH + "a"):
                    entries.append({"title": text_of(a), "href": a.get("href", "")})
                break
    if not entries:
        ncx = book.ncx_item()
        if ncx is not None and ncx["zip"] in book.files:
            root = parse_xml(book.files[ncx["zip"]])
            for np in root.iter(NCX + "navPoint"):
                lbl = np.find(NCX + "navLabel/" + NCX + "text")
                content = np.find(NCX + "content")
                entries.append({
                    "title": text_of(lbl) if lbl is not None else "",
                    "href": content.get("src", "") if content is not None else "",
                })
    return entries


def toc_with_levels(book: Epub) -> list[dict]:
    """The existing table of contents, keeping its nesting depth.

    existing_toc flattens, which is all the header display needs. Seeding a
    plan from the publisher's own navigation needs the hierarchy too, since
    that is half of what makes the entry worth reusing.
    """
    out: list[dict] = []
    nav = book.nav_item()
    if nav is not None and nav["zip"] in book.files:
        root = parse_xml(book.files[nav["zip"]])
        for n in root.iter(XH + "nav"):
            if (n.get(EPUB + "type") or "") != "toc":
                continue

            def walk(ol, level):
                for li in ol.findall(XH + "li"):
                    a = li.find(XH + "a")
                    if a is not None:
                        out.append({"title": text_of(a), "href": a.get("href", ""),
                                    "level": level})
                    for sub in li.findall(XH + "ol"):
                        walk(sub, level + 1)
            for ol in n.findall(XH + "ol"):
                walk(ol, 1)
            break
    if out:
        return out
    ncx = book.ncx_item()
    if ncx is not None and ncx["zip"] in book.files:
        root = parse_xml(book.files[ncx["zip"]])
        navmap = root.find(NCX + "navMap")

        def walk_ncx(parent, level):
            for np in parent.findall(NCX + "navPoint"):
                lbl = np.find(NCX + "navLabel/" + NCX + "text")
                content = np.find(NCX + "content")
                out.append({
                    "title": text_of(lbl) if lbl is not None else "",
                    "href": content.get("src", "") if content is not None else "",
                    "level": level,
                })
                walk_ncx(np, level + 1)
        if navmap is not None:
            walk_ncx(navmap, 1)
    return out


def anchored_toc_positions(book: Epub) -> dict[str, list[dict]]:
    """Existing ToC entries that point INTO a spine document, as cut positions.

    A publisher who ships one enormous XHTML file often still ships correct
    navigation into it - a hundred navPoints aimed at a hundred anchors. When
    that is present it is not a heuristic at all: it is the answer, with the
    right titles and the right nesting, written by whoever made the book.
    """
    src = book.nav_item() or book.ncx_item()
    if src is None:
        return {}
    base = str(PurePosixPath(src["zip"]).parent)
    if base == ".":
        base = ""

    wanted: dict[str, list[dict]] = {}
    for e in toc_with_levels(book):
        target, frag = urldefrag(e["href"])
        if not target or not frag:
            continue
        z = posix_join(base, target)
        wanted.setdefault(z, []).append(dict(e, frag=unquote(frag)))
    if not wanted:
        return {}

    out: dict[str, list[dict]] = {}
    for it in book.spine_docs():
        entries = wanted.get(it["zip"])
        if not entries:
            continue
        body = find_body(parse_xml(book.files[it["zip"]]))
        if body is None:
            continue
        elems = list(body.iter())
        # An anchor is usually an empty <a id> inside the heading we want to cut
        # at, so walk up to the nearest block-level ancestor.
        pos_of_id: dict[str, int] = {}
        for i, el in enumerate(elems):
            if not isinstance(el.tag, str):
                continue
            v = el.get("id")
            if not v or v in pos_of_id:
                continue
            owner, oi = el, i
            while (owner is not None
                   and etree.QName(owner).localname.lower() in INLINE_TAGS
                   and owner.getparent() is not None
                   and owner.getparent() is not body):
                owner = owner.getparent()
                oi = elems.index(owner)
            pos_of_id[v] = oi
        found = []
        for e in entries:
            p = pos_of_id.get(e["frag"])
            if p is not None and p > 0:
                found.append(dict(e, pos=p))
        if found:
            found.sort(key=lambda e: e["pos"])
            out[it["zip"]] = found
    return out


def find_page_list(book: Epub) -> dict:
    info = {"nav_page_list": False, "ncx_page_list": 0, "page_map_file": None}
    nav = book.nav_item()
    if nav is not None and nav["zip"] in book.files:
        root = parse_xml(book.files[nav["zip"]])
        for n in root.iter(XH + "nav"):
            if (n.get(EPUB + "type") or "") == "page-list":
                info["nav_page_list"] = True
    ncx = book.ncx_item()
    if ncx is not None and ncx["zip"] in book.files:
        root = parse_xml(book.files[ncx["zip"]])
        pl = root.find(NCX + "pageList")
        if pl is not None:
            info["ncx_page_list"] = len(pl.findall(NCX + "pageTarget"))
    for it in book.items.values():
        if it["media"] == PAGEMAP_MEDIA:
            info["page_map_file"] = it["zip"]
    return info


def extended_title(el, extend_re, sep: str) -> str:
    """Fold the text of immediately-following sibling blocks whose class matches into the title."""
    parts = [heading_text(el)]
    if extend_re is not None:
        for sib in el.itersiblings():
            if not isinstance(sib.tag, str) or not extend_re.search(sib.get("class") or ""):
                break
            t = text_of(sib)
            if t:
                parts.append(t)
    return sep.join(x for x in parts if x)


def extended_title_styles(el, styles: set, sep: str) -> str:
    """extended_title, but driven by discovered styles rather than a regex.

    --auto knows the heading's continuation styles exactly (it followed the
    chain to find them), so matching them by identity avoids building a regex
    only to parse it back again.
    """
    parts = [heading_text(el)]
    cur = el
    while True:
        sib = next_block(cur)
        if sib is None or style_of(sib) not in styles:
            break
        t = text_of(sib)
        if t:
            parts.append(t)
        cur = sib
    return sep.join(x for x in parts if x)


def absorb_start(el, pre_re, into_re, pos_of: dict) -> int:
    """Move a split point earlier to swallow the blocks immediately before it.

    Books often print a part epigraph *above* the part heading it introduces.
    Cutting at the heading would strand those quotes at the end of the previous
    chapter's file, where they read as a non-sequitur.

    into_re exists because the same quote style is reused elsewhere - a closing
    epigraph before the acknowledgements, for instance. Scoping absorption to
    part headings only keeps that one where the author put it.
    """
    here = pos_of[id(el)]
    if pre_re is None:
        return here
    if into_re is not None and not into_re.search(el.get("class") or ""):
        return here
    start = here
    for sib in el.itersiblings(preceding=True):
        if not isinstance(sib.tag, str) or not pre_re.search(sib.get("class") or ""):
            break
        if id(sib) in pos_of:
            start = pos_of[id(sib)]
    return start


def cmd_scan(args):
    book = Epub(args.epub)
    docs = book.spine_docs()
    stats = collect_stats(book)
    breaks = page_break_classes(book)
    attrib = stats.attribution_styles()
    anchored = anchored_toc_positions(book)
    fingerprint = book_fingerprint(book)
    recipes = load_recipes(Path(args.recipes)) if args.recipes else {}
    known, similarity = match_recipe(recipes, fingerprint)

    print(f"OPF          : {book.opf_path}")
    print(f"Spine docs   : {len(docs)}")
    pl = find_page_list(book)
    print(f"Page list    : nav={pl['nav_page_list']} ncx_targets={pl['ncx_page_list']} "
          f"page-map={pl['page_map_file']}")
    toc = existing_toc(book)
    print(f"Fingerprint  : {fingerprint['id']}  ({len(fingerprint['classes'])} styles)")
    if known:
        print(f"               matches {known.get('from','a saved recipe')} "
              f"({similarity:.0%} style overlap), saved {known.get('saved','?')}: "
              f"split on {', '.join(known['split_styles'])}"
              + (f"; rejected {', '.join(known['rejected_styles'])}" if known.get("rejected_styles") else ""))
    print(f"Existing ToC : {len(toc)} entries")
    for e in toc[:12]:
        print(f"   - {e['title'][:60]!r} -> {e['href']}")
    if len(toc) > 12:
        print(f"   ... and {len(toc)-12} more")

    # ---- proposals ---------------------------------------------------------
    proposals = [] if args.from_toc else propose_selectors(stats)
    chosen = None
    parents: list[dict] = []
    if args.auto or args.propose:
        print(f"\nProposed chapter markers (dominant body style: "
              f"{style_label(stats.dominant()) if stats.dominant() else 'n/a'}):")
        if not proposals:
            print("   none - no style reliably precedes body text. Try 'classes'.")
        for k, p in enumerate(proposals, 1):
            chain = " -> ".join(style_label(s) for s in p["chain"])
            print(f"  {k}. score {p['score']:8.2f}  {p['count']:>4} x {style_label(p['opener'])}"
                  + (f" -> {chain}" if chain else "")
                  + ("  (prose: %s)" % ", ".join(sorted(style_label(f) for f in p["followers"]))
                     if p["followers"] else ""))
            print(f"       {proposal_command(p)}")
        if known and proposals:
            # A reviewed recipe outranks a fresh guess: it encodes decisions a
            # person already made about books built exactly like this one.
            want = set(known["split_styles"])
            match_p = next((p for p in proposals if style_label(p["opener"]) in want), None)
            if match_p is not None and match_p is not proposals[0]:
                print(f"     using the saved recipe's marker "
                      f"({style_label(match_p['opener'])}) over proposal 1")
                proposals = [match_p] + [p for p in proposals if p is not match_p]
        if proposals:
            chosen = proposals[0]
            _r = stats.attribution_rate(chosen["opener"], attrib)
            if attrib and 0.35 <= _r <= 0.97:
                print(f"     attribution test enabled: {_r:.0%} of "
                      f"{style_label(chosen['opener'])} carry a source line "
                      f"({', '.join(sorted(style_label(a) for a in attrib))})")
            # A rarer label-shaped marker that consistently precedes the winner
            # is a part heading, not a rival: keep it and nest under it later.
            for p in proposals[1:]:
                if (stats.is_title_like(p["opener"])
                        and 2 <= p["count"] <= max(2, chosen["count"] // 5)
                        and stats.has_letters(p["opener"])):
                    parents.append(p)

    if args.auto and chosen is None:
        raise SystemExit("--auto found no usable chapter marker; run 'classes' and pass --class-regex.")

    # ---- matcher -----------------------------------------------------------
    if chosen is not None and args.auto:
        openers = {chosen["opener"]} | {p["opener"] for p in parents}
        chain_styles = set(chosen["chain"])
        for p in parents:
            chain_styles |= set(p["chain"])
        followers = set(chosen["followers"])
        for _p in parents:
            followers |= set(_p["followers"])
        rate = stats.attribution_rate(chosen["opener"], attrib)
        use_attrib = 0.35 <= rate <= 0.97

        def match(el):
            return isinstance(el.tag, str) and style_of(el) in openers

        extend_re = None
        extend_styles = chain_styles
    else:
        match = build_matcher("" if args.no_tags else args.select, args.class_regex, args.id_regex)
        extend_re = re.compile(args.title_extend, re.I) if args.title_extend else None
        extend_styles = set()
        followers = set()
        use_attrib = False
        chain_styles = set()

    pre_re = re.compile(args.include_preceding, re.I) if args.include_preceding else None
    req_re = re.compile(args.require_following, re.I) if args.require_following else None
    into_re = re.compile(args.include_preceding_into, re.I) if args.include_preceding_into else None

    plan = {
        "epub": str(Path(args.epub).name),
        "strip_page_list": True,
        "documents": [],
    }

    print()
    all_cands: list[dict] = []
    for doc_index, it in enumerate(docs):
        data = book.files.get(it["zip"])
        if data is None:
            continue
        root = parse_xml(data)
        body = find_body(root)
        if body is None:
            continue
        elems = list(body.iter())
        pos_of = {id(e): i for i, e in enumerate(elems)}
        cands = []

        if args.from_toc:
            for e in anchored.get(it["zip"], []):
                el = elems[e["pos"]]
                cands.append({
                    "pos": e["pos"], "heading_pos": e["pos"],
                    "tag": etree.QName(el).localname.lower(),
                    "class": el.get("class") or "", "id": el.get("id") or "",
                    "depth": 0, "doc_index": doc_index,
                    "text": heading_text(el)[:120],
                    "score": 1.0, "why": "from the book's own navigation",
                    "split": True, "level": int(e["level"]),
                    "title": clean_title(e["title"])[:200] or "Untitled",
                })
            all_cands.extend(cands)
            plan["documents"].append({"file": it["zip"], "bytes": len(data),
                                      "candidates": cands})
            continue

        for i, el in enumerate(elems):
            if i == 0 or not match(el):
                continue
            depth = 0
            p = el.getparent()
            while p is not None and p is not body:
                depth += 1
                p = p.getparent()

            score, why = score_candidate(el, stats, chain_styles, followers,
                                         breaks, attrib, use_attrib)
            if req_re is not None:
                nxt = next_block(el)
                ok = nxt is not None and bool(req_re.search(nxt.get("class") or ""))
                why = "--require-following: " + ("matched" if ok else "no match")
            else:
                ok = score >= args.threshold

            if extend_styles:
                title = extended_title_styles(el, extend_styles, args.title_sep)
            else:
                title = extended_title(el, extend_re, args.title_sep)
            start = absorb_start(el, pre_re, into_re, pos_of)
            cands.append({
                "pos": start,
                "heading_pos": i,
                "tag": etree.QName(el).localname.lower(),
                "class": el.get("class") or "",
                "id": el.get("id") or "",
                "depth": depth,
                "doc_index": doc_index,
                "text": heading_text(el)[:120],
                "score": round(score, 2),
                "why": why,
                "split": ok,
                "level": 1,
                "title": clean_title(title)[:200] or "Untitled",
            })
        all_cands.extend(cands)
        plan["documents"].append({
            "file": it["zip"],
            "bytes": len(data),
            "candidates": cands,
        })

    # ---- cross-check against the publisher's own anchors -------------------
    if anchored and not args.from_toc:
        want = {(z, e["pos"]) for z, v in anchored.items() for e in v}
        got = {(d["file"], c["pos"]) for d in plan["documents"]
               for c in d["candidates"] if c["split"]}
        hit = len(want & got)
        print(f"\nExisting navigation anchors into split documents: {len(want)}; "
              f"{hit} coincide with a chosen split point.")
        # Only worth suggesting when the publisher's navigation is substantially
        # richer than what detection found. Fewer anchors than split points
        # usually means the book shipped part markers only, which --auto has
        # already folded into its titles.
        if len(want) > 3 and len(want) > 1.5 * max(len(got), 1):
            print("  The book's own navigation is more detailed than the "
                  "detected headings; --from-toc will use it instead.")

    # ---- levels and titles -------------------------------------------------
    infer_levels([c for c in all_cands if c["split"]])
    numbering = titles_are_prose([c["title"] for c in all_cands if c["split"]])
    if numbering:
        plan["title_format"] = "Chapter {n}"

    # ---- report ------------------------------------------------------------
    total = 0
    for d in plan["documents"]:
        cands = d["candidates"]
        on = sum(1 for c in cands if c["split"])
        total += on
        extra = f", {len(cands) - on} marked split:false" if on != len(cands) else ""
        print(f"{d['file']}  ({d['bytes']:,} bytes)  {len(cands)} candidate(s){extra}")
        # Least confident first, so review starts where the doubt is. Confident
        # candidates are summarised rather than listed: a 116-chapter book
        # otherwise buries the handful of decisions actually worth making.
        ordered = sorted(cands, key=lambda c: (c["score"], c["pos"]))
        shown = [c for c in ordered if c["score"] < args.show_above] if not args.verbose else ordered
        for c in shown:
            mark = f"<-{c['heading_pos'] - c['pos']}" if c["heading_pos"] != c["pos"] else "   "
            flag = " " if c["split"] else "-"
            print(f"  {flag} {c['score']:.2f} pos={c['pos']:<6}{mark} L{c['level']} "
                  f"{c['tag']:<3} class={c['class'][:20]:<20} {c['title'][:52]!r}  [{c['why']}]")
        rest = len(ordered) - len(shown)
        if rest:
            print(f"    ... and {rest} candidate(s) at {args.show_above:.2f}+ confidence "
                  f"(--verbose to list them)")

    for c in all_cands:
        c.pop("doc_index", None)

    print(f"\nTotal split points: {total}   (lines prefixed '-' are in the plan but disabled; "
          f"flip \"split\" to enable)")
    if numbering:
        print("Titles look like opening prose rather than chapter names; "
              'plan sets title_format "Chapter {n}". Remove it to keep the detected text.')
    levels = sorted({c["level"] for c in all_cands if c["split"]})
    if len(levels) > 1:
        print(f"Levels inferred: {levels} (rarer marker nested as level 1)")

    if args.propose and not args.auto:
        print("\nProposals only - no plan written. Re-run with --auto to use proposal 1.")
        return
    out = Path(args.out)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Plan written to {out}. Edit it, then run 'apply'.")

    if args.save_recipe:
        store = Path(args.save_recipe)
        recipes = load_recipes(store)
        entry = recipe_from_plan(plan)
        entry["saved"] = datetime.date.today().isoformat()
        entry["from"] = Path(args.epub).name
        entry["classes"] = fingerprint["classes"]
        recipes[fingerprint["id"]] = entry
        store.write_text(json.dumps(recipes, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Recipe for {fingerprint['id']} saved to {store}.")
        print("Re-run scan with --recipes on the next book from this producer.")


def cmd_classes(args):
    """Tally every (tag, class) pair and every matching id, so you can find the heading style."""
    book = Epub(args.epub)
    irx = re.compile(args.id_regex, re.I) if args.id_regex else None

    for it in book.spine_docs():
        data = book.files.get(it["zip"])
        if data is None:
            continue
        root = parse_xml(data)
        body = find_body(root)
        if body is None:
            continue

        tally: dict[tuple[str, str], list] = {}
        for el in body.iter():
            if not isinstance(el.tag, str):
                continue
            local = etree.QName(el).localname.lower()
            if local in ("body", "span", "a", "br", "em", "i", "b", "strong", "img"):
                continue
            key = (local, el.get("class") or "")
            rec = tally.setdefault(key, [0, []])
            rec[0] += 1
            if len(rec[1]) < 3:
                t = text_of(el)[:70]
                if t:
                    rec[1].append(t)

        print(f"\n=== {it['zip']}  ({len(data):,} bytes) ===")
        print(f"{'count':>7}  {'tag':<8} {'class':<40} samples")
        rows = sorted(tally.items(), key=lambda kv: kv[1][0])
        for (local, cls), (n, samples) in rows:
            if not args.all and n > args.max_count:
                continue
            print(f"{n:>7}  {local:<8} {cls[:40]:<40} " + " | ".join(repr(x) for x in samples))
        if not args.all:
            hidden = sum(1 for _, (n, _) in rows if n > args.max_count)
            if hidden:
                print(f"({hidden} style(s) with more than {args.max_count} uses hidden; pass --all to see them)")

        if irx is not None:
            hits = []
            for el in body.iter():
                if not isinstance(el.tag, str):
                    continue
                v = el.get("id") or ""
                if irx.search(v):
                    owner = el
                    while owner is not None and etree.QName(owner).localname.lower() in ("a", "span"):
                        owner = owner.getparent()
                    hits.append((v, etree.QName(owner).localname.lower() if owner is not None else "?", (owner.get("class") if owner is not None else "") or "", text_of(owner)[:60] if owner is not None else ""))
            print(f"\n  ids matching /{args.id_regex}/ : {len(hits)}")
            for v, tag, cls, t in hits[:args.show_ids]:
                print(f"    {v:<24} in <{tag} class={cls[:28]!r}> {t!r}")
            if len(hits) > args.show_ids:
                print(f"    ... and {len(hits) - args.show_ids} more (raise --show-ids to see them all)")


IMAGE_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


def image_size(data: bytes):
    """Return (width, height, format) for PNG/JPEG/GIF without any image library.

    Parsing three container headers by hand is less trouble than making Pillow
    a dependency of a script whose entire job is rearranging zip entries. We
    need dimensions only to resize an SVG cover wrapper's viewBox.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"), "png")
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        n = len(data)
        while i < n - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 2
                continue
            seglen = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h, "jpeg")
            i += 2 + seglen
        return (None, None, "jpeg")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"), "gif")
    return (None, None, "unknown")


def find_cover(book: "Epub"):
    """Locate the cover image item, trying each convention in turn.

    There are three ways a cover gets declared and books in the wild use all of
    them, sometimes inconsistently. EPUB 3 marks the manifest item with
    properties="cover-image"; EPUB 2 uses <meta name="cover"> pointing at an
    item id; older or sloppier files declare nothing and simply put an image on
    the first spine page. Returns how it was found so the caller can show the
    user what it latched onto before anything is overwritten.
    """
    for it in book.items.values():
        if "cover-image" in it["properties"].split():
            return it, 'manifest properties="cover-image" (EPUB 3)'
    meta = book.opf.find(OPF + "metadata")
    if meta is not None:
        for m in meta.findall(OPF + "meta"):
            if (m.get("name") or "").lower() == "cover":
                cid = m.get("content")
                if cid in book.items:
                    return book.items[cid], '<meta name="cover"> in metadata (EPUB 2)'
    # fall back to an image referenced by the first spine document
    docs = book.spine_docs()
    if docs:
        first = docs[0]
        try:
            r = parse_xml(book.files[first["zip"]])
        except Exception:
            r = None
        if r is not None:
            base_dir = str(PurePosixPath(first["zip"]).parent)
            if base_dir == ".":
                base_dir = ""
            for el in r.iter():
                if not isinstance(el.tag, str):
                    continue
                local = etree.QName(el).localname.lower()
                ref = el.get("src") if local == "img" else (
                    el.get("{%s}href" % NS["xlink"]) or el.get("href") if local == "image" else None)
                if ref:
                    z = posix_join(base_dir, ref)
                    for it in book.items.values():
                        if it["zip"] == z and it["media"].startswith("image/"):
                            return it, f"image referenced by the first spine document ({first['zip']})"
    return None, "no cover image could be identified"


def cmd_cover(book_path, image_path, out_path):
    book = Epub(book_path)
    item, how = find_cover(book)

    new_bytes = Path(image_path).read_bytes()
    nw, nh, nfmt = image_size(new_bytes)

    print(f"Replacement    : {image_path}")
    print(f"                 {nw}x{nh} {nfmt}, {len(new_bytes):,} bytes")
    if nw and nh:
        print(f"                 aspect ratio 1:{nh / nw:.2f}")

    if item is None:
        print(f"\nERROR: {how}.")
        print("Nothing was written. Inspect the book with 'classes' to see how its cover is wired up.")
        return
    print(f"\nCurrent cover  : {item['zip']}")
    print(f"                 found via {how}")
    old = book.files.get(item["zip"])
    if old:
        ow, oh, ofmt = image_size(old)
        print(f"                 {ow}x{oh} {ofmt}, {len(old):,} bytes")

    # where is it displayed?
    referrers = []
    for it in book.spine_docs():
        try:
            r = parse_xml(book.files[it["zip"]])
        except Exception:
            continue
        base_dir = str(PurePosixPath(it["zip"]).parent)
        if base_dir == ".":
            base_dir = ""
        for el in r.iter():
            if not isinstance(el.tag, str):
                continue
            for attr in ("src", "href", "{%s}href" % NS["xlink"]):
                v = el.get(attr)
                if v and posix_join(base_dir, v) == item["zip"]:
                    referrers.append((it["zip"], etree.QName(el).localname.lower()))
    if referrers:
        for z, tag in referrers:
            print(f"Displayed on   : {z}  (<{tag}>)")
    else:
        print("Displayed on   : not referenced by any spine document (thumbnail only)")

    if out_path is None:
        print("\nReport only - no output file written.")
        print("Re-run with -o OUTPUT.epub to apply the replacement.")
        return

    new_files = dict(book.files)

    # Overwrite in place where possible: every reference in the book already
    # points at this path, so not moving it means nothing else has to change.
    # Only rename when the format genuinely differs, since a .png holding JPEG
    # bytes will be rejected or mis-rendered by strict readers.
    target = item["zip"]
    want_ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif"}.get(nfmt)
    cur_ext = PurePosixPath(target).suffix.lower()
    renamed = None
    if want_ext and cur_ext not in ({".jpg", ".jpeg"} if nfmt == "jpeg" else {want_ext}):
        target = str(PurePosixPath(target).with_suffix(want_ext))
        renamed = (item["zip"], target)

    if renamed:
        new_files.pop(renamed[0], None)
    new_files[target] = new_bytes

    new_media = IMAGE_MEDIA.get(PurePosixPath(target).suffix.lower(), item["media"])
    item["el"].set("media-type", new_media)
    if renamed:
        item["el"].set("href", quote(relpath(target, book.opf_dir), safe="/"))

    # make sure the cover pointers exist. properties="cover-image" is EPUB 3 only,
    # so don't add it to a 2.0 package where epubcheck would reject it.
    pkg_version = (book.opf.get("version") or "").strip()
    if pkg_version.startswith("3"):
        props = set(item["properties"].split())
        props.add("cover-image")
        item["el"].set("properties", " ".join(sorted(props)))
    meta = book.opf.find(OPF + "metadata")
    if meta is not None:
        existing = [m for m in meta.findall(OPF + "meta")
                    if (m.get("name") or "").lower() == "cover"]
        if existing:
            for m in existing:
                m.set("content", item["id"])
        else:
            m = etree.SubElement(meta, OPF + "meta")
            m.set("name", "cover")
            m.set("content", item["id"])

    # update the cover page: rewrite refs, and resize any SVG wrapper
    fixed_pages = []
    for it in book.spine_docs():
        zname = it["zip"]
        if zname not in new_files:
            continue
        try:
            r = parse_xml(new_files[zname])
        except Exception:
            continue
        base_dir = str(PurePosixPath(zname).parent)
        if base_dir == ".":
            base_dir = ""
        changed = False
        for el in r.iter():
            if not isinstance(el.tag, str):
                continue
            local = etree.QName(el).localname.lower()
            for attr in ("src", "href", "{%s}href" % NS["xlink"]):
                v = el.get(attr)
                if not v or posix_join(base_dir, v) != (renamed[0] if renamed else item["zip"]):
                    continue
                if renamed:
                    el.set(attr, quote(relpath(target, base_dir), safe="/"))
                    changed = True
                if local in ("img", "image") and nw and nh:
                    # only rewrite absolute pixel dimensions; leave %/em/auto alone,
                    # since those are deliberately responsive
                    def _abs(v):
                        return v is not None and v.strip().isdigit()
                    if _abs(el.get("width")):
                        el.set("width", str(nw))
                        changed = True
                    if _abs(el.get("height")):
                        el.set("height", str(nh))
                        changed = True
                    svg = el.getparent()
                    while svg is not None and etree.QName(svg).localname.lower() != "svg":
                        svg = svg.getparent()
                    if svg is not None:
                        svg.set("viewBox", f"0 0 {nw} {nh}")
                        if _abs(svg.get("width")):
                            svg.set("width", str(nw))
                        if _abs(svg.get("height")):
                            svg.set("height", str(nh))
                        changed = True
        if changed:
            new_files[zname] = serialize_xhtml(r)
            fixed_pages.append(zname)

    new_files[book.opf_path] = serialize_xml(book.opf)

    # Rebuild the archive rather than editing in place: "mimetype" must be the
    # first entry and stored uncompressed for the file to be recognised as an
    # EPUB at all. Original entry order is otherwise preserved.
    out = Path(out_path)
    ordered = ["mimetype"] + [n for n in book.order if n in new_files and n != "mimetype"]
    ordered += [n for n in new_files if n not in ordered]
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), new_files.get("mimetype", b"application/epub+zip"),
                   compress_type=zipfile.ZIP_STORED)
        for name in ordered:
            if name == "mimetype":
                continue
            z.writestr(name, new_files[name], compress_type=zipfile.ZIP_DEFLATED)

    print()
    if renamed:
        print(f"Renamed        : {renamed[0]} -> {target}")
    print(f"Media type     : {new_media}")
    print(f"Pointers       : properties=\"cover-image\" and <meta name=\"cover\"> both set to id {item['id']!r}")
    for z in fixed_pages:
        print(f"Updated page   : {z}")
    print(f"Written        : {out}")


def _fmt_block(el) -> str:
    local = etree.QName(el).localname.lower()
    cls = el.get("class") or ""
    txt = text_of(el)[:62]
    return f"<{local} class={cls!r}> {txt!r}"


def cmd_outline(args):
    """Show each matching element with its neighbours, in reading order.

    `classes` tells you which styles exist; it cannot tell you whether a given
    occurrence opens a chapter. That is only visible from context - a chapter
    epigraph is followed by the chapter's first paragraph, whereas a song a
    character overhears is followed by more of the same scene. This prints
    exactly the surrounding blocks needed to make that call by eye.
    """
    rx = re.compile(args.class_regex)
    book = Epub(args.epub)
    for it in book.spine_docs():
        if args.doc and args.doc not in it["zip"]:
            continue
        data = book.files.get(it["zip"])
        if data is None:
            continue
        root = parse_xml(data)
        body = find_body(root)
        if body is None:
            continue
        elems = list(body.iter())
        pos_of = {id(e): i for i, e in enumerate(elems)}
        hits = [e for e in elems
                if isinstance(e.tag, str) and rx.search(e.get("class") or "")]
        print(f"\n=== {it['zip']}  ({len(data):,} bytes)  {len(hits)} match(es) for /{args.class_regex}/ ===")
        for h in hits[:args.limit]:
            before = []
            for sib in h.itersiblings(preceding=True):
                if isinstance(sib.tag, str):
                    before.append(sib)
                if len(before) >= args.context:
                    break
            before.reverse()
            after = []
            for sib in h.itersiblings():
                if isinstance(sib.tag, str):
                    after.append(sib)
                if len(after) >= args.context:
                    break
            print(f"\n  --- pos {pos_of[id(h)]} ---")
            for e in before:
                print(f"        {_fmt_block(e)}")
            print(f"    >>> {_fmt_block(h)}")
            for e in after:
                print(f"        {_fmt_block(e)}")
        if len(hits) > args.limit:
            print(f"\n  ... {len(hits) - args.limit} more match(es); raise --limit to see them")


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------

def subtree_size(el) -> int:
    return sum(1 for _ in el.iter())


def split_document(root, body, cut_positions: list[int]) -> list:
    """Split one document into segments at the given positions.

    Works by deep-copying the whole document per segment and pruning what falls
    outside the range, rather than moving nodes into fresh documents. That
    preserves <head> (so stylesheet links survive) and every ancestor wrapper
    (so CSS descendant selectors like .body p still match) without having to
    reason about which wrappers matter.

    The prune test relies on document order: an element's subtree occupies a
    contiguous index range [pos, pos + size), so it survives iff that range
    intersects the segment. Positions are assigned by an identical traversal of
    original and copy, so indices are directly comparable.
    """
    elems = list(body.iter())
    n = len(elems)
    bounds = sorted(set(p for p in cut_positions if 0 < p < n))
    starts = []
    if not bounds or bounds[0] > 1:
        starts.append(1)
    starts.extend(bounds)
    segments = []
    for k, lo in enumerate(starts):
        hi = starts[k + 1] if k + 1 < len(starts) else n
        segments.append((lo, hi))

    out = []
    for lo, hi in segments:
        new_root = copy.deepcopy(root)
        new_body = find_body(new_root)
        new_elems = list(new_body.iter())
        drop = []
        for i, el in enumerate(new_elems):
            if i == 0:
                continue
            size = subtree_size(el)
            keep = (i < hi) and (i + size > lo)
            if not keep:
                drop.append(el)
            elif i < lo:
                # structural ancestor sitting before the range
                el.text = None
                el.tail = None
        for el in drop:
            p = el.getparent()
            if p is not None:
                p.remove(el)
        if lo > 1:
            new_body.text = None
        out.append(new_root)
    return out


def collect_ids(root) -> set[str]:
    ids = set()
    for el in root.iter():
        if isinstance(el.tag, str):
            v = el.get("id")
            if v:
                ids.add(v)
    return ids


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def cmd_apply(args):
    book = Epub(args.epub)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    strip_pl = plan.get("strip_page_list", True) and not args.keep_page_list

    plan_by_file = {d["file"]: d for d in plan.get("documents", [])}

    new_files: dict[str, bytes] = dict(book.files)
    # Three lookup tables drive the link rewriting below. They must be fully
    # populated for EVERY spine document - including ones left alone - before a
    # single href is touched, because a link in an untouched file can still
    # point into a file that moved.
    file_map: dict[str, list[str]] = {}   # oldzip -> the files it became
    origin: dict[str, str] = {}           # newzip -> the file it came from
    # (oldzip, id) -> the new file that id ended up in. This is what lets a
    # footnote reference survive a cut that landed between it and its target.
    # (oldzip, id) -> newzip
    id_map: dict[tuple[str, str], str] = {}
    toc_entries: list[dict] = []   # {level, title, zip, frag}

    for it in book.spine_docs():
        z = it["zip"]
        entry = plan_by_file.get(z)
        cuts = []
        chosen = []
        if entry:
            for c in entry["candidates"]:
                if c.get("split"):
                    cuts.append(int(c["pos"]))
                    chosen.append(c)
        chosen.sort(key=lambda c: c["pos"])

        data = new_files[z]
        root = parse_xml(data)
        body = find_body(root)
        if body is None or not cuts:
            file_map[z] = [z]
            origin[z] = z
            for i in collect_ids(root):
                id_map[(z, i)] = z
            if entry and chosen:
                pass
            continue

        parts = split_document(root, body, cuts)
        leading = min(cuts) > 1
        _pb = find_body(parts[0])
        if leading and _pb is not None and not text_of(_pb).strip() \
                and _pb.find(".//" + XH + "img") is None \
                and _pb.find(".//" + XH + "image") is None:
            parts = parts[1:]
            leading = False

        stem = PurePosixPath(z)
        base = stem.stem
        ext = stem.suffix or ".xhtml"
        folder = str(stem.parent) if str(stem.parent) != "." else ""

        if len(parts) == 1:
            # Nothing actually needs dividing (e.g. one epigraph in a file that is
            # already its own chapter). Leave the original bytes untouched and just
            # attach a ToC entry to it, rather than reconstructing/reserializing it.
            names = [z]
            origin[z] = z
            for i in collect_ids(root):
                id_map[(z, i)] = z
        else:
            names = []
            for idx in range(len(parts)):
                nm = f"{base}_{idx:03d}{ext}"
                nz = f"{folder}/{nm}" if folder else nm
                names.append(nz)
            del new_files[z]
            for idx, (nz, proot) in enumerate(zip(names, parts)):
                new_files[nz] = serialize_xhtml(proot)
                origin[nz] = z
                for i in collect_ids(proot):
                    id_map[(z, i)] = nz
        file_map[z] = names

        # Map each chosen heading to the file it now opens. When content
        # preceded the first cut it became segment 0, so the headings start one
        # index later; `leading` tracks that.
        offset = 1 if leading else 0
        for k, c in enumerate(chosen):
            toc_entries.append({
                "level": int(c.get("level", 1)),
                "title": c.get("title") or c.get("text") or "Untitled",
                "zip": names[k + offset],
                "frag": "",   # the heading now opens its own file
            })

    # documents that were not split still need ids registered
    for it in book.items.values():
        if it["media"] in XHTML_MEDIA and it["zip"] in new_files and it["zip"] not in file_map:
            file_map[it["zip"]] = [it["zip"]]
            origin.setdefault(it["zip"], it["zip"])
            try:
                r = parse_xml(new_files[it["zip"]])
            except Exception:
                continue
            for i in collect_ids(r):
                id_map.setdefault((it["zip"], i), it["zip"])

    # ---- rewrite hyperlinks ------------------------------------------------
    unresolved: list[tuple[str, str]] = []

    def remap(src_zip: str, href: str) -> str:
        if not href or href.startswith(("http:", "https:", "mailto:", "data:")):
            return href
        if href.startswith("#"):
            # A bare "#id" was same-file by definition before the split, so
            # naive rewriting skips it entirely - and that is exactly the bug
            # that silently breaks every footnote in an InDesign export, where
            # notes link to <a href="#_idFootnoteAnchor-3"> in what used to be
            # the same document. Resolve against the ORIGIN file, not the
            # current one, because that is where the id was indexed.
            frag = unquote(href[1:])
            src_origin = origin.get(src_zip, src_zip)
            new = id_map.get((src_origin, frag))
            if new is None:
                if len(file_map.get(src_origin, [])) > 1:
                    unresolved.append((src_zip, href))   # target id does not exist anywhere
                return href
            if new == src_zip:
                return href
            base_dir = str(PurePosixPath(src_zip).parent)
            if base_dir == ".":
                base_dir = ""
            return quote(relpath(new, base_dir), safe="/") + "#" + href[1:]
        target, frag = urldefrag(href)
        base_dir = str(PurePosixPath(src_zip).parent)
        if base_dir == ".":
            base_dir = ""
        old = posix_join(base_dir, target)
        if old not in file_map:
            return href
        parts = file_map[old]
        if len(parts) == 1 and parts[0] == old:
            return href
        new = id_map.get((old, unquote(frag))) if frag else parts[0]
        if new is None:
            unresolved.append((src_zip, href))           # target id does not exist anywhere
            new = parts[0]
        rel = relpath(new, base_dir)
        return quote(rel, safe="/") + (("#" + frag) if frag else "")

    for zname in list(new_files.keys()):
        if not zname.lower().endswith((".xhtml", ".html", ".htm", ".ncx", ".xml")):
            continue
        if zname in ("META-INF/container.xml",) or zname == book.opf_path:
            continue
        try:
            r = parse_xml(new_files[zname])
        except Exception:
            continue
        changed = False
        for el in r.iter():
            if not isinstance(el.tag, str):
                continue
            for attr in ("href", "src", "{%s}href" % NS["xlink"]):
                v = el.get(attr)
                if v:
                    nv = remap(zname, v)
                    if nv != v:
                        el.set(attr, nv)
                        changed = True
        if changed:
            if zname.lower().endswith(".ncx"):
                new_files[zname] = serialize_xml(r)
            else:
                new_files[zname] = serialize_xhtml(r)

    # ---- OPF: manifest + spine --------------------------------------------
    opf = book.opf
    manifest = opf.find(OPF + "manifest")
    spine = opf.find(OPF + "spine")

    id_for_zip: dict[str, str] = {}
    used_ids = set(book.items.keys())

    def mint(prefix: str) -> str:
        i = 1
        while f"{prefix}_{i:03d}" in used_ids:
            i += 1
        nid = f"{prefix}_{i:03d}"
        used_ids.add(nid)
        return nid

    # Replace each split item with its parts at the same manifest position, and
    # carry `properties` across - dropping it would strip the nav or cover-image
    # designation and orphan the book's navigation.
    old_items = {it["zip"]: it for it in book.items.values()}
    for oldzip, parts in file_map.items():
        it = old_items.get(oldzip)
        if it is None or (len(parts) == 1 and parts[0] == oldzip):
            if it is not None:
                id_for_zip[oldzip] = it["id"]
            continue
        el = it["el"]
        anchor = el
        props = it["properties"]
        for idx, nz in enumerate(parts):
            new_el = etree.SubElement(manifest, OPF + "item")
            nid = mint(PurePosixPath(nz).stem.replace(".", "_"))
            new_el.set("id", nid)
            new_el.set("href", quote(relpath(nz, book.opf_dir), safe="/"))
            new_el.set("media-type", it["media"])
            if props:
                new_el.set("properties", props)
            anchor.addprevious(new_el)
            id_for_zip[nz] = nid
        manifest.remove(el)

    # Spine order IS reading order, so the parts must be inserted exactly where
    # the original itemref sat. linear="no" is preserved because it is how a
    # cover is kept out of the linear reading sequence.
    for ir in list(spine.findall(OPF + "itemref")):
        idref = ir.get("idref")
        it = book.items.get(idref)
        if it is None:
            continue
        parts = file_map.get(it["zip"])
        if not parts or (len(parts) == 1 and parts[0] == it["zip"]):
            continue
        for nz in parts:
            new_ir = etree.SubElement(spine, OPF + "itemref")
            new_ir.set("idref", id_for_zip[nz])
            if ir.get("linear"):
                new_ir.set("linear", ir.get("linear"))
            ir.addprevious(new_ir)
        spine.remove(ir)

    # ---- page list removal -------------------------------------------------
    if strip_pl:
        if spine.get("page-map"):
            del spine.attrib["page-map"]
        for it in list(book.items.values()):
            if it["media"] == PAGEMAP_MEDIA:
                new_files.pop(it["zip"], None)
                if it["el"].getparent() is not None:
                    manifest.remove(it["el"])
        ncx_it = book.ncx_item()
        if ncx_it is not None and ncx_it["zip"] in new_files:
            r = parse_xml(new_files[ncx_it["zip"]])
            pl = r.find(NCX + "pageList")
            if pl is not None:
                r.remove(pl)
                new_files[ncx_it["zip"]] = serialize_xml(r)
        nav_it = book.nav_item()
        if nav_it is not None and nav_it["zip"] in new_files:
            r = parse_xml(new_files[nav_it["zip"]])
            for n in list(r.iter(XH + "nav")):
                if (n.get(EPUB + "type") or "") == "page-list":
                    n.getparent().remove(n)
            new_files[nav_it["zip"]] = serialize_xhtml(r)

    # ---- carry over ToC entries whose target survived intact ---------------
    carried: list[dict] = []
    if not args.rebuild_toc_only:
        spine_pos: dict[str, int] = {}
        seq = 0
        for idref in book.spine:
            it = book.items.get(idref)
            if it is None:
                continue
            for nz in file_map.get(it["zip"], [it["zip"]]):
                if nz not in spine_pos:
                    spine_pos[nz] = seq
                    seq += 1

        kept = []
        for e in existing_toc(book):
            target, frag = urldefrag(e["href"])
            if not target:
                continue
            src = book.nav_item() or book.ncx_item()
            base_dir = str(PurePosixPath(src["zip"]).parent) if src else ""
            if base_dir == ".":
                base_dir = ""
            z = posix_join(base_dir, target)
            parts = file_map.get(z)
            if parts and len(parts) == 1 and parts[0] == z and e["title"].strip():
                kept.append({"level": 1, "title": e["title"].strip(),
                             "zip": z, "frag": unquote(frag)})
        # A file that also received a freshly generated entry (e.g. a chapter file
        # that already carried its own <h1> but got a title assigned from its
        # epigraph) should show the new specific title, not the old generic one.
        generated_zips = {e["zip"] for e in toc_entries}
        kept = [e for e in kept if e["zip"] not in generated_zips]
        # Otherwise a book that already had one file per chapter but a single
        # collapsed "Whole Novel" entry would end up listing both that stale
        # entry and the new per-chapter one for the same file.
        carried = kept
        if kept:
            merged = kept + toc_entries
            merged.sort(key=lambda e: (spine_pos.get(e["zip"], 1 << 30),
                                       0 if e in kept else 1))
            toc_entries = merged

    # ---- optional sequential titles ---------------------------------------
    # An explicit --title-format wins; otherwise honour the one `scan` recorded
    # after finding the book's headings were really its opening prose.
    title_format = args.title_format or plan.get("title_format")
    if title_format:
        # Only entries this run generated get renumbered. Carried-over entries
        # ("Cover", "Epilogue") keep the names the publisher gave them, and are
        # identified by identity rather than by title, since two chapters can
        # legitimately share a label.
        generated = {id(e) for e in toc_entries} - {id(e) for e in carried}
        n = 0
        for e in toc_entries:
            if e.get("frag") or id(e) not in generated:
                continue
            n += 1
            e["title"] = title_format.format(n=n, text=e["title"])

    # ---- rebuild navigation ------------------------------------------------
    if toc_entries:
        nav_it = book.nav_item()
        if nav_it is not None and nav_it["zip"] in new_files:
            rebuild_nav(new_files, nav_it["zip"], toc_entries)
        ncx_it = book.ncx_item()
        if ncx_it is not None and ncx_it["zip"] in new_files:
            rebuild_ncx(new_files, ncx_it["zip"], toc_entries)

    new_files[book.opf_path] = serialize_xml(opf)

    # ---- write -------------------------------------------------------------
    out = Path(args.out)
    ordered = ["mimetype"] + [n for n in book.order if n in new_files and n != "mimetype"]
    ordered += [n for n in new_files if n not in ordered]
    with zipfile.ZipFile(out, "w") as z:
        if "mimetype" in new_files:
            z.writestr(zipfile.ZipInfo("mimetype"), new_files["mimetype"],
                       compress_type=zipfile.ZIP_STORED)
        else:
            z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip",
                       compress_type=zipfile.ZIP_STORED)
        for name in ordered:
            if name == "mimetype":
                continue
            z.writestr(name, new_files[name], compress_type=zipfile.ZIP_DEFLATED)

    spine_zips = {book.items[idref]["zip"] for idref in book.spine if idref in book.items}
    print(f"Spine documents: {len(book.spine)} -> "
          f"{sum(len(v) for z, v in file_map.items() if v and z in spine_zips)}")
    print(f"ToC entries    : {len(toc_entries)}")
    print(f"Page list      : {'stripped' if strip_pl else 'left alone'}")
    if unresolved:
        print(f"\nWARNING: {len(unresolved)} link(s) point at an id that does not exist anywhere in the book.")
        print("These were already broken before splitting. They now aim at the first segment of the target file.")
        for src, href in unresolved[:20]:
            print(f"   {src} -> {href}")
        if len(unresolved) > 20:
            print(f"   ... and {len(unresolved) - 20} more")
    print(f"Written        : {out}")


# --------------------------------------------------------------------------
# producer recipes
# --------------------------------------------------------------------------
#
# Books from one imprint are built by one toolchain and share a stylesheet, so
# the marker that worked for the last Gollancz title works for the next. What
# is worth storing is not the automatic answer - that is recomputed in a second
# - but a HUMAN-CORRECTED one, so a judgement made once is not made again.

def stylesheet_classes(book: Epub) -> set[str]:
    """Every class name the book's stylesheets define."""
    out: set[str] = set()
    for it in book.items.values():
        if "css" not in it["media"]:
            continue
        data = book.files.get(it["zip"])
        if data:
            out |= set(re.findall(r"\.([A-Za-z_][\w\-]*)",
                                  data.decode("utf-8", "replace")))
    return out


def book_fingerprint(book: Epub) -> dict:
    """A description of "books built like this one", for fuzzy matching.

    Deliberately not a hash. Two titles from one imprint are laid out by the
    same template but rarely ship byte-identical stylesheets - the two Gollancz
    books here declare 67 and 62 classes - so equality would never match and a
    stored recipe would never be reused. Their class vocabularies overlap 93%
    while sharing 1% with an InDesign export, which makes set similarity a
    reliable family test where equality is useless.
    """
    gen = ""
    meta = book.opf.find(OPF + "metadata")
    if meta is not None:
        for m in meta.findall(OPF + "meta"):
            if (m.get("name") or "").lower() == "generator":
                gen = (m.get("content") or "").strip()
    classes = sorted(stylesheet_classes(book))
    return {"generator": gen, "classes": classes,
            "id": (gen or "unknown") + "/" +
                  hashlib.sha1("|".join(classes).encode("utf-8")).hexdigest()[:12]}


# Two books count as the same producer above this class-vocabulary overlap.
# Measured: 0.93 between two titles from one imprint, 0.01 across producers.
FAMILY_MATCH = 0.6


def match_recipe(recipes: dict, fp: dict):
    """Best stored recipe for this book's producer, or None."""
    mine = set(fp["classes"])
    if not mine:
        return None, 0.0
    best, best_j = None, 0.0
    for key, entry in recipes.items():
        theirs = set(entry.get("classes", []))
        if not theirs:
            continue
        j = len(mine & theirs) / len(mine | theirs)
        if j > best_j:
            best, best_j = entry, j
    return (best, best_j) if best_j >= FAMILY_MATCH else (None, best_j)


def load_recipes(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def recipe_from_plan(plan: dict) -> dict:
    """Distil a reviewed plan back into the rule that would reproduce it.

    Taking this from the plan rather than from the proposal is the whole point:
    by the time a plan is saved, the classes it enables and disables reflect
    whatever the reader decided, including decisions no heuristic made.
    """
    on: dict[str, int] = {}
    off: dict[str, int] = {}
    for d in plan.get("documents", []):
        for c in d["candidates"]:
            key = f"{c['tag']}.{c['class']}" if c["class"] else c["tag"]
            (on if c.get("split") else off)[key] = \
                (on if c.get("split") else off).get(key, 0) + 1
    return {
        "split_styles": sorted(on),
        "rejected_styles": sorted(k for k in off if k not in on),
        "counts": {"enabled": sum(on.values()), "disabled": sum(off.values())},
        "title_format": plan.get("title_format"),
    }


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
#
# The README's guarantee - "the prose is never modified" - was until now
# checked by opening the result in Calibre and reading it. That is fine for
# three books and useless as a safety net for an automatic mode, so the
# guarantee is made executable here. Every check answers a question you would
# otherwise have to notice by eye, 300 pages in.

def spine_prose(book: Epub) -> str:
    """All body text in spine order, whitespace-normalised.

    Normalising is what makes the comparison meaningful rather than merely
    strict: splitting a file legitimately changes indentation and line breaks,
    and re-serialising turns &mdash; into the character it names. Neither
    changes a word, and neither should be reported as one.
    """
    parts = []
    for it in book.spine_docs():
        data = book.files.get(it["zip"])
        if data is None:
            continue
        body = find_body(parse_xml(data))
        if body is not None:
            parts.append(text_of(body))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def check_links(book: Epub) -> list[str]:
    """Every internal href must resolve to a file, and to an id if it names one."""
    problems = []
    ids_by_file: dict[str, set[str]] = {}
    for it in book.items.values():
        if it["media"] in XHTML_MEDIA and it["zip"] in book.files:
            try:
                ids_by_file[it["zip"]] = collect_ids(parse_xml(book.files[it["zip"]]))
            except Exception:
                ids_by_file[it["zip"]] = set()

    for zname, data in book.files.items():
        if not zname.lower().endswith((".xhtml", ".html", ".htm", ".ncx")):
            continue
        try:
            root = parse_xml(data)
        except Exception:
            problems.append(f"{zname}: does not parse")
            continue
        base = str(PurePosixPath(zname).parent)
        if base == ".":
            base = ""
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for attr in ("href", "src", "{%s}href" % NS["xlink"]):
                v = el.get(attr)
                if not v or v.startswith(("http:", "https:", "mailto:", "data:", "tel:")):
                    continue
                target, frag = urldefrag(v)
                z = posix_join(base, target) if target else zname
                if z not in book.files:
                    problems.append(f"{zname}: {v} -> missing file {z}")
                elif frag and z in ids_by_file and unquote(frag) not in ids_by_file[z]:
                    problems.append(f"{zname}: {v} -> id not found in {z}")
    return problems


def cmd_verify(args):
    fixed = Epub(args.fixed)
    failures: list[str] = []
    checks: list[tuple[str, str]] = []

    # 1. prose preserved
    if args.original:
        original = Epub(args.original)
        before, after = spine_prose(original), spine_prose(fixed)
        if before == after:
            checks.append(("prose unchanged", f"{len(after):,} characters, identical"))
        else:
            failures.append("PROSE CHANGED")
            i = 0
            while i < min(len(before), len(after)) and before[i] == after[i]:
                i += 1
            checks.append(("prose unchanged",
                           f"FAIL - {len(before):,} -> {len(after):,} chars, "
                           f"first difference at {i}:\n"
                           f"        original: {before[i - 40:i + 60]!r}\n"
                           f"        fixed   : {after[i - 40:i + 60]!r}"))

    # 2. links resolve
    problems = check_links(fixed)
    if problems:
        failures.append(f"{len(problems)} unresolved link(s)")
        checks.append(("links resolve", f"FAIL - {len(problems)}"))
        for p in problems[:15]:
            checks.append(("", "  " + p))
        if len(problems) > 15:
            checks.append(("", f"  ... and {len(problems) - 15} more"))
    else:
        checks.append(("links resolve", "every internal href found its target"))

    # 3. table of contents resolves and is not empty
    toc = existing_toc(fixed)
    nav = fixed.nav_item() or fixed.ncx_item()
    base = str(PurePosixPath(nav["zip"]).parent) if nav else ""
    if base == ".":
        base = ""
    bad = [e for e in toc
           if e["href"] and posix_join(base, urldefrag(e["href"])[0]) not in fixed.files]
    dupes = len(toc) - len({(e["title"], e["href"]) for e in toc})
    if not toc:
        failures.append("empty table of contents")
        checks.append(("toc", "FAIL - no entries"))
    elif bad:
        failures.append(f"{len(bad)} toc entries point nowhere")
        checks.append(("toc", f"FAIL - {len(bad)} of {len(toc)} entries point nowhere"))
    else:
        note = f"{len(toc)} entries, all resolve"
        if dupes:
            # Not fatal, but it is what a half-finished run looks like.
            note += f"  (warning: {dupes} duplicate entr{'y' if dupes == 1 else 'ies'})"
        checks.append(("toc", note))
    untitled = [e for e in toc if e["title"].strip().lower() in ("", "untitled")]
    if untitled:
        failures.append(f"{len(untitled)} untitled toc entries")
        checks.append(("toc titles", f"FAIL - {len(untitled)} entries have no usable title"))

    # 4. spine integrity
    seen: dict[str, int] = {}
    for idref in fixed.spine:
        it = fixed.items.get(idref)
        if it is None:
            failures.append(f"spine references unknown item {idref}")
            continue
        seen[it["zip"]] = seen.get(it["zip"], 0) + 1
    repeated = [z for z, n in seen.items() if n > 1]
    missing = [z for z in seen if z not in fixed.files]
    if repeated or missing:
        failures.append("spine is inconsistent")
        checks.append(("spine", f"FAIL - {len(repeated)} repeated, {len(missing)} missing"))
    else:
        checks.append(("spine", f"{len(fixed.spine)} documents, each present exactly once"))

    # 5. the page map that started all this
    pl = find_page_list(fixed)
    live = pl["nav_page_list"] or pl["ncx_page_list"] or pl["page_map_file"]
    if live and not args.keep_page_list:
        failures.append("a print page map survived")
        checks.append(("page map", f"FAIL - nav={pl['nav_page_list']} "
                                   f"ncx={pl['ncx_page_list']} file={pl['page_map_file']}"))
    else:
        checks.append(("page map", "gone" if not live else "present (allowed)"))

    # 6. the archive is still an EPUB
    with zipfile.ZipFile(args.fixed) as z:
        names = z.namelist()
        first = names[0] if names else ""
        info = z.getinfo("mimetype") if "mimetype" in names else None
    if first != "mimetype" or info is None or info.compress_type != zipfile.ZIP_STORED:
        failures.append("mimetype entry is not first and stored")
        checks.append(("archive", "FAIL - mimetype must be the first entry, uncompressed"))
    else:
        checks.append(("archive", "mimetype first and stored"))

    width = max(len(k) for k, _ in checks if k) if checks else 10
    for k, v in checks:
        print(f"{k:<{width}}  {v}" if k else v)
    print()
    if failures:
        print("FAILED: " + "; ".join(failures))
        raise SystemExit(1)
    print("OK - all checks passed.")


def rebuild_nav(files: dict, nav_zip: str, entries: list[dict]):
    root = parse_xml(files[nav_zip])
    base_dir = str(PurePosixPath(nav_zip).parent)
    if base_dir == ".":
        base_dir = ""
    target = None
    for n in root.iter(XH + "nav"):
        if (n.get(EPUB + "type") or "") == "toc":
            target = n
            break
    if target is None:
        body = find_body(root)
        target = etree.SubElement(body, XH + "nav")
        target.set(EPUB + "type", "toc")
    for child in list(target):
        target.remove(child)
    target.text = None
    h = etree.SubElement(target, XH + "h1")
    h.text = "Contents"

    def build(parent_el, idx, level):
        ol = etree.SubElement(parent_el, XH + "ol")
        while idx < len(entries):
            e = entries[idx]
            if e["level"] < level:
                break
            if e["level"] > level:
                if len(ol) == 0:          # nothing to nest under yet; treat it as this level
                    e = dict(e, level=level)
                    entries[idx] = e
                    continue
                idx = build(ol[-1], idx, level + 1)
                continue
            li = etree.SubElement(ol, XH + "li")
            a = etree.SubElement(li, XH + "a")
            href = quote(relpath(e["zip"], base_dir), safe="/")
            if e.get("frag"):
                href += "#" + e["frag"]
            a.set("href", href)
            a.text = e["title"]
            idx += 1
        return idx

    build(target, 0, min(e["level"] for e in entries))
    files[nav_zip] = serialize_xhtml(root)


def rebuild_ncx(files: dict, ncx_zip: str, entries: list[dict]):
    root = parse_xml(files[ncx_zip])
    base_dir = str(PurePosixPath(ncx_zip).parent)
    if base_dir == ".":
        base_dir = ""
    navmap = root.find(NCX + "navMap")
    if navmap is None:
        navmap = etree.SubElement(root, NCX + "navMap")
    for c in list(navmap):
        navmap.remove(c)

    counter = [0]

    def make(parent_el, e):
        counter[0] += 1
        np = etree.SubElement(parent_el, NCX + "navPoint")
        np.set("id", f"navpoint-{counter[0]}")
        np.set("playOrder", str(counter[0]))
        lbl = etree.SubElement(np, NCX + "navLabel")
        t = etree.SubElement(lbl, NCX + "text")
        t.text = e["title"]
        c = etree.SubElement(np, NCX + "content")
        src = quote(relpath(e["zip"], base_dir), safe="/")
        if e.get("frag"):
            src += "#" + e["frag"]
        c.set("src", src)
        return np

    def build(parent_el, idx, level):
        while idx < len(entries):
            e = entries[idx]
            if e["level"] < level:
                break
            if e["level"] > level:
                prev = parent_el.findall(NCX + "navPoint")
                if not prev:
                    e = dict(e, level=level)
                    entries[idx] = e
                    continue
                idx = build(prev[-1], idx, level + 1)
                continue
            make(parent_el, e)
            idx += 1
        return idx

    build(navmap, 0, min(e["level"] for e in entries))
    files[ncx_zip] = serialize_xml(root)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="inspect the book and write a plan file")
    s.add_argument("epub")
    s.add_argument("-o", "--out", default="plan.json")
    s.add_argument("--select", default=DEFAULT_SELECT, help=f"pipe-separated tag names to treat as chapter headings, or \"\" for none (default: {DEFAULT_SELECT})")
    s.add_argument("--class-regex", default=None, help="also treat elements whose @class matches this regex as headings")
    s.add_argument("--id-regex", default=None, help="also treat elements carrying (or containing) an id matching this regex as headings")
    s.add_argument("--no-tags", action="store_true", help="ignore --select entirely; match only on --class-regex / --id-regex (use instead of --select \"\", which PowerShell discards)")
    s.add_argument("--title-extend", default=None, help="regex of @class values; text of immediately-following sibling blocks matching it is folded into the ToC title")
    s.add_argument("--require-following", default=None, help="mark a candidate split:false unless the very next block's @class matches this regex (e.g. \"^noindent$\" to require a chapter-opening paragraph)")
    s.add_argument("--include-preceding", default=None, help="regex of @class values; blocks immediately BEFORE a split point that match are pulled into its file (e.g. part epigraphs)")
    s.add_argument("--include-preceding-into", default=None, help="only do --include-preceding for split points whose own @class matches this regex")
    s.add_argument("--title-sep", default=" - ", help="separator used by --title-extend (default: ' - ')")
    s.add_argument("--auto", action="store_true", help="discover the chapter marker automatically and write a plan using it")
    s.add_argument("--propose", action="store_true", help="print ranked marker proposals with their equivalent command lines")
    s.add_argument("--recipes", default=None, help="json file of saved per-producer recipes; a match overrides the automatic choice")
    s.add_argument("--save-recipe", default=None, help="after writing the plan, record the rule it represents in this json file")
    s.add_argument("--from-toc", action="store_true", help="build the plan from the book's own navigation anchors instead of detecting headings")
    s.add_argument("--verbose", action="store_true", help="list every candidate, not just the ones worth reviewing")
    s.add_argument("--show-above", type=float, default=0.95, help="hide candidates at or above this confidence (default: 0.95)")
    s.add_argument("--threshold", type=float, default=0.5, help="confidence at or above which a candidate is written split:true (default: 0.5)")
    s.set_defaults(func=cmd_scan)

    cv = sub.add_parser("cover", help="report on, or replace, the book's cover image")
    cv.add_argument("epub")
    cv.add_argument("--image", required=True, help="replacement image (jpg/png/gif)")
    cv.add_argument("-o", "--out", default=None, help="output epub; omit to only report what would change")
    cv.set_defaults(func=lambda a: cmd_cover(a.epub, a.image, a.out))

    o = sub.add_parser("outline", help="show matching blocks with their neighbours, to judge whether they open a chapter")
    o.add_argument("epub")
    o.add_argument("--class-regex", required=True, help="regex matched against @class")
    o.add_argument("--context", type=int, default=2, help="sibling blocks to show either side (default: 2)")
    o.add_argument("--doc", default=None, help="only look at spine documents whose path contains this substring")
    o.add_argument("--limit", type=int, default=40, help="max matches to print per document (default: 40)")
    o.set_defaults(func=cmd_outline)

    c = sub.add_parser("classes", help="list the paragraph styles in use, to find the heading selector")
    c.add_argument("epub")
    c.add_argument("--max-count", type=int, default=400, help="hide styles used more than this many times (default: 400)")
    c.add_argument("--all", action="store_true", help="show every style regardless of frequency")
    c.add_argument("--id-regex", default=None, help="also list every id matching this regex, e.g. _idParaDest")
    c.add_argument("--show-ids", type=int, default=80, help="how many matching ids to print (default: 80)")
    c.set_defaults(func=cmd_classes)

    a = sub.add_parser("apply", help="split, relink and rebuild navigation")
    a.add_argument("epub")
    a.add_argument("plan")
    a.add_argument("-o", "--out", default="fixed.epub")
    a.add_argument("--rebuild-toc-only", action="store_true", help="discard existing ToC entries instead of carrying over the ones whose target file was not split")
    a.add_argument("--title-format", default=None, help="rename generated entries, e.g. \"Chapter {n}\"; {n} numbers them in reading order, {text} is the detected title")
    a.add_argument("--keep-page-list", action="store_true",
                   help="keep the print page-number map instead of removing it")
    a.set_defaults(func=cmd_apply)

    v = sub.add_parser("verify", help="check a fixed book against the guarantees, and against its original")
    v.add_argument("fixed", help="the epub to check")
    v.add_argument("original", nargs="?", default=None, help="the book it was made from; enables the prose-unchanged check")
    v.add_argument("--keep-page-list", action="store_true", help="do not fail when a print page map is still present")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
