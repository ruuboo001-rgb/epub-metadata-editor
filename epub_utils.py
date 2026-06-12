import os
import re
import zipfile
import tempfile
import mimetypes
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


class EpubEditor:
    def __init__(self, epub_bytes: bytes):
        self.epub_bytes = epub_bytes
        self.opf_path = ""
        self.opf_dir = ""
        self.root = None
        self.cover_item = None
        self.cover_zip_path = None
        self.file_rows = []
        self.names = []
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
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed": info.compress_size,
                    }
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

                self.cover_item = self.find_cover_item()
                if self.cover_item is not None:
                    self.cover_zip_path = join_zip_path(
                        self.opf_dir,
                        self.cover_item.attrib.get("href", "")
                    )
                else:
                    self.cover_zip_path = self.find_cover_image_from_cover_page()
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
        for key in [
            "title", "creator", "language", "publisher",
            "identifier", "date", "rights", "description"
        ]:
            self.set_dc(key, info.get(key, ""))

        metadata = self.metadata_el()
        for el in metadata.findall("dc:subject", NS):
            metadata.remove(el)

        subjects = info.get("subject", "")
        for subject in [x.strip() for x in subjects.split(",") if x.strip()]:
            el = ET.SubElement(metadata, f"{{{NS['dc']}}}subject")
            el.text = subject

    def find_cover_item(self):
        manifest = self.manifest_el()

        # EPUB3: properties="cover-image"
        for item in manifest.findall("opf:item", NS):
            props = item.attrib.get("properties", "")
            if "cover-image" in props.split():
                return item

        # EPUB2: <meta name="cover" content="cover-id"/>
        for meta in self.metadata_el().findall("opf:meta", NS):
            if meta.attrib.get("name") == "cover":
                cover_id = meta.attrib.get("content")
                for item in manifest.findall("opf:item", NS):
                    if item.attrib.get("id") == cover_id:
                        return item

        # fallback: filename contains cover
        for item in manifest.findall("opf:item", NS):
            mt = item.attrib.get("media-type", "")
            href = item.attrib.get("href", "").lower()
            if mt.startswith("image/") and "cover" in href:
                return item

        return None

    def guess_cover_page_paths(self):
        candidates = []
        for name in self.names:
            low = name.lower()
            if low.endswith((".xhtml", ".html", ".htm")) and "cover" in low:
                candidates.append(name)
        return candidates

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

    def find_cover_image_from_cover_page(self):
        cover_pages = self.guess_cover_page_paths()
        if not cover_pages:
            return None

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                names = set(zf.namelist())

                for page in cover_pages:
                    try:
                        html = zf.read(page).decode("utf-8", errors="ignore")
                    except Exception:
                        continue

                    patterns = [
                        r'xlink:href\s*=\s*"([^"]+)"',
                        r'<image\b[^>]*?\shref\s*=\s*"([^"]+)"',
                        r'<img\b[^>]*?\ssrc\s*=\s*"([^"]+)"',
                    ]

                    for pat in patterns:
                        m = re.search(pat, html, flags=re.IGNORECASE)
                        if not m:
                            continue

                        img_path = self.resolve_relative_zip_path(page, m.group(1))
                        if img_path and img_path in names:
                            return img_path

            return None
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def get_cover_bytes(self):
        cover_path = self.cover_zip_path

        if not cover_path:
            cover_path = self.find_cover_image_from_cover_page()

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

        # 기존 cover meta 제거 후 하나만 생성
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

        html = re.sub(
            r'(xlink:href\s*=\s*")[^"]+(")',
            r'\1../Images/cover.jpg\2',
            html,
            flags=re.IGNORECASE
        )
        html = re.sub(
            r'(<image\b[^>]*?\shref\s*=\s*")[^"]+(")',
            r'\1../Images/cover.jpg\2',
            html,
            flags=re.IGNORECASE
        )
        html = re.sub(
            r'(<img\b[^>]*?\ssrc\s*=\s*")[^"]+(")',
            r'\1../Images/cover.jpg\2',
            html,
            flags=re.IGNORECASE
        )

        return html.encode("utf-8")

    def build_epub(self, info: dict, new_cover_bytes=None, new_cover_filename="cover.jpg") -> bytes:
        self.set_info(info)

        # 새 표지가 있으면 새 표지를 사용, 없으면 기존 표지를 자동 사용
        if new_cover_bytes:
            final_cover_bytes = new_cover_bytes
        else:
            final_cover_bytes, _ = self.get_cover_bytes()

        if final_cover_bytes:
            self.normalize_cover_metadata()

        opf_bytes = ET.tostring(self.root, encoding="utf-8", xml_declaration=True)
        cover_pages = set(self.guess_cover_page_paths())

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

                        # 표준 표지 경로는 새로 씀
                        if final_cover_bytes and name == self.cover_zip_path:
                            continue

                        data = zin.read(name)

                        # Cover.xhtml 내부 이미지 경로 보정
                        if final_cover_bytes and name in cover_pages:
                            data = self.rewrite_cover_page_html(data)

                        zout.writestr(item, data)

                    zout.writestr(self.opf_path, opf_bytes, compress_type=zipfile.ZIP_DEFLATED)

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
