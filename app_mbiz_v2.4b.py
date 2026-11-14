import streamlit as st
import asyncio
import re
import os
from pathlib import Path
import pandas as pd
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

st.set_page_config(page_title="Batch Ekstraktor Mbizmarket v2.4b", page_icon="🛒", layout="wide")

# ==========================================
# KONSTANTA
# ==========================================
AUTH_STATE_PATH = Path(".mbiz_auth") / "auth_state.json"
AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ==========================================
# SESSION STATE
# ==========================================
if "hasil_semua" not in st.session_state:
    st.session_state.hasil_semua = None
if "df_ringkasan" not in st.session_state:
    st.session_state.df_ringkasan = None
if "semua_df_timeline" not in st.session_state:
    st.session_state.semua_df_timeline = None
if "login_selesai" not in st.session_state:
    st.session_state.login_selesai = False


# ==========================================
# FORMAT TANGGAL
# ==========================================
def konversi_format_tanggal(tanggal_teks):
    """30 Juli 2026 09:56 WIB → 07/30/2026 09:56"""
    if not tanggal_teks or tanggal_teks == "-":
        return "-"
    kamus_bulan = {
        'januari': '01', 'februari': '02', 'maret': '03', 'april': '04',
        'mei': '05', 'juni': '06', 'juli': '07', 'agustus': '08',
        'september': '09', 'oktober': '10', 'november': '11', 'desember': '12'
    }
    match = re.search(
        r'(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})\s+(\d{1,2}:\d{2})\s+WIB',
        tanggal_teks, re.IGNORECASE
    )
    if match:
        hari = match.group(1).zfill(2)
        bulan_str = match.group(2).lower()
        tahun = match.group(3)
        waktu = match.group(4).zfill(5)
        bulan = kamus_bulan.get(bulan_str, '01')
        return f"{bulan}/{hari}/{tahun} {waktu}"
    return tanggal_teks


# ==========================================
# EKSTRAKSI INFORMASI
# ==========================================
def ekstrak_informasi_umum(lines):
    info = {"Nama Penjual": "-", "Nama Paket": "-", "Kode RUP": "-", "Total Transaksi": "-"}
    for i, teks in enumerate(lines):
        if teks == "Penjual" and info["Nama Penjual"] == "-":
            if i + 1 < len(lines):
                info["Nama Penjual"] = lines[i + 1]
        elif teks == "Nama Paket" and info["Nama Paket"] == "-":
            if i + 1 < len(lines):
                info["Nama Paket"] = lines[i + 1]
        elif teks == "Rencana Umum Pengadaan (RUP)" and info["Kode RUP"] == "-":
            if i + 1 < len(lines):
                info["Kode RUP"] = lines[i + 1]
        elif "* * * Total Transaksi" in teks or teks == "Total Transaksi":
            for j in range(1, 6):
                if i + j < len(lines):
                    match_uang = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})", lines[i + j])
                    if match_uang:
                        info["Total Transaksi"] = "Rp " + match_uang.group(1)
                        break
    return info


def ekstrak_tabel_timeline(lines, nama_paket, url_asal):
    start_index = 0
    for i, line in enumerate(lines):
        if "### Timeline" in line:
            start_index = i + 1
            break

    timeline_lines = lines[start_index:]
    pola_pihak = re.compile(r"^\[(Sistem|Pembeli|Penjual|Penyetuju)\]")
    pola_link_dokumen = re.compile(
        r"\[((?:INV|PO|PO-TR|PR|PR-TR|QN|RFQ)-[\d\-]+)\]\((https?://[^\)]+)\)"
    )
    pola_dokumen_saja = re.compile(r"((?:INV|PO|PO-TR|PR|PR-TR|QN|RFQ)-[\d\-]+)")
    pola_tanggal = re.compile(r"(\d{1,2}\s+[a-zA-Z]+\s+\d{4}\s+\d{1,2}:\d{2}\s+WIB)")

    data_tabel = []
    item_saat_ini = {}
    langkah_pembacaan = 0

    for line in timeline_lines:
        match_pihak = pola_pihak.search(line)
        if match_pihak:
            if item_saat_ini:
                data_tabel.append(item_saat_ini)
            item_saat_ini = {
                "Nama Paket": nama_paket,
                "Pihak": match_pihak.group(1),
                "Tahapan": "",
                "No Dokumen": "-",
                "Link Tahapan": "-",
                "Tanggal Tahapan": "",
                "URL Sumber": url_asal,
            }
            langkah_pembacaan = 1
            continue

        if item_saat_ini:
            if langkah_pembacaan == 1:
                item_saat_ini["Tahapan"] = line
                langkah_pembacaan = 2
            elif langkah_pembacaan == 2:
                if pola_tanggal.search(line):
                    item_saat_ini["Tanggal Tahapan"] = konversi_format_tanggal(line)
                    langkah_pembacaan = 3
            elif langkah_pembacaan == 3:
                match_link = pola_link_dokumen.search(line)
                if match_link:
                    item_saat_ini["No Dokumen"] = match_link.group(1)
                    item_saat_ini["Link Tahapan"] = match_link.group(2)
                else:
                    match_dok = pola_dokumen_saja.search(line)
                    if match_dok:
                        item_saat_ini["No Dokumen"] = match_dok.group(1)

    if item_saat_ini:
        data_tabel.append(item_saat_ini)
    return data_tabel


# ==========================================
# RINGKASAN PR/PO (v2.4b: + Nama Penjual, Total, Link PO)
# ==========================================
def buat_tabel_ringkasan_pr_po(hasil_semua):
    ringkasan = []
    for item in hasil_semua:
        if item["status"] != "Sukses":
            continue
        info = item["info"] or {}
        nama_paket = info.get("Nama Paket", "-")
        nama_penjual = info.get("Nama Penjual", "-")
        total_trx = info.get("Total Transaksi", "-")
        timeline = item["timeline"]

        kode_pr, tgl_pr = "-", "-"
        kode_po, tgl_po, link_po = "-", "-", "-"

        for baris in timeline:
            tahapan = baris["Tahapan"]
            no_dok = baris["No Dokumen"]
            tgl = baris["Tanggal Tahapan"]
            link = baris.get("Link Tahapan", "-")

            if "Membuat Purchase Request" in tahapan and ("PR-" in no_dok or "PR" in no_dok):
                kode_pr = no_dok
                tgl_pr = tgl

            if "Membuat Purchase Order" in tahapan and ("PO-" in no_dok or "PO" in no_dok):
                kode_po = no_dok
                tgl_po = tgl
                link_po = link if link else "-"

        ringkasan.append({
            "Nama Paket": nama_paket,
            "Nama Penjual": nama_penjual,
            "Total Transaksi": total_trx,
            "Kode PR": kode_pr,
            "Tanggal PR": tgl_pr,
            "Kode PO": kode_po,
            "Tanggal PO": tgl_po,
            "Link PO": link_po,
        })
    return pd.DataFrame(ringkasan)


# ==========================================
# HELPER: SIMPAN STORAGE STATE (crawl4ai 0.9.3)
# ==========================================
async def _simpan_storage_state(crawler, path: Path) -> str:
    strat = getattr(crawler, "crawler_strategy", None)
    bm = getattr(strat, "browser_manager", None) if strat else None
    if bm is None:
        raise RuntimeError("BrowserManager tidak ditemukan di crawler_strategy.")

    ctx = None
    jalur = None

    cbc = getattr(bm, "contexts_by_config", None)
    if cbc:
        try:
            ctx = next(iter(cbc.values()))
            jalur = "contexts_by_config"
        except StopIteration:
            pass

    if ctx is None:
        ctx = getattr(bm, "default_context", None)
        if ctx is not None:
            jalur = "default_context"

    if ctx is None and hasattr(bm, "create_browser_context"):
        try:
            ctx = await bm.create_browser_context(bm.config)
            jalur = "create_browser_context (FALLBACK — cookie mungkin tidak terbawa)"
        except Exception:
            pass

    if ctx is None:
        raise RuntimeError("Tidak bisa menemukan BrowserContext aktif.")

    await ctx.storage_state(path=str(path))
    print(f"[storage_state] disimpan via: {jalur} → {path}")
    return jalur


# ==========================================
# FASE 1: LOGIN (VISIBLE, MANUAL) + SIMPAN SESSION
# ==========================================
async def proses_login(target_email, target_password, status_placeholder, waktu_tunggu=45):
    browser_config = BrowserConfig(headless=False, verbose=True)

    js_injeksi_kredensial = f"""
    (async () => {{
        const waitForElement = (selector, timeout = 5000) => {{
            return new Promise((resolve) => {{
                if (document.querySelector(selector)) return resolve(document.querySelector(selector));
                const observer = new MutationObserver(() => {{
                    if (document.querySelector(selector)) {{
                        observer.disconnect();
                        resolve(document.querySelector(selector));
                    }}
                }});
                observer.observe(document.body, {{ childList: true, subtree: true }});
                setTimeout(() => {{ observer.disconnect(); resolve(null); }}, timeout);
            }});
        }};
        const emailInput = await waitForElement('input[type="email"], input[name="email"], input[placeholder*="Email"]');
        if (emailInput) {{
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(emailInput, '{target_email}');
            emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            emailInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
        const passInput = await waitForElement('input[type="password"], input[name="password"]');
        if (passInput) {{
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(passInput, '{target_password}');
            passInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            passInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
    }})();
    """

    async with AsyncWebCrawler(config=browser_config) as crawler:
        session_id = "mbiz_login_session_v2_4b"
        status_placeholder.info("🔄 Membuka browser untuk login...")
        await crawler.arun(
            url="https://www.mbizmarket.co.id/masuk",
            config=CrawlerRunConfig(
                session_id=session_id,
                js_code=js_injeksi_kredensial,
                cache_mode=CacheMode.BYPASS,
                delay_before_return_html=2,
            ),
        )

        for detik in range(waktu_tunggu, 0, -1):
            status_placeholder.warning(
                f"⚠️ **TINDAKAN DIPERLUKAN DI BROWSER**\n\n"
                f"Surel & Sandi sudah terisi otomatis.\n"
                f"Silakan klik **'Masuk'** dan selesaikan verifikasi manual.\n"
                f"Session akan disimpan otomatis dalam: **{detik} detik**"
            )
            await asyncio.sleep(1)

        try:
            jalur = await _simpan_storage_state(crawler, AUTH_STATE_PATH)
            return True, f"✅ Session disimpan via `{jalur}` ke `{AUTH_STATE_PATH}`", jalur
        except Exception as e:
            return False, f"❌ Gagal menyimpan session: {e}", None


# ==========================================
# FASE 2: SCRAPING (HEADLESS + LOAD SESSION)
# ==========================================
async def proses_crawling_batch(target_urls, status_placeholder, headless=True):
    if not AUTH_STATE_PATH.exists():
        raise RuntimeError(
            f"File session `{AUTH_STATE_PATH}` tidak ditemukan. Lakukan login dulu."
        )

    browser_config = BrowserConfig(
        headless=headless,
        verbose=True,
        storage_state=str(AUTH_STATE_PATH),
    )

    hasil_batch = []
    async with AsyncWebCrawler(config=browser_config) as crawler:
        for index, url in enumerate(target_urls, 1):
            status_placeholder.info(
                f"🚀 Memproses URL {index} dari {len(target_urls)} "
                f"({'headless' if headless else 'visible'}):\n{url}"
            )
            timeline_result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, magic=True),
            )
            if timeline_result.success:
                lines = [ln.strip() for ln in timeline_result.markdown.split('\n') if ln.strip()]
                info_umum = ekstrak_informasi_umum(lines)
                data_timeline = ekstrak_tabel_timeline(lines, info_umum["Nama Paket"], url)
                hasil_batch.append({
                    "url": url,
                    "info": info_umum,
                    "timeline": data_timeline,
                    "status": "Sukses",
                })
            else:
                hasil_batch.append({
                    "url": url,
                    "info": None,
                    "timeline": [],
                    "status": "Gagal dimuat",
                })
    return hasil_batch


# ==========================================
# UI STREAMLIT
# ==========================================
st.title("🛒 Ekstraktor Mbizmarket v2.4b")
st.markdown(
    "**Baru di v2.4b:** CSV Ringkasan kini menyertakan **Link PO** (kolom terakhir). "
    "Fitur v2.4 tetap: login manual + simpan session, scraping headless, "
    "kolom **Nama Penjual** & **Total Transaksi**."
)

# ---------- LANGKAH 1: LOGIN ----------
st.subheader("🔐 Langkah 1 — Login & Simpan Session")
col_a, col_b, col_c = st.columns([2, 2, 1])
with col_a:
    email_input = st.text_input("✉️ Email Akun", value="")
with col_b:
    password_input = st.text_input("🔑 Kata Sandi", value="", type="password")
with col_c:
    waktu_input = st.slider("⏳ Jeda Klik Manual (detik)", 15, 90, 45, 5)

btn_login = st.button("🔓 Login & Simpan Session", width='stretch')

if btn_login:
    if not email_input or not password_input:
        st.error("Email dan password wajib diisi.")
    else:
        status_login = st.empty()
        try:
            ok, msg, jalur = asyncio.run(
                proses_login(email_input, password_input, status_login, waktu_input)
            )
            if ok:
                st.session_state.login_selesai = True
                status_login.success(msg)
            else:
                status_login.error(msg)
        except Exception as e:
            st.error(f"Gagal login: {e}")

if AUTH_STATE_PATH.exists():
    mtime = pd.Timestamp(AUTH_STATE_PATH.stat().st_mtime, unit='s', tz='Asia/Jakarta')
    st.caption(f"💾 Session tersimpan: `{AUTH_STATE_PATH}` (modified: {mtime})")

st.divider()

# ---------- LANGKAH 2: SCRAPING ----------
st.subheader("🚀 Langkah 2 — Ekstraksi Massal")
url_input = st.text_area(
    "🔗 Daftar URL Detail Transaksi (satu per baris)",
    placeholder="https://www.mbizmarket.co.id/transaksi/purchase-request/detail/PR-1...",
    height=160,
)
headless_toggle = st.checkbox(
    "🕶️ Jalankan scraping secara **headless** (tanpa jendela browser — untuk server)",
    value=True,
)
btn_scrape = st.button("📥 Mulai Scraping", width='stretch', type="primary")

if btn_scrape:
    daftar_url = [u.strip() for u in url_input.split('\n') if u.strip()]
    if not daftar_url:
        st.error("Daftar URL kosong.")
    elif not AUTH_STATE_PATH.exists():
        st.error("Session belum ada. Lakukan Login (Langkah 1) dulu.")
    else:
        status_placeholder = st.empty()
        try:
            hasil_semua = asyncio.run(
                proses_crawling_batch(daftar_url, status_placeholder, headless=headless_toggle)
            )
            st.session_state.hasil_semua = hasil_semua
            st.session_state.df_ringkasan = buat_tabel_ringkasan_pr_po(hasil_semua)

            semua_list = []
            for h in hasil_semua:
                if h["status"] == "Sukses" and h["timeline"]:
                    semua_list.append(pd.DataFrame(h["timeline"]))
            st.session_state.semua_df_timeline = semua_list

            status_placeholder.success(f"✅ Selesai! Diproses {len(daftar_url)} URL.")
        except Exception as e:
            st.error(f"Kesalahan sistem: {e}")

# ==========================================
# TAMPILKAN HASIL
# ==========================================
if st.session_state.hasil_semua is not None:
    hasil_semua = st.session_state.hasil_semua
    df_ringkasan = st.session_state.df_ringkasan
    semua_df_timeline = st.session_state.semua_df_timeline

    st.subheader("📌 Ringkasan Dokumentasi (PR & PO)")
    if df_ringkasan is not None and not df_ringkasan.empty:
        df_tampil = df_ringkasan.copy()
        df_tampil.index += 1
        st.dataframe(
            df_tampil,
            width='stretch',
            column_config={
                "Link PO": st.column_config.LinkColumn("Link PO", display_text="Buka PO"),
            },
        )
    else:
        st.warning("Data PR/PO tidak ditemukan.")

    st.divider()

    st.subheader("📦 Detail Per Transaksi")
    for i, hasil in enumerate(hasil_semua, 1):
        judul_paket = hasil['info']['Nama Paket'] if hasil['info'] else 'Data Gagal'
        with st.expander(
            f"Hasil {i}: {judul_paket} ({hasil['status']})",
            expanded=(i == 1),
        ):
            if hasil['status'] == "Sukses":
                info_umum = hasil['info']
                st.info(f"""
- **Nama Penjual:** {info_umum['Nama Penjual']}
- **Nama Paket:** {info_umum['Nama Paket']}
- **Kode RUP:** {info_umum['Kode RUP']}
- **Total Transaksi:** {info_umum['Total Transaksi']}
                """)
                df_lokal = pd.DataFrame(hasil['timeline'])
                if not df_lokal.empty:
                    df_lokal.index += 1
                    kolom_tampil = ["Pihak", "Tahapan", "No Dokumen", "Link Tahapan", "Tanggal Tahapan"]
                    st.dataframe(df_lokal[kolom_tampil], width='stretch')
            else:
                st.error(f"Gagal mengekstrak data dari URL: {hasil['url']}")

    st.divider()
    st.subheader("📥 Unduh Hasil")
    col_dl1, col_dl2 = st.columns(2)

    with col_dl1:
        if df_ringkasan is not None and not df_ringkasan.empty:
            st.download_button(
                "📊 Download Ringkasan PR & PO (CSV)",
                df_ringkasan.to_csv(index=False).encode('utf-8'),
                "Ringkasan_PR_PO_Mbizmarket_v24b.csv",
                "text/csv",
                width='stretch',
                key="btn_dl_ringkasan",
            )

    with col_dl2:
        if semua_df_timeline:
            df_gab = pd.concat(semua_df_timeline, ignore_index=True)
            df_gab.index += 1
            st.download_button(
                "📊 Download Timeline Gabungan (CSV)",
                df_gab.to_csv(index=False).encode('utf-8'),
                "Timeline_Mbizmarket_Gabungan_v24b.csv",
                "text/csv",
                width='stretch',
                key="btn_dl_gabungan",
            )