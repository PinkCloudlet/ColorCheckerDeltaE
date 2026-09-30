#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
colorchecker_app.py - Streamlit GUI do liczenia Delta E76/2000 dla
ColorChecker Classic na podstawie zdjecia TIFF z osadzonym profilem ICC.

Nowosci w tej wersji:
- Automatyczne wykrywanie wzornika (cv2.mcc, ColorChecker Classic).
- Przeciaganie 4 uchwytow rogow MYSZKA na stalym, wygodnym podgladzie
  (nie trzeba juz klikac po kolei ani grzebac w suwakach).
- Architektura pod dodatkowe typy wzornikow (SG, wlasny) w CHART_CONFIGS.

URUCHOMIENIE:
    pip install -r requirements.txt
    python -m streamlit run colorchecker_app.py

WAZNE - biblioteka OpenCV:
    Auto-detekcja wymaga `opencv-contrib-python` (NIE zwyklego
    `opencv-python` - te dwa pakiety sie gryza, nie moga byc
    zainstalowane oba naraz). Jesli masz juz zwykly opencv-python:
        pip uninstall opencv-python opencv-python-headless
        pip install opencv-contrib-python

UWAGA - duze pliki (Phase One 150MP):
    Domyslny limit uploadu w Streamlit to 200MB. Jesli Twoje TIFF-y sa
    wieksze, uruchom z wiekszym limitem:
        python -m streamlit run colorchecker_app.py --server.maxUploadSize 2000
    (wartosc w MB). Podglad na ekranie jest i tak zawsze przeskalowany
    do stalego, wygodnego rozmiaru - wielkosc oryginalu na to nie wplywa.
"""

import io
import math
import re

import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image, ImageCms, ImageDraw

try:
    import cv2
    HAVE_CV2 = True
    HAVE_MCC = hasattr(cv2, "mcc")
except ImportError:
    HAVE_CV2 = False
    HAVE_MCC = False

try:
    import plotly.graph_objects as go
    HAVE_PLOTLY = True
except ImportError:
    HAVE_PLOTLY = False

try:
    from streamlit_image_coordinates import streamlit_image_coordinates
    HAVE_CLICK = True
except ImportError:
    HAVE_CLICK = False


DISPLAY_MAX_DIM = 900  # staly rozmiar podgladu w px, niezalezny od rozdzielczosci oryginalu
HANDLE_COLORS = {"A1": "#22c55e", "A6": "#3b82f6", "D6": "#ef4444", "D1": "#eab308"}
HANDLE_ORDER = ["A1", "A6", "D6", "D1"]  # tl, tr, br, bl

st.set_page_config(page_title="ColorChecker Delta E", layout="wide")


# ---------------------------------------------------------------------------
# Konfiguracja typow wzornikow. Dopisanie kolejnego typu (SG, wlasny preset)
# to docelowo tylko nowy wpis w tym slowniku.
# ---------------------------------------------------------------------------

CHART_CONFIGS = {
    "classic": {
        "label": "ColorChecker Classic (24 pola)",
        "rows": ["A", "B", "C", "D"],
        "cols": list(range(1, 7)),
        # Frakcje srodkow skrajnych pol wzgledem calej karty (z pliku .cht:
        # plansza 330x228, patch A1 srodek (35.25,34.75), A6 (297,34.75),
        # D6 (297,192), D1 (35.25,192))
        "corner_fracs": {
            "A1": (0.10682, 0.15241),
            "A6": (0.90000, 0.15241),
            "D6": (0.90000, 0.84211),
            "D1": (0.10682, 0.84211),
        },
        "cv2_chart_type": "MCC24",
        "auto_detect": True,
        "board_aspect": 330 / 228,
    },
    "sg": {
        "label": "ColorChecker SG (140 pol) — wsparcie w przygotowaniu",
        "rows": [chr(c) for c in range(ord("A"), ord("A") + 14)],
        "cols": list(range(1, 11)),
        "corner_fracs": None,  # TODO: brak jeszcze zmierzonej geometrii .cht dla SG
        "cv2_chart_type": "SG140",
        "auto_detect": False,
        "board_aspect": None,
    },
    "custom": {
        "label": "Wlasny wzornik (recznie zdefiniowana siatka)",
        "rows": None,   # ustawiane dynamicznie w UI
        "cols": None,
        "corner_fracs": None,
        "cv2_chart_type": None,
        "auto_detect": False,
        "board_aspect": None,
    },
}


# ---------------------------------------------------------------------------
# CGATS / referencje / Delta E / ICC
# (logika parsowania i CIEDE2000 zweryfikowana na tescie Sharmy 2005)
# ---------------------------------------------------------------------------

def parse_cgats(file_bytes):
    text = file_bytes.decode("utf-8", errors="replace")
    fmt_match = re.search(r"BEGIN_DATA_FORMAT\s*\n(.*?)\nEND_DATA_FORMAT", text, re.S)
    data_match = re.search(r"BEGIN_DATA\s*\n(.*?)\nEND_DATA", text, re.S)
    if not fmt_match or not data_match:
        raise ValueError("Nie rozpoznaje formatu CGATS w tym pliku.")
    fields = fmt_match.group(1).split()
    rows_raw = [ln for ln in data_match.group(1).splitlines() if ln.strip()]
    token_re = re.compile(r'"[^"]*"|\S+')
    rows = []
    for ln in rows_raw:
        tokens = [t.strip('"') for t in token_re.findall(ln)]
        if len(tokens) != len(fields):
            continue
        rows.append(dict(zip(fields, tokens)))
    return rows


def normalize_patch_id(raw):
    raw = raw.strip().strip('"')
    m = re.match(r"([A-Za-z]+)0*([0-9]+)", raw)
    if not m:
        return raw.upper()
    return f"{m.group(1).upper()}{int(m.group(2))}"


def load_reference_cie(file_bytes):
    rows = parse_cgats(file_bytes)
    out = {}
    for r in rows:
        pid = normalize_patch_id(r["SAMPLE_ID"])
        out[pid] = (float(r["LAB_L"]), float(r["LAB_A"]), float(r["LAB_B"]))
    return out


def xyz_to_lab(X, Y, Z, white=(96.42, 100.0, 82.49)):
    Xn, Yn, Zn = white
    def f(t):
        d = 6.0 / 29.0
        return t ** (1.0 / 3.0) if t > d ** 3 else t / (3 * d * d) + 4.0 / 29.0
    fx, fy, fz = f(X / Xn), f(Y / Yn), f(Z / Zn)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def load_reference_ti3(file_bytes):
    rows = parse_cgats(file_bytes)
    out = {}
    for r in rows:
        pid = normalize_patch_id(r["SAMPLE_LOC"])
        X, Y, Z = float(r["XYZ_X"]), float(r["XYZ_Y"]), float(r["XYZ_Z"])
        out[pid] = xyz_to_lab(X, Y, Z)
    return out


def delta_e_76(lab1, lab2):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(lab1, lab2)))


def delta_e_2000(lab1, lab2, kL=1.0, kC=1.0, kH=1.0):
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    G = 0.5 * (1 - math.sqrt((Cbar ** 7) / (Cbar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    def hp(a, b):
        if a == 0 and b == 0:
            return 0.0
        h = math.degrees(math.atan2(b, a))
        return h + 360 if h < 0 else h

    h1p, h2p = hp(a1p, b1), hp(a2p, b2)
    dLp, dCp = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        if dh > 180: dh -= 360
        elif dh < -180: dh += 360
        dhp = dh
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2)
    Lbarp, Cbarp = (L1 + L2) / 2.0, (C1p + C2p) / 2.0
    if C1p * C2p == 0:
        Hbarp = h1p + h2p
    else:
        if abs(h1p - h2p) > 180:
            Hbarp = (h1p + h2p + 360) / 2 if (h1p + h2p) < 360 else (h1p + h2p - 360) / 2
        else:
            Hbarp = (h1p + h2p) / 2
    T = (1 - 0.17 * math.cos(math.radians(Hbarp - 30))
           + 0.24 * math.cos(math.radians(2 * Hbarp))
           + 0.32 * math.cos(math.radians(3 * Hbarp + 6))
           - 0.20 * math.cos(math.radians(4 * Hbarp - 63)))
    dTheta = 30 * math.exp(-(((Hbarp - 275) / 25) ** 2))
    Rc = 2 * math.sqrt((Cbarp ** 7) / (Cbarp ** 7 + 25 ** 7))
    Sl = 1 + (0.015 * (Lbarp - 50) ** 2) / math.sqrt(20 + (Lbarp - 50) ** 2)
    Sc = 1 + 0.045 * Cbarp
    Sh = 1 + 0.015 * Cbarp * T
    Rt = -math.sin(math.radians(2 * dTheta)) * Rc
    return math.sqrt(
        (dLp / (kL * Sl)) ** 2 + (dCp / (kC * Sc)) ** 2 + (dHp / (kH * Sh)) ** 2
        + Rt * (dCp / (kC * Sc)) * (dHp / (kH * Sh))
    )


def lab_to_srgb_display(L, a, b):
    """Tylko do PODGLADU (kafelki w UI) - Lab(D50) -> XYZ -> Bradford D50->D65
    -> sRGB. Nie jest to kalibrowany podglad, tylko orientacyjny kolor."""
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    def finv(t):
        d = 6.0 / 29.0
        return t ** 3 if t > d else 3 * d * d * (t - 4.0 / 29.0)
    Xn, Yn, Zn = 96.42, 100.0, 82.49
    X, Y, Z = finv(fx) * Xn, finv(fy) * Yn, finv(fz) * Zn
    X, Y, Z = X / 100, Y / 100, Z / 100
    M_bradford = np.array([
        [ 0.9555766, -0.0230393, 0.0631636],
        [-0.0282895,  1.0099416, 0.0210077],
        [ 0.0122982, -0.0204830, 1.3299098],
    ])
    xyz_d65 = M_bradford @ np.array([X, Y, Z])
    M_rgb = np.array([
        [ 3.2404542, -1.5371385, -0.4985314],
        [-0.9692660,  1.8760108,  0.0415560],
        [ 0.0556434, -0.2040259,  1.0572252],
    ])
    rgb = M_rgb @ xyz_d65
    rgb = np.clip(rgb, 0, 1)
    rgb = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * rgb ** (1 / 2.4) - 0.055)
    return tuple((np.clip(rgb, 0, 1) * 255).astype(int))


def grid_uv(rows, cols):
    out = []
    for ri, row in enumerate(rows):
        for ci, col in enumerate(cols):
            out.append((f"{row}{col}", ci / (len(cols) - 1), ri / (len(rows) - 1)))
    return out


def compute_patch_centers(corners, rows, cols):
    """corners = [A1, A6, D6, D1] (tl,tr,br,bl) w PELNEJ rozdzielczosci obrazu."""
    A1, A6, D6, D1 = [np.array(c, dtype=np.float64) for c in corners]
    centers = {}
    if HAVE_CV2:
        src = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
        dst = np.array([A1, A6, D6, D1], dtype=np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        for pid, u, v in grid_uv(rows, cols):
            pt = cv2.perspectiveTransform(np.array([[[u, v]]], dtype=np.float32), H)
            centers[pid] = (float(pt[0, 0, 0]), float(pt[0, 0, 1]))
    else:
        for pid, u, v in grid_uv(rows, cols):
            top = A1 * (1 - u) + A6 * u
            bottom = D1 * (1 - u) + D6 * u
            pt = top * (1 - v) + bottom * v
            centers[pid] = (float(pt[0]), float(pt[1]))
    return centers


# ---------------------------------------------------------------------------
# TIFF + ICC -> Lab
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def build_icc_to_lab_transform(icc_bytes, mode_in):
    src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
    lab_profile = ImageCms.createProfile("LAB")
    return ImageCms.buildTransform(
        src_profile, lab_profile, mode_in, "LAB",
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
    )


def lab_image_to_array(lab_pil_image):
    """UWAGA: np.array(img) na trybie 'LAB' w Pillow zle dekoduje a/b -
    trzeba czytac przez getdata()."""
    w, h = lab_pil_image.size
    arr = np.array(lab_pil_image.getdata(), dtype=np.uint8).reshape(h, w, 3)
    L = arr[..., 0].astype(np.float64) / 255.0 * 100.0
    a = arr[..., 1].astype(np.float64) - 128.0
    b = arr[..., 2].astype(np.float64) - 128.0
    return L, a, b


def sample_patch_lab(pil_rgb_image, transform, cx, cy, half_size):
    left, upper = int(round(cx - half_size)), int(round(cy - half_size))
    right, lower = int(round(cx + half_size)), int(round(cy + half_size))
    crop = pil_rgb_image.crop((left, upper, right, lower))
    if crop.mode != "RGB":
        crop = crop.convert("RGB")
    lab_crop = ImageCms.applyTransform(crop, transform)
    L, a, b = lab_image_to_array(lab_crop)
    return float(np.median(L)), float(np.median(a)), float(np.median(b))


@st.cache_data(show_spinner="Wczytuje TIFF...")
def load_tif(file_bytes):
    img = Image.open(io.BytesIO(file_bytes))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    icc = img.info.get("icc_profile")
    return img, icc


@st.cache_data(show_spinner=False)
def make_display_image(_img, file_key, max_dim=DISPLAY_MAX_DIM):
    """_img nie jest hashowany przez Streamlit (podkreslnik) - do cache
    sluzy zamiast tego file_key (nazwa+rozmiar pliku)."""
    w, h = _img.size
    scale = min(1.0, max_dim / max(w, h))
    disp = _img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR) if scale < 1.0 else _img.copy()
    return disp, scale


# ---------------------------------------------------------------------------
# Order points + auto-detekcja (cv2.mcc)
# ---------------------------------------------------------------------------

def order_points_tl_tr_br_bl(pts):
    pts = np.array(pts, dtype=np.float64)
    rect = np.zeros((4, 2))
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left
    rect[2] = pts[np.argmax(s)]   # bottom-right
    diff = pts[:, 1] - pts[:, 0]  # y - x
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def detect_chart_corners(display_img, chart_key):
    """Probuje wykryc wzornik na obrazie PODGLADU (szybsze), zwraca 4 punkty
    [A1,A6,D6,D1] w KOORDYNATACH TEGO OBRAZU (nie oryginalu), albo None."""
    cfg = CHART_CONFIGS[chart_key]
    if not (HAVE_MCC and cfg["auto_detect"] and cfg["corner_fracs"]):
        return None
    try:
        rgb = np.array(display_img.convert("RGB"))
        bgr = rgb[:, :, ::-1].copy()
        detector = cv2.mcc.CCheckerDetector_create()
        chart_type = getattr(cv2.mcc, cfg["cv2_chart_type"])
        detector.setColorChartType(chart_type)
        ok = detector.process(bgr, 1)
        if not ok:
            return None
        checker = detector.getBestColorChecker()
        box = np.array(checker.getBox(), dtype=np.float64).reshape(-1, 2)
        tl, tr, br, bl = order_points_tl_tr_br_bl(box)
        src = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
        dst = np.array([tl, tr, br, bl], dtype=np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        result = []
        for key in HANDLE_ORDER:
            u, v = cfg["corner_fracs"][key]
            pt = cv2.perspectiveTransform(np.array([[[u, v]]], dtype=np.float32), H)
            result.append((float(pt[0, 0, 0]), float(pt[0, 0, 1])))
        return result
    except Exception:
        return None


def default_corner_guess(w, h):
    """Wysrodkowany prostokat, gdy nie ma jeszcze zadnej detekcji/klikniecia."""
    return [
        (w * 0.15, h * 0.15),
        (w * 0.85, h * 0.15),
        (w * 0.85, h * 0.85),
        (w * 0.15, h * 0.85),
    ]


def draw_full_overlay(disp_img, handle_points_disp, centers_full, scale, half_size_full, active_idx):
    """Jeden wspolny podglad: czerwone ramki probkowania 24 pol + 4 kolorowe
    uchwyty rogow (aktywny wyrozniony wieksza, biala obwodka)."""
    im = disp_img.convert("RGB").copy()
    draw = ImageDraw.Draw(im)
    hs_disp = half_size_full * scale
    for pid, (x, y) in centers_full.items():
        dx, dy = x * scale, y * scale
        draw.rectangle([dx - hs_disp, dy - hs_disp, dx + hs_disp, dy + hs_disp], outline=(255, 40, 40), width=2)
    for i, label in enumerate(HANDLE_ORDER):
        x, y = handle_points_disp[i]
        color = HANDLE_COLORS[label]
        r = 13 if i == active_idx else 8
        if i == active_idx:
            draw.ellipse([x - r - 3, y - r - 3, x + r + 3, y + r + 3], outline=(255, 255, 255), width=3)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(0, 0, 0), width=2)
        draw.text((x + r + 4, y - r - 2), label, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    return im


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("🎨 ColorChecker – Delta E 76 / CIEDE2000")
st.caption(
    "Wczytaj zdjecie TIFF (z osadzonym profilem ICC). Wzornik wykrywa sie "
    "automatycznie, a rogi mozna doszlifowac przeciagajac myszka."
)

with st.sidebar:
    st.header("1. Pliki")
    tif_file = st.file_uploader("Zdjecie TIFF (z ICC)", type=["tif", "tiff"])
    cie_file = st.file_uploader("Referencja producenta (.cie)", type=["cie", "txt"])
    ti3_file = st.file_uploader("Twoj pomiar spektro (.ti3)", type=["ti3"])

    st.header("2. Typ wzornika")
    chart_key = st.selectbox(
        "Wzornik", options=list(CHART_CONFIGS.keys()),
        format_func=lambda k: CHART_CONFIGS[k]["label"], index=0,
    )
    if chart_key == "custom":
        c1, c2 = st.columns(2)
        n_rows = c1.number_input("Liczba wierszy", min_value=1, max_value=30, value=4)
        n_cols = c2.number_input("Liczba kolumn", min_value=1, max_value=30, value=6)
        rows = [chr(ord("A") + i) for i in range(int(n_rows))]
        cols = list(range(1, int(n_cols) + 1))
    else:
        rows = CHART_CONFIGS[chart_key]["rows"]
        cols = CHART_CONFIGS[chart_key]["cols"]

    st.header("3. Probkowanie pol")
    patch_frac = st.slider("Rozmiar probki (% pola)", 0.1, 0.8, 0.4, 0.05)

if not tif_file:
    st.info("⬅️ Zacznij od wgrania zdjecia TIFF w panelu bocznym.")
    st.stop()

if not HAVE_CLICK:
    st.error(
        "Brakuje pakietu `streamlit-image-coordinates`. Zainstaluj: "
        "`pip install streamlit-image-coordinates` i uruchom aplikacje ponownie."
    )
    st.stop()

img, icc_bytes = load_tif(tif_file.getvalue())
if not icc_bytes:
    st.error(
        "W tym pliku TIFF nie ma osadzonego profilu ICC. Sprawdz w Capture One, "
        "czy przy eksporcie zaznaczone jest dolaczenie profilu."
    )
    st.stop()

file_key = f"{tif_file.name}_{tif_file.size}"
disp_img, scale = make_display_image(img, file_key)
disp_w, disp_h = disp_img.size
st.caption(f"Rozdzielczosc oryginalu: {img.size[0]}×{img.size[1]} px  •  podglad {disp_w}×{disp_h}px (stale, wygodne)")

# --- inicjalizacja / reset stanu siatki przy zmianie pliku lub typu wzornika ---
state_key = f"{file_key}_{chart_key}"
if st.session_state.get("chart_state_key") != state_key:
    st.session_state.chart_state_key = state_key
    st.session_state.epoch = 0
    st.session_state.active_idx = 0
    st.session_state.last_click = None
    cfg = CHART_CONFIGS[chart_key]
    detected = None
    detect_status = None
    if cfg["auto_detect"]:
        with st.spinner("Probuje wykryc wzornik automatycznie..."):
            detected = detect_chart_corners(disp_img, chart_key)
        detect_status = "ok" if detected else "fail"
    if detected:
        st.session_state.handle_points_disp = detected  # juz w koordynatach podgladu
    else:
        st.session_state.handle_points_disp = default_corner_guess(disp_w, disp_h)
    st.session_state.detect_status = detect_status

st.subheader("Krok 1 — sprawdz / popraw siatke")
if st.session_state.get("detect_status") == "ok":
    st.success("✅ Wzornik wykryty automatycznie. Kliknij ponizej, jesli chcesz doszlifowac ktorys rog.")
elif st.session_state.get("detect_status") == "fail":
    st.warning("⚠️ Nie udalo sie wykryc automatycznie — kliknij kolejno 4 rogi na zdjeciu ponizej.")
elif not CHART_CONFIGS[chart_key]["auto_detect"]:
    st.info("Ten typ wzornika nie ma jeszcze auto-detekcji — ustaw 4 rogi klikajac na zdjeciu.")

cA, cB = st.columns(2)
if cA.button("🔍 Wykryj ponownie", use_container_width=True, disabled=not CHART_CONFIGS[chart_key]["auto_detect"]):
    with st.spinner("Wykrywam..."):
        detected = detect_chart_corners(disp_img, chart_key)
    if detected:
        st.session_state.handle_points_disp = detected
        st.session_state.detect_status = "ok"
    else:
        st.session_state.detect_status = "fail"
    st.session_state.active_idx = 0
    st.session_state.epoch += 1
    st.rerun()
if cB.button("↺ Resetuj do srodka", use_container_width=True):
    st.session_state.handle_points_disp = default_corner_guess(disp_w, disp_h)
    st.session_state.active_idx = 0
    st.session_state.epoch += 1
    st.rerun()

active_label = st.radio(
    "Ktory rog ustawiasz teraz? (kliknij na zdjeciu, gdzie ma byc — po kliknieciu przeskoczy do nastepnego)",
    HANDLE_ORDER,
    index=st.session_state.active_idx,
    format_func=lambda lbl: {
        "A1": "🟢 A1 (lewy-gorny)", "A6": "🔵 A6 (prawy-gorny)",
        "D6": "🔴 D6 (prawy-dolny)", "D1": "🟡 D1 (lewy-dolny)",
    }[lbl],
    horizontal=True,
    key=f"active_radio_{st.session_state.epoch}",
)
st.session_state.active_idx = HANDLE_ORDER.index(active_label)

corners_full = [(x / scale, y / scale) for x, y in st.session_state.handle_points_disp]
centers_full = compute_patch_centers(corners_full, rows, cols)
xs = [centers_full[f"{rows[0]}{c}"][0] for c in cols]
ys = [centers_full[f"{rows[0]}{c}"][1] for c in cols]
approx_field = math.hypot(xs[1] - xs[0], ys[1] - ys[0]) if len(cols) > 1 else 50.0
half_size_full = max(3.0, approx_field * patch_frac / 2.0)

overlay_img = draw_full_overlay(
    disp_img, st.session_state.handle_points_disp, centers_full, scale,
    half_size_full, st.session_state.active_idx,
)
click = streamlit_image_coordinates(overlay_img, width=disp_w, key=f"clicker_{st.session_state.epoch}")
if click is not None and click != st.session_state.last_click:
    st.session_state.last_click = click
    pts = list(st.session_state.handle_points_disp)
    pts[st.session_state.active_idx] = (click["x"], click["y"])
    st.session_state.handle_points_disp = pts
    st.session_state.active_idx = (st.session_state.active_idx + 1) % 4
    st.rerun()

st.caption("Czerwone ramki = obszar probkowania kazdego pola. Biala obwodka = aktywny rog (ten, ktory przestawi kolejne klikniecie).")

with st.expander("Precyzyjna korekta liczbowa (opcjonalnie)"):
    st.caption("Wspolrzedne w pikselach PODGLADU.")
    edited = []
    for i, label in enumerate(HANDLE_ORDER):
        x0, y0 = st.session_state.handle_points_disp[i]
        c1, c2 = st.columns(2)
        x = c1.number_input(f"{label} – X", value=float(x0), key=f"nx{i}_{st.session_state.epoch}", format="%.1f")
        y = c2.number_input(f"{label} – Y", value=float(y0), key=f"ny{i}_{st.session_state.epoch}", format="%.1f")
        edited.append((x, y))
    if st.button("Zastosuj wartosci liczbowe", use_container_width=True):
        st.session_state.handle_points_disp = edited
        st.session_state.epoch += 1
        st.rerun()

if not (cie_file or ti3_file):
    st.warning("Wgraj przynajmniej jeden plik referencyjny (.cie i/lub .ti3) w panelu bocznym, zeby policzyc Delta E.")
    st.stop()

if st.button("✅ Siatka wyglada dobrze — policz Delta E", type="primary", use_container_width=True):
    with st.spinner("Licze Lab dla kazdego pola i porownuje z referencja..."):
        transform = build_icc_to_lab_transform(icc_bytes, "RGB")
        ref_cie = load_reference_cie(cie_file.getvalue()) if cie_file else {}
        ref_ti3 = load_reference_ti3(ti3_file.getvalue()) if ti3_file else {}

        result_rows = []
        for pid, (x, y) in centers_full.items():
            lab_meas = sample_patch_lab(img, transform, x, y, half_size_full)
            row = {"pole": pid, "L*": lab_meas[0], "a*": lab_meas[1], "b*": lab_meas[2]}
            if pid in ref_cie:
                r = ref_cie[pid]
                row["dE76 (producent)"] = delta_e_76(lab_meas, r)
                row["dE2000 (producent)"] = delta_e_2000(lab_meas, r)
                row["_lab_cie"] = r
            if pid in ref_ti3:
                r = ref_ti3[pid]
                row["dE76 (moj pomiar)"] = delta_e_76(lab_meas, r)
                row["dE2000 (moj pomiar)"] = delta_e_2000(lab_meas, r)
                row["_lab_ti3"] = r
            row["_lab_meas"] = lab_meas
            result_rows.append(row)

        result_rows.sort(key=lambda r: (r["pole"][0], int(r["pole"][1:])))
        st.session_state.results = result_rows
        st.session_state.results_rows_cols = (rows, cols)

if "results" not in st.session_state:
    st.stop()

result_rows = st.session_state.results
res_rows, res_cols = st.session_state.results_rows_cols
df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in result_rows]).set_index("pole")

st.subheader("Wyniki")

de_cols = [c for c in df.columns if c.startswith("dE")]
if de_cols:
    metric_cols = st.columns(len(de_cols))
    for i, c in enumerate(de_cols):
        vals = df[c]
        metric_cols[i].metric(f"{c} – srednia", f"{vals.mean():.2f}", f"max {vals.max():.2f}")

styled = df.style.format(precision=2)
for c in de_cols:
    styled = styled.background_gradient(subset=[c], cmap="RdYlGn_r", vmin=0, vmax=6)
st.dataframe(styled, use_container_width=True)

csv_bytes = df.to_csv().encode("utf-8")
st.download_button("⬇️ Pobierz CSV", csv_bytes, file_name="deltae_wyniki.csv", mime="text/csv")

if HAVE_PLOTLY and de_cols:
    st.subheader("Wykres Delta E per pole")
    fig = go.Figure()
    for c in de_cols:
        fig.add_trace(go.Bar(x=df.index, y=df[c], name=c))
    fig.add_hline(y=2.0, line_dash="dot", line_color="gray",
                  annotation_text="dE ≈ 2 — granica ledwo dostrzegalnej roznicy", annotation_position="top left")
    fig.update_layout(barmode="group", xaxis_title="pole", yaxis_title="Delta E", height=420)
    st.plotly_chart(fig, use_container_width=True)

st.subheader("Podglad kolorow: zmierzony vs referencja")
st.caption(
    "To tylko orientacyjny podglad (Lab→sRGB dla ekranu), nie kalibrowany kolorymetrycznie — "
    "sluzy do szybkiej wizualnej weryfikacji, nie do oceny dokladnosci."
)
ref_choice = None
if any("_lab_cie" in r for r in result_rows):
    ref_choice = "_lab_cie"
if any("_lab_ti3" in r for r in result_rows):
    ref_choice = st.radio(
        "Referencja do podgladu kolorow",
        [k for k in ("_lab_ti3", "_lab_cie") if any(k in r for r in result_rows)],
        format_func=lambda k: "Moj pomiar (.ti3)" if k == "_lab_ti3" else "Producent (.cie)",
        horizontal=True,
    )

if ref_choice:
    n_cols_disp = min(len(res_cols), 6)
    for row_letter in res_rows:
        cols_widgets = st.columns(n_cols_disp)
        for i, colnum in enumerate(res_cols):
            pid = f"{row_letter}{colnum}"
            r = next((r for r in result_rows if r["pole"] == pid), None)
            if r is None or ref_choice not in r:
                continue
            meas_rgb = lab_to_srgb_display(*r["_lab_meas"])
            ref_rgb = lab_to_srgb_display(*r[ref_choice])
            de_key = "dE2000 (moj pomiar)" if ref_choice == "_lab_ti3" else "dE2000 (producent)"
            de_val = r.get(de_key, float("nan"))
            swatch = Image.new("RGB", (100, 50))
            d = ImageDraw.Draw(swatch)
            d.rectangle([0, 0, 49, 49], fill=tuple(int(v) for v in meas_rgb))
            d.rectangle([50, 0, 99, 49], fill=tuple(int(v) for v in ref_rgb))
            with cols_widgets[i % n_cols_disp]:
                st.image(swatch, use_container_width=True)
                st.caption(f"**{pid}**  dE00={de_val:.2f}")