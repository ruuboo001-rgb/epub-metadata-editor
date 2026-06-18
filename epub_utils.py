import os
import re
import zipfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

ET.register_namespace("", NS["opf"])
ET.register_namespace("dc", NS["dc"])


def norm_zip_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def join_zip_path(base_dir: str, href: str) -> str:
    href = unquote(href)
    if base_dir:
        return norm_zip_path(str(Path(base_dir) / href))
    return norm_zip_path(href)


def is_image_path(path: str) -> bool:
    return path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


class EpubEditor:
    def __init__(self, epub_bytes: bytes):
        self.epub_bytes = epub_bytes
        self.opf_path = ""
        self.opf_dir = ""
        self.root = None
        self.cover_item = None
        self.cover_zip_path = None
        self.cover_page_path = None
        self.file_rows = []
        self.names = []
        self.spine_ids = []
        self.id_to_href = {}
        self._read()

    def _read(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                self.names = zf.namelist()

                if "META-INF/container.xml" not in self.names:
                    raise ValueError("META-INF/container.xml이 없습니다. 올바른 EPUB 파일이 아닐 수 있습니다.")

                self.file_rows = [
                    {"name": info.filename, "size": info.file_size, "compressed": info.compress_size}
                    for info in zf.infolist()
                ]

                container_xml = zf.read("META-INF/container.xml")
                croot = ET.fromstring(container_xml)
                rootfile = croot.find(".//container:rootfile", NS)
                if rootfile is None:
                    raise ValueError("OPF 파일 경로를 찾을 수 없습니다.")

                self.opf_path = norm_zip_path(rootfile.attrib["full-path"])
                self.opf_dir = str(Path(self.opf_path).parent).replace("\\", "/")
                if self.opf_dir == ".":
                    self.opf_dir = ""

                opf_xml = zf.read(self.opf_path)
                self.root = ET.fromstring(opf_xml)

                self.build_manifest_spine_maps()

                self.cover_item = self.find_cover_item()
                if self.cover_item is not None:
                    self.cover_zip_path = join_zip_path(
                        self.opf_dir,
                        self.cover_item.attrib.get("href", "")
                    )

                if not self.cover_zip_path or self.cover_zip_path not in self.names:
                    found = self.find_cover_image_deep()
                    if found:
                        self.cover_zip_path, self.cover_page_path = found
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def metadata_el(self):
        el = self.root.find("opf:metadata", NS)
        if el is None:
            el = ET.SubElement(self.root, f"{{{NS['opf']}}}metadata")
        return el

    def manifest_el(self):
        el = self.root.find("opf:manifest", NS)
        if el is None:
            el = ET.SubElement(self.root, f"{{{NS['opf']}}}manifest")
        return el

    def spine_el(self):
        spine = self.root.find("opf:spine", NS)
        if spine is None:
            spine = ET.SubElement(self.root, f"{{{NS['opf']}}}spine")
        return spine

    def spine_itemrefs(self):
        spine = self.spine_el()
        return spine, spine.findall("opf:itemref", NS)

    def manifest_items(self):
        return self.manifest_el().findall("opf:item", NS)

    def build_manifest_spine_maps(self):
        self.id_to_href = {}
        manifest = self.manifest_el()
        for item in manifest.findall("opf:item", NS):
            item_id = item.attrib.get("id")
            href = item.attrib.get("href")
            if item_id and href:
                self.id_to_href[item_id] = join_zip_path(self.opf_dir, href)

        self.spine_ids = []
        spine = self.root.find("opf:spine", NS)
        if spine is not None:
            for itemref in spine.findall("opf:itemref", NS):
                rid = itemref.attrib.get("idref")
                if rid:
                    self.spine_ids.append(rid)

    def get_dc_all(self, tag: str):
        vals = []
        for el in self.metadata_el().findall(f"dc:{tag}", NS):
            if el.text:
                vals.append(el.text.strip())
        return vals

    def get_dc(self, tag: str) -> str:
        vals = self.get_dc_all(tag)
        return vals[0] if vals else ""

    def set_dc(self, tag: str, value: str):
        metadata = self.metadata_el()
        existing = metadata.findall(f"dc:{tag}", NS)

        for el in existing[1:]:
            metadata.remove(el)

        first = existing[0] if existing else None
        if value and value.strip():
            if first is None:
                first = ET.SubElement(metadata, f"{{{NS['dc']}}}{tag}")
            first.text = value.strip()
        else:
            if first is not None:
                metadata.remove(first)

    def get_info(self):
        return {
            "title": self.get_dc("title"),
            "creator": self.get_dc("creator"),
            "language": self.get_dc("language"),
            "publisher": self.get_dc("publisher"),
            "subject": ", ".join(self.get_dc_all("subject")),
            "identifier": self.get_dc("identifier"),
            "date": self.get_dc("date"),
            "rights": self.get_dc("rights"),
            "description": self.get_dc("description"),
        }

    def set_info(self, info: dict):
        for key in ["title", "creator", "language", "publisher", "identifier", "date", "rights", "description"]:
            self.set_dc(key, info.get(key, ""))

        metadata = self.metadata_el()
        for el in metadata.findall("dc:subject", NS):
            metadata.remove(el)

        for subject in [x.strip() for x in info.get("subject", "").split(",") if x.strip()]:
            el = ET.SubElement(metadata, f"{{{NS['dc']}}}subject")
            el.text = subject

    # ---------- cover ----------
    def find_cover_item(self):
        manifest = self.manifest_el()

        for item in manifest.findall("opf:item", NS):
            props = item.attrib.get("properties", "")
            if "cover-image" in props.split():
                return item

        for meta in self.metadata_el().findall("opf:meta", NS):
            if meta.attrib.get("name") == "cover":
                cover_id = meta.attrib.get("content")
                for item in manifest.findall("opf:item", NS):
                    if item.attrib.get("id") == cover_id:
                        return item

        for item in manifest.findall("opf:item", NS):
            mt = item.attrib.get("media-type", "")
            href = item.attrib.get("href", "").lower()
            if mt.startswith("image/") and "cover" in href:
                return item

        return None

    def html_page_candidates(self):
        candidates = []

        for rid in self.spine_ids[:8]:
            p = self.id_to_href.get(rid)
            if p and p.lower().endswith((".xhtml", ".html", ".htm")):
                candidates.append(p)

        for name in self.names:
            low = name.lower()
            if low.endswith((".xhtml", ".html", ".htm")) and ("cover" in low or "title" in low):
                candidates.append(name)

        for name in self.names:
            low = name.lower()
            if low.endswith((".xhtml", ".html", ".htm")):
                candidates.append(name)

        seen = set()
        out = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def resolve_relative_zip_path(self, page_path: str, href: str) -> str:
        href = unquote(href).replace("\\", "/")
        if href.startswith("#") or href.startswith("http://") or href.startswith("https://"):
            return ""
        href = href.split("#", 1)[0].split("?", 1)[0]
        page_dir = str(Path(page_path).parent).replace("\\", "/")
        if page_dir == ".":
            page_dir = ""
        if page_dir:
            return norm_zip_path(str(Path(page_dir) / href))
        return norm_zip_path(href)

    def extract_image_refs_from_html(self, html: str):
        refs = []
        patterns = [
            r'xlink:href\s*=\s*[\'"]([^\'"]+)[\'"]',
            r'<image\b[^>]*?\shref\s*=\s*[\'"]([^\'"]+)[\'"]',
            r'<img\b[^>]*?\ssrc\s*=\s*[\'"]([^\'"]+)[\'"]',
        ]
        for pat in patterns:
            refs.extend(re.findall(pat, html, flags=re.IGNORECASE))
        return refs

    def find_cover_image_deep(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                names = set(zf.namelist())

                for page in self.html_page_candidates():
                    if page not in names:
                        continue

                    try:
                        html = zf.read(page).decode("utf-8", errors="ignore")
                    except Exception:
                        continue

                    refs = self.extract_image_refs_from_html(html)
                    for ref in refs:
                        img_path = self.resolve_relative_zip_path(page, ref)
                        if img_path and img_path in names and is_image_path(img_path):
                            return img_path, page

                for name in self.names:
                    low = name.lower()
                    if is_image_path(name) and "cover" in low:
                        return name, None

            return None
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def get_cover_bytes(self):
        cover_path = self.cover_zip_path

        if not cover_path:
            found = self.find_cover_image_deep()
            if found:
                cover_path, self.cover_page_path = found

        if not cover_path:
            return None, None

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                if cover_path not in zf.namelist():
                    return None, None
                self.cover_zip_path = cover_path
                return zf.read(cover_path), cover_path
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def normalize_cover_metadata(self):
        manifest = self.manifest_el()
        metadata = self.metadata_el()

        if self.cover_item is None:
            self.cover_item = self.find_cover_item()

        if self.cover_item is None:
            self.cover_item = ET.SubElement(manifest, f"{{{NS['opf']}}}item")

        self.cover_item.set("id", "cover-image")
        self.cover_item.set("href", "Images/cover.jpg")
        self.cover_item.set("media-type", "image/jpeg")
        self.cover_item.set("properties", "cover-image")
        self.cover_zip_path = join_zip_path(self.opf_dir, "Images/cover.jpg")

        for meta in list(metadata.findall("opf:meta", NS)):
            if meta.attrib.get("name") == "cover":
                metadata.remove(meta)

        meta = ET.SubElement(metadata, f"{{{NS['opf']}}}meta")
        meta.set("name", "cover")
        meta.set("content", "cover-image")

    def rewrite_cover_page_html(self, html_bytes: bytes) -> bytes:
        try:
            html = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html = html_bytes.decode("utf-8", errors="ignore")

        html = re.sub(r'(xlink:href\s*=\s*[\'"])[^\'"]+([\'"])', r'\1../Images/cover.jpg\2', html, flags=re.IGNORECASE)
        html = re.sub(r'(<image\b[^>]*?\shref\s*=\s*[\'"])[^\'"]+([\'"])', r'\1../Images/cover.jpg\2', html, flags=re.IGNORECASE)
        html = re.sub(r'(<img\b[^>]*?\ssrc\s*=\s*[\'"])[^\'"]+([\'"])', r'\1../Images/cover.jpg\2', html, flags=re.IGNORECASE)

        return html.encode("utf-8")

    # ---------- TOC ----------
    def xml_escape(self, s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def strip_tags_text(self, html: str) -> str:
        html = re.sub(r"<script\b.*?</script>", "", html, flags=re.I | re.S)
        html = re.sub(r"<style\b.*?</style>", "", html, flags=re.I | re.S)
        html = re.sub(r"<[^>]+>", "", html)
        html = html.replace("&nbsp;", " ")
        html = html.replace("&amp;", "&")
        html = html.replace("&lt;", "<")
        html = html.replace("&gt;", ">")
        html = html.replace("&quot;", '"')
        html = re.sub(r"\s+", " ", html).strip()
        return html

    def get_dc_identifier(self):
        uid_name = self.root.attrib.get("unique-identifier")
        if uid_name:
            for el in self.metadata_el().findall("dc:identifier", NS):
                if el.attrib.get("id") == uid_name and el.text:
                    return el.text.strip()
        ident = self.get_dc("identifier")
        return ident or "epub-id"

    def find_existing_nav_item(self):
        for item in self.manifest_el().findall("opf:item", NS):
            props = item.attrib.get("properties", "")
            href = item.attrib.get("href", "").lower()
            mt = item.attrib.get("media-type", "")
            if "nav" in props.split():
                return item
            if href.endswith(("nav.xhtml", "nav.html")) and (mt == "application/xhtml+xml" or not mt):
                return item
        return None

    def find_existing_ncx_item(self):
        for item in self.manifest_el().findall("opf:item", NS):
            href = item.attrib.get("href", "").lower()
            mt = item.attrib.get("media-type", "")
            item_id = item.attrib.get("id", "").lower()
            if mt == "application/x-dtbncx+xml" or href.endswith(".ncx") or item_id == "ncx":
                return item
        return None

    def get_spine_reading_order(self):
        spine, itemrefs = self.spine_itemrefs()
        id_to_item = {item.attrib.get("id"): item for item in self.manifest_el().findall("opf:item", NS)}
        rows = []

        for itemref in itemrefs:
            idref = itemref.attrib.get("idref")
            if not idref:
                continue

            item = id_to_item.get(idref)
            if item is None:
                continue

            href = item.attrib.get("href", "")
            mt = item.attrib.get("media-type", "")
            props = item.attrib.get("properties", "")
            linear = itemref.attrib.get("linear", "yes")

            if linear == "no":
                continue
            if "nav" in props.split():
                continue

            low = href.lower()
            if not (mt == "application/xhtml+xml" or low.endswith((".xhtml", ".html", ".htm"))):
                continue

            abs_path = join_zip_path(self.opf_dir, href)
            rows.append({"href": href, "abs_path": abs_path, "item": item})

        return rows

    def extract_title_from_xhtml_bytes(self, data: bytes, fallback: str):
        html = data.decode("utf-8", errors="ignore")

        for tag in ["h1", "h2", "h3"]:
            for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, flags=re.I | re.S):
                title = self.strip_tags_text(m.group(1))
                if title:
                    return title

        m = re.search(
            r'<(?:p|div)\b[^>]*class=["\'][^"\']*(?:chapter|episode|title)[^"\']*["\'][^>]*>(.*?)</(?:p|div)>',
            html,
            flags=re.I | re.S
        )
        if m:
            title = self.strip_tags_text(m.group(1))
            if title:
                return title

        m = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        if m:
            title = self.strip_tags_text(m.group(1))
            if title and title.lower() not in ["cover", "table of contents"]:
                return title

        for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", html, flags=re.I | re.S):
            title = self.strip_tags_text(m.group(1))
            if title and len(title) <= 100:
                return title

        return fallback

    def collect_toc_entries(self):
        entries = []

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                names = set(zf.namelist())
                for row in self.get_spine_reading_order():
                    href = row["href"]
                    abs_path = row["abs_path"]
                    low = href.lower()

                    if "cover" in low:
                        continue
                    if "nav" in low or "toc" in low:
                        continue
                    if "titlepage" in low or "title_page" in low:
                        continue

                    if abs_path not in names:
                        continue

                    title = self.extract_title_from_xhtml_bytes(zf.read(abs_path), Path(href).stem)
                    normalized = title.strip().lower()
                    if normalized in ["cover", "table of contents", "contents", "toc", "차례", "목차", "시작"]:
                        continue

                    entries.append({"title": title, "href": href, "source": "spine-h-title"})
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        return entries

    def toc_status(self):
        nav_item = self.find_existing_nav_item()
        ncx_item = self.find_existing_ncx_item()
        spine = self.spine_el()
        entries = self.collect_toc_entries()

        return {
            "has_nav": nav_item is not None,
            "nav_href": nav_item.attrib.get("href", "") if nav_item is not None else "",
            "has_ncx": ncx_item is not None,
            "ncx_href": ncx_item.attrib.get("href", "") if ncx_item is not None else "",
            "spine_toc": spine.attrib.get("toc", ""),
            "entry_count": len(entries),
            "entries": entries,
        }

    def ensure_unique_manifest_id(self, base_id):
        ids = {item.attrib.get("id") for item in self.manifest_el().findall("opf:item", NS)}
        if base_id not in ids:
            return base_id
        n = 2
        while f"{base_id}-{n}" in ids:
            n += 1
        return f"{base_id}-{n}"

    def remove_nav_from_spine(self, nav_id):
        if not nav_id:
            return
        spine = self.spine_el()
        for ref in list(spine.findall("opf:itemref", NS)):
            if ref.attrib.get("idref") == nav_id:
                spine.remove(ref)

    def build_nav_xhtml(self, entries):
        book_title = self.get_dc("title") or "Table of Contents"
        lines = []

        for entry in entries:
            label = self.xml_escape(entry["title"])
            href = self.xml_escape(entry["href"])
            lines.append(f'      <li><a href="{href}">{label}</a></li>')

        if not lines:
            lines.append('      <li><a href="">Start</a></li>')

        body = "\n".join(lines)

        html = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>Table of Contents</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>{self.xml_escape(book_title)}</h1>
    <ol>
{body}
    </ol>
  </nav>
</body>
</html>
"""
        return html.encode("utf-8")

    def build_ncx(self, entries):
        book_title = self.get_dc("title") or "Table of Contents"
        uid = self.get_dc_identifier()

        points = []
        for i, entry in enumerate(entries, start=1):
            label = self.xml_escape(entry["title"])
            href = self.xml_escape(entry["href"])
            points.append(f"""    <navPoint id="navPoint-{i}" playOrder="{i}">
      <navLabel><text>{label}</text></navLabel>
      <content src="{href}"/>
    </navPoint>""")

        if not points:
            points.append("""    <navPoint id="navPoint-1" playOrder="1">
      <navLabel><text>Start</text></navLabel>
      <content src=""/>
    </navPoint>""")

        body = "\n".join(points)

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{self.xml_escape(uid)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{self.xml_escape(book_title)}</text></docTitle>
  <navMap>
{body}
  </navMap>
</ncx>
"""
        return xml.encode("utf-8")

    def normalize_toc_metadata(self):
        entries = self.collect_toc_entries()
        manifest = self.manifest_el()
        spine, itemrefs = self.spine_itemrefs()

        nav_item = self.find_existing_nav_item()
        if nav_item is None:
            nav_item = ET.SubElement(manifest, f"{{{NS['opf']}}}item")
            nav_item.set("id", self.ensure_unique_manifest_id("nav"))
            nav_item.set("href", "nav.xhtml")

        nav_item.set("media-type", "application/xhtml+xml")
        nav_item.set("properties", "nav")

        nav_href = nav_item.attrib.get("href", "nav.xhtml")
        nav_abs = join_zip_path(self.opf_dir, nav_href)

        self.remove_nav_from_spine(nav_item.attrib.get("id"))

        ncx_item = self.find_existing_ncx_item()
        if ncx_item is None:
            ncx_item = ET.SubElement(manifest, f"{{{NS['opf']}}}item")
            ncx_item.set("id", self.ensure_unique_manifest_id("ncx"))
            ncx_item.set("href", "toc.ncx")

        ncx_item.set("media-type", "application/x-dtbncx+xml")
        ncx_href = ncx_item.attrib.get("href", "toc.ncx")
        ncx_abs = join_zip_path(self.opf_dir, ncx_href)

        spine.set("toc", ncx_item.attrib.get("id", "ncx"))

        return {
            "entries": entries,
            "nav_abs": nav_abs,
            "nav_bytes": self.build_nav_xhtml(entries),
            "ncx_abs": ncx_abs,
            "ncx_bytes": self.build_ncx(entries),
        }

    def build_epub(self, info: dict, new_cover_bytes=None, new_cover_filename="cover.jpg") -> bytes:
        self.set_info(info)

        if new_cover_bytes:
            final_cover_bytes = new_cover_bytes
        else:
            final_cover_bytes, _ = self.get_cover_bytes()

        if final_cover_bytes:
            self.normalize_cover_metadata()

        toc_result = self.normalize_toc_metadata()

        opf_bytes = ET.tostring(self.root, encoding="utf-8", xml_declaration=True)

        cover_pages = set()
        if self.cover_page_path:
            cover_pages.add(self.cover_page_path)
        for p in self.html_page_candidates():
            low = p.lower()
            if "cover" in low or "title" in low:
                cover_pages.add(p)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as src:
            src.write(self.epub_bytes)
            src_path = src.name

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as out:
            out_path = out.name

        try:
            with zipfile.ZipFile(src_path, "r") as zin:
                names = zin.namelist()

                with zipfile.ZipFile(out_path, "w") as zout:
                    if "mimetype" in names:
                        zout.writestr("mimetype", zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)

                    for item in zin.infolist():
                        name = item.filename

                        if name == "mimetype":
                            continue
                        if name == self.opf_path:
                            continue

                        if final_cover_bytes and name == self.cover_zip_path:
                            continue

                        if toc_result and name in (toc_result["nav_abs"], toc_result["ncx_abs"]):
                            continue

                        data = zin.read(name)

                        if final_cover_bytes and name in cover_pages:
                            data = self.rewrite_cover_page_html(data)

                        zout.writestr(item, data)

                    zout.writestr(self.opf_path, opf_bytes, compress_type=zipfile.ZIP_DEFLATED)

                    if toc_result:
                        zout.writestr(toc_result["nav_abs"], toc_result["nav_bytes"], compress_type=zipfile.ZIP_DEFLATED)
                        zout.writestr(toc_result["ncx_abs"], toc_result["ncx_bytes"], compress_type=zipfile.ZIP_DEFLATED)

                    if final_cover_bytes:
                        zout.writestr(self.cover_zip_path, final_cover_bytes, compress_type=zipfile.ZIP_DEFLATED)

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in [src_path, out_path]:
                try:
                    os.remove(p)
                except Exception:
                    pass
