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
    match = build_matcher("" if args.no_tags else args.select, args.class_regex, args.id_regex)
    extend_re = re.compile(args.title_extend, re.I) if args.title_extend else None
    pre_re = re.compile(args.include_preceding, re.I) if args.include_preceding else None
    req_re = re.compile(args.require_following, re.I) if args.require_following else None
    into_re = re.compile(args.include_preceding_into, re.I) if args.include_preceding_into else None
    docs = book.spine_docs()

    plan = {
        "epub": str(Path(args.epub).name),
        "strip_page_list": True,
        "documents": [],
    }

    print(f"OPF          : {book.opf_path}")
    print(f"Spine docs   : {len(docs)}")
    pl = find_page_list(book)
    print(f"Page list    : nav={pl['nav_page_list']} ncx_targets={pl['ncx_page_list']} "
          f"page-map={pl['page_map_file']}")
    toc = existing_toc(book)
    print(f"Existing ToC : {len(toc)} entries")
    for e in toc[:12]:
        print(f"   - {e['title'][:60]!r} -> {e['href']}")
    if len(toc) > 12:
        print(f"   ... and {len(toc)-12} more")
    print()

    total = 0
    for it in docs:
        data = book.files.get(it["zip"])
        if data is None:
            continue
        root = parse_xml(data)
        body = root.find(XH + "body")
        if body is None:
            body = root.find("body")
        if body is None:
            continue
        elems = list(body.iter())
        pos_of = {id(e): i for i, e in enumerate(elems)}
        cands = []
        for i, el in enumerate(elems):
            if i == 0 or not match(el):
                continue
            parent = el.getparent()
            depth = 0
            p = parent
            while p is not None and p is not body:
                depth += 1
                p = p.getparent()
            ok = True
            if req_re is not None:
                nxt = None
                for sib in el.itersiblings():
                    if isinstance(sib.tag, str):
                        nxt = sib
                        break
                ok = nxt is not None and bool(req_re.search(nxt.get("class") or ""))
            start = absorb_start(el, pre_re, into_re, pos_of)
            cands.append({
                "pos": start,
                "heading_pos": i,
                "tag": etree.QName(el).localname.lower(),
                "class": el.get("class") or "",
                "id": el.get("id") or "",
                "depth": depth,
                "text": heading_text(el)[:120],
                "split": ok,
                "level": 1,
                "title": extended_title(el, extend_re, args.title_sep)[:200] or "Untitled",
            })
        on = sum(1 for c in cands if c["split"])
        total += on
        extra = f", {len(cands) - on} marked split:false" if on != len(cands) else ""
        print(f"{it['zip']}  ({len(data):,} bytes)  {len(cands)} candidate(s){extra}")
        for c in cands:
            mark = f"<-{c['heading_pos'] - c['pos']}" if c["heading_pos"] != c["pos"] else "   "
            flag = " " if c["split"] else "-"
            print(f"  {flag} pos={c['pos']:<6}{mark} {c['tag']:<3} depth={c['depth']} class={c['class'][:22]:<22} {c['title'][:70]!r}")
        plan["documents"].append({
            "file": it["zip"],
            "bytes": len(data),
            "candidates": cands,
        })

    print(f"\nTotal split points: {total}   (lines prefixed '-' are in the plan but disabled; flip \"split\" to enable)")
    out = Path(args.out)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Plan written to {out}. Edit it, then run 'apply'.")


def cmd_classes(args):
    """Tally every (tag, class) pair and every matching id, so you can find the heading style."""
    book = Epub(args.epub)
    irx = re.compile(args.id_regex, re.I) if args.id_regex else None

    for it in book.spine_docs():
        data = book.files.get(it["zip"])
        if data is None:
            continue
        root = parse_xml(data)
        body = root.find(XH + "body")
        if body is None:
            body = root.find("body")
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
        body = root.find(XH + "body")
        if body is None:
            body = root.find("body")
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
        new_body = new_root.find(XH + "body")
        if new_body is None:
            new_body = new_root.find("body")
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
        body = root.find(XH + "body")
        if body is None:
            body = root.find("body")
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
        _pb = parts[0].find(XH + "body")
        if _pb is None:
            _pb = parts[0].find("body")
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
            nz = names[k + offset]
            frag = c.get("id") or ""
            toc_entries.append({
                "level": int(c.get("level", 1)),
                "title": c.get("title") or c.get("text") or "Untitled",
                "zip": nz,
                "frag": "",   # heading is now at the top of its own file
            })
            _ = frag

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
        if kept:
            merged = kept + toc_entries
            merged.sort(key=lambda e: (spine_pos.get(e["zip"], 1 << 30),
                                       0 if e in kept else 1))
            toc_entries = merged

    # ---- optional sequential titles ---------------------------------------
    if args.title_format:
        n = 0
        for e in toc_entries:
            if e.get("frag") or e["title"] in {k["title"] for k in locals().get("kept", [])}:
                continue
            n += 1
            e["title"] = args.title_format.format(n=n, text=e["title"])

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
        body = root.find(XH + "body")
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

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
