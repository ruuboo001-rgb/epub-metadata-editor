import streamlit as st
import pandas as pd
from pathlib import Path

from epub_utils import EpubEditor

st.set_page_config(
    page_title="EPUB Metadata Editor",
    page_icon="📚",
    layout="wide",
)

st.markdown("""
<style>
.main-title {
    font-size: 2.4rem;
    font-weight: 800;
    margin-bottom: 0.2rem;
}
.sub-title {
    color: #8a7a6a;
    margin-bottom: 1.2rem;
}
.card {
    border: 1px solid #e0d4c8;
    border-radius: 18px;
    padding: 1rem;
    background: #fffaf5;
}
.warning-box {
    border: 1px solid #e6c27a;
    background: #fff8e5;
    border-radius: 14px;
    padding: 0.8rem;
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">📚 EPUB Metadata Editor</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">EPUB 메타데이터와 표지를 웹에서 간단히 수정합니다.</div>', unsafe_allow_html=True)

st.markdown("""
<div class="warning-box">
업로드된 EPUB은 서버에 영구 저장하지 않고, 수정 결과를 다운로드 파일로만 제공합니다.
그래도 개인 파일이나 저작권 파일을 공개 서버에 올릴 때는 주의하세요.
</div>
""", unsafe_allow_html=True)

uploaded = st.file_uploader("EPUB 파일 업로드", type=["epub"])

if not uploaded:
    st.info("EPUB 파일을 업로드하면 메타데이터 편집 화면이 열립니다.")
    st.stop()

epub_bytes = uploaded.getvalue()

try:
    editor = EpubEditor(epub_bytes)
except Exception as e:
    st.error(f"EPUB을 열 수 없습니다: {e}")
    st.stop()

info = editor.get_info()
cover_bytes, cover_path = editor.get_cover_bytes()

left, right = st.columns([1, 2])

with left:
    st.subheader("표지")
    if cover_bytes:
        st.image(cover_bytes, caption=cover_path, use_container_width=True)
        st.download_button(
            "기존 표지 다운로드",
            data=cover_bytes,
            file_name=Path(cover_path).name,
            mime="application/octet-stream",
        )
    else:
        st.info("표지를 찾지 못했습니다.")

    new_cover = st.file_uploader(
        "새 표지 업로드",
        type=["jpg", "jpeg", "png", "webp", "gif", "svg"],
    )

    if new_cover:
        st.image(new_cover.getvalue(), caption="새 표지 미리보기", use_container_width=True)

    st.subheader("파일 정보")
    st.write(f"파일명: `{uploaded.name}`")
    st.write(f"OPF: `{editor.opf_path}`")
    st.write(f"표지 경로: `{editor.cover_zip_path or '감지 안 됨'}`")

with right:
    st.subheader("메타데이터")

    with st.form("metadata_form"):
        title = st.text_input("제목", value=info.get("title", ""))
        creator = st.text_input("작가", value=info.get("creator", ""))
        language = st.text_input("언어", value=info.get("language", "ko"))
        publisher = st.text_input("출판사", value=info.get("publisher", ""))
        subject = st.text_input("태그/장르", value=info.get("subject", ""))
        identifier = st.text_input("식별자/ISBN", value=info.get("identifier", ""))
        date = st.text_input("발행일", value=info.get("date", ""))
        rights = st.text_input("권리/저작권", value=info.get("rights", ""))
        description = st.text_area("설명", value=info.get("description", ""), height=180)

        submitted = st.form_submit_button("수정된 EPUB 만들기")

    if submitted:
        new_info = {
            "title": title,
            "creator": creator,
            "language": language,
            "publisher": publisher,
            "subject": subject,
            "identifier": identifier,
            "date": date,
            "rights": rights,
            "description": description,
        }

        try:
            new_cover_bytes = new_cover.getvalue() if new_cover else None
            new_cover_filename = new_cover.name if new_cover else "cover.jpg"

            result = editor.build_epub(
                new_info,
                new_cover_bytes=new_cover_bytes,
                new_cover_filename=new_cover_filename,
            )

            output_name = Path(uploaded.name).stem + "_edited.epub"

            st.success("수정된 EPUB을 만들었습니다.")
            st.download_button(
                "수정된 EPUB 다운로드",
                data=result,
                file_name=output_name,
                mime="application/epub+zip",
            )
        except Exception as e:
            st.error(f"EPUB 생성 실패: {e}")

st.divider()
st.subheader("EPUB 내부 파일 목록")

df = pd.DataFrame(editor.file_rows)
if not df.empty:
    df["size_kb"] = (df["size"] / 1024).round(1)
    st.dataframe(df[["name", "size_kb"]], use_container_width=True, hide_index=True)
else:
    st.info("파일 목록을 불러오지 못했습니다.")
