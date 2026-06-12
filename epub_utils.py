import os
import zipfile
import tempfile
import mimetypes
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from datetime import datetime

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
        self._read()

    def _read(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                names = zf.namelist()
                if "META-INF/container.xml" not in names:
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

    def get_cover_bytes(self):
        if not self.cover_zip_path:
            return None, None

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(self.epub_bytes)
            tmp_path = tmp.name

        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                if self.cover_zip_path not in zf.namelist():
                    return None, None
                return zf.read(self.cover_zip_path), self.cover_zip_path
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def ensure_cover_item(self, filename: str):
        manifest = self.manifest_el()
        metadata = self.metadata_el()

        ext = Path(filename).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"]:
            ext = ".jpg"

        media_type = mimetypes.guess_type("cover" + ext)[0] or "image/jpeg"

        if self.cover_item is not None:
            props = self.cover_item.attrib.get("properties", "")
            if "cover-image" not in props.split():
                self.cover_item.set("properties", (props + " cover-image").strip())
            self.cover_item.set("media-type", media_type)
            return self.cover_item

        ids = {i.attrib.get("id") for i in manifest.findall("opf:item", NS)}
        new_id = "cover-image"
        n = 1
        while new_id in ids:
            n += 1
            new_id = f"cover-image-{n}"

        href = f"Images/cover{ext}"
        item = ET.SubElement(manifest, f"{{{NS['opf']}}}item")
        item.set("id", new_id)
        item.set("href", href)
        item.set("media-type", media_type)
        item.set("properties", "cover-image")

        meta = ET.SubElement(metadata, f"{{{NS['opf']}}}meta")
        meta.set("name", "cover")
        meta.set("content", new_id)

        self.cover_item = item
        self.cover_zip_path = join_zip_path(self.opf_dir, href)
        return item

    def build_epub(self, info: dict, new_cover_bytes=None, new_cover_filename="cover.jpg") -> bytes:
        self.set_info(info)

        cover_target_zip_path = self.cover_zip_path
        if new_cover_bytes:
            item = self.ensure_cover_item(new_cover_filename)
            cover_target_zip_path = join_zip_path(self.opf_dir, item.attrib.get("href", ""))
            self.cover_zip_path = cover_target_zip_path

        opf_bytes = ET.tostring(self.root, encoding="utf-8", xml_declaration=True)

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
                        if new_cover_bytes and name == cover_target_zip_path:
                            continue
                        zout.writestr(item, zin.read(name))

                    zout.writestr(self.opf_path, opf_bytes, compress_type=zipfile.ZIP_DEFLATED)

                    if new_cover_bytes and cover_target_zip_path:
                        zout.writestr(cover_target_zip_path, new_cover_bytes, compress_type=zipfile.ZIP_DEFLATED)

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in [src_path, out_path]:
                try:
                    os.remove(p)
                except Exception:
                    pass
