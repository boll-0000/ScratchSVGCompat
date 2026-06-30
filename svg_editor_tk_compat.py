import os
import sys
import shutil
import subprocess
import urllib.request
import platform
import zipfile
import tempfile
import xml.etree.ElementTree as ET
import re
import base64
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

# PyInstaller 同梱用 PATH 設定
try:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
        paths = [
            os.path.join(base, "inkscape"),
            os.path.join(base, "inkscape", "bin"),
            os.path.join(base, "gtk", "bin"),
        ]
        existing = [p for p in paths if os.path.isdir(p)]
        if existing:
            os.environ["PATH"] = (
                os.pathsep.join(existing) + os.pathsep + os.environ.get("PATH", "")
            )
except Exception as e:
    print(f"PATHの設定に失敗しました: {e}")

# --- 画像描画ライブラリのインポート ---
try:
    import cairosvg
    from PIL import Image, ImageTk
    import io
    USE_CAIROSVG = True
except ImportError:
    USE_CAIROSVG = False
    try:
        import tksvg
    except ImportError:
        print("[Error] 描画エンジンが見つかりません。")
        sys.exit(1)

# --- テキストのパス化用ライブラリ (Pure Python モード用) ---
try:
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties, fontManager, findfont
    from matplotlib import ft2font
    from matplotlib.path import Path
    from matplotlib.transforms import Affine2D
    USE_MATPLOTLIB = True
except ImportError:
    USE_MATPLOTLIB = False

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace('', SVG_NS)
ET.register_namespace('xlink', XLINK_NS)

# ==========================================
# 1. Inkscape 自動ダウンロード & パス解決機能
# ==========================================

INKSCAPE_URLS = {
    "Windows": {
        "type": "msi",
        "url": "https://inkscape.org/gallery/item/56340/inkscape-1.4.2_2025-05-13_f4327f4-x64.msi",
    },
}

def get_inkscape_command():
    cmd = shutil.which("inkscape")
    if cmd:
        return cmd
    if platform.system() == "Windows":
        standard_paths = [
            r"C:\Program Files\Inkscape\bin\inkscape.exe",
            r"C:\Program Files\Inkscape\inkscape.exe",
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Inkscape", "bin", "inkscape.exe")
        ]
        for path in standard_paths:
            if os.path.exists(path):
                return path
    local_inkscape = os.path.abspath(os.path.join("inkscape_portable", "inkscape", "bin", "inkscape.exe"))
    if os.path.exists(local_inkscape):
        return local_inkscape
    return None

def download_file_with_progress(url, dest_path, progress_cb=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as f:
        total_size = int(response.info().get('Content-Length', 0))
        downloaded = 0
        chunk_size = 8192
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_cb and total_size > 0:
                progress_cb(downloaded, total_size)

def install_inkscape_msi(msi_path):
    cmd = ["msiexec", "/i", msi_path, "/quiet", "/norestart"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0

# ==========================================
# 2. 画像のBase64埋め込みユーティリティ
# ==========================================

def get_image_base64(src, base_dir):
    try:
        if src.startswith('data:'):
            return src
        if urlparse(src).scheme in ('http', 'https'):
            with urllib.request.urlopen(src) as response:
                content_type = response.info().get_content_type()
                data = response.read()
        else:
            if not os.path.isabs(src):
                src = os.path.join(base_dir, src)
            if not os.path.exists(src):
                return src
            with open(src, 'rb') as f:
                data = f.read()
            ext = os.path.splitext(src)[1].lower()
            content_type = 'image/png' if ext == '.png' else 'image/jpeg' if ext in ('.jpg', '.jpeg') else 'image/svg+xml' if ext == '.svg' else 'image/gif'

        b64_data = base64.b64encode(data).decode('utf-8')
        return f"data:{content_type};base64,{b64_data}"
    except Exception as e:
        print(f"[Warn] Failed to embed image {src}: {e}")
        return src

# ==========================================
# 3. SVG変換ロジック (Python 共通処理)
# ==========================================

def get_tag_name(elem):
    return elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

_UNIT_RE = re.compile(r'^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$')
# CSS ユニット → ユーザー単位への換算係数 (96dpi ベース)
_UNIT_FACTOR = {
    '': 1.0,
    'px': 1.0,
    'pt': 96.0 / 72.0,
    'pc': 16.0,
    'mm': 96.0 / 25.4,
    'cm': 96.0 / 2.54,
    'in': 96.0,
    # em/ex/% は文脈依存なので、ここでは1:1として扱う
    'em': 16.0,
    'ex': 8.0,
    '%': 1.0,
}

def parse_float(value, default=0.0):
    """
    SVG/CSS の長さ値を float として取り出す。
    'px', 'pt', 'mm', 'em', '%' などの単位付きも OK。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        pass
    try:
        m = _UNIT_RE.match(str(value))
        if not m:
            return default
        num = float(m.group(1))
        unit = m.group(2).lower()
        factor = _UNIT_FACTOR.get(unit, 1.0)
        return num * factor
    except Exception:
        return default

def replace_element(parent, old_elem, new_elems):
    idx = list(parent).index(old_elem)
    for offset, new_elem in enumerate(new_elems):
        parent.insert(idx + offset, new_elem)
    parent.remove(old_elem)

def make_path_elem(d, **attrs):
    elem = ET.Element(f"{{{SVG_NS}}}path")
    elem.set('d', d)
    for k, v in attrs.items():
        if v is not None:
            elem.set(k, str(v))
    return elem

def rect_to_path_d(x, y, w, h, rx=0.0, ry=0.0):
    x2 = x + w; y2 = y + h
    rx = max(0.0, min(rx, w / 2.0)); ry = max(0.0, min(ry, h / 2.0))
    if rx == 0 and ry == 0:
        return f"M{x},{y} H{x2} V{y2} H{x} Z"
    return (f"M{x + rx},{y} H{x2 - rx} A{rx},{ry} 0 0 1 {x2},{y + ry} V{y2 - ry} "
            f"A{rx},{ry} 0 0 1 {x2 - rx},{y2} H{x + rx} A{rx},{ry} 0 0 1 {x},{y2 - ry} "
            f"V{y + ry} A{rx},{ry} 0 0 1 {x + rx},{y} Z")

def circle_to_path_d(cx, cy, r):
    # 4分割の弧で閉じる。開始点を 12 時方向に寄せて位相差を抑える。
    return (
        f"M{cx},{cy - r} "
        f"A{r},{r} 0 0 1 {cx + r},{cy} "
        f"A{r},{r} 0 0 1 {cx},{cy + r} "
        f"A{r},{r} 0 0 1 {cx - r},{cy} "
        f"A{r},{r} 0 0 1 {cx},{cy - r} Z"
    )

def ellipse_to_path_d(cx, cy, rx, ry):
    return (
        f"M{cx},{cy - ry} "
        f"A{rx},{ry} 0 0 1 {cx + rx},{cy} "
        f"A{rx},{ry} 0 0 1 {cx},{cy + ry} "
        f"A{rx},{ry} 0 0 1 {cx - rx},{cy} "
        f"A{rx},{ry} 0 0 1 {cx},{cy - ry} Z"
    )

def line_to_path_d(x1, y1, x2, y2):
    return f"M{x1},{y1} L{x2},{y2}"

def points_to_path_d(points, close=False):
    coords = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", points)
    if len(coords) < 4: return None
    pts = [float(v) for v in coords]
    d = [f"M{pts[0]},{pts[1]}"]
    for i in range(2, len(pts), 2):
        if i + 1 < len(pts): d.append(f"L{pts[i]},{pts[i+1]}")
    if close: d.append("Z")
    return " ".join(d)

def build_grid_overlay(x, y, w, h, step=48, color="#8bb5ff", opacity="0.12"):
    elems = []; x_end = x + w; y_end = y + h
    xx = x
    while xx <= x_end + 0.001:
        elems.append(make_path_elem(f"M{xx},{y} V{y_end}", fill="none", stroke=color, **{"stroke-opacity": opacity, "stroke-width": "1"}))
        xx += step
    yy = y
    while yy <= y_end + 0.001:
        elems.append(make_path_elem(f"M{x},{yy} H{x_end}", fill="none", stroke=color, **{"stroke-opacity": opacity, "stroke-width": "1"}))
        yy += step
    return elems

def build_dots_overlay(x, y, w, h, step=28, dot_x=2, dot_y=2, r=1.2, color="#b6d2ff", opacity="0.12"):
    elems = []; x_end = x + w; y_end = y + h; yy = y + dot_y
    while yy <= y_end + 0.001:
        xx = x + dot_x
        while xx <= x_end + 0.001:
            elems.append(make_path_elem(circle_to_path_d(xx, yy, r), fill=color, opacity=opacity))
            xx += step
        yy += step
    return elems

# ==========================================
# 3a. [新規] CSS スタイル解析・要素への展開
# ==========================================
# SVG内の <style> ブロックや style="..." 属性を解析して、
# 要素の "個別属性 (presentation attribute)" として展開する。
# こうしておけば後工程で <style> を消しても、フォント色や太さが残る。

# 継承される / プレゼンテーション属性として SVG に持たせて良いプロパティ
_PRESENTATION_PROPS = {
    'fill', 'fill-opacity', 'fill-rule',
    'stroke', 'stroke-opacity', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
    'stroke-dasharray', 'stroke-dashoffset', 'stroke-miterlimit',
    'opacity', 'color', 'visibility', 'display',
    'font-family', 'font-size', 'font-weight', 'font-style', 'font-variant',
    'font-stretch', 'letter-spacing', 'word-spacing', 'text-anchor', 'dominant-baseline',
    'alignment-baseline', 'text-decoration', 'unicode-bidi', 'direction',
    'clip-rule', 'paint-order', 'shape-rendering', 'text-rendering',
    'vector-effect',
    'filter',  # filter も保持
}

# 「子要素に継承される」プロパティ (CSS 仕様準拠)
# opacity / filter / display / visibility は継承されない
_INHERITED_PROPS = {
    'fill', 'fill-opacity', 'fill-rule',
    'stroke', 'stroke-opacity', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
    'stroke-dasharray', 'stroke-dashoffset', 'stroke-miterlimit',
    'color',
    'font-family', 'font-size', 'font-weight', 'font-style', 'font-variant',
    'letter-spacing', 'word-spacing', 'text-anchor', 'dominant-baseline',
    'alignment-baseline', 'text-decoration',
    'visibility',
}

def _parse_style_string(style_str):
    """ 'fill:red; font-weight:bold' → {'fill':'red', 'font-weight':'bold'} """
    result = {}
    if not style_str:
        return result
    for decl in style_str.split(';'):
        if ':' in decl:
            k, v = decl.split(':', 1)
            k = k.strip()
            v = v.strip()
            if k:
                result[k] = v
    return result

def _selector_specificity(selector):
    """
    CSS specificity を (a, b, c) のタプルで返す:
      a: #id の数
      b: .class の数
      c: タグの数
    タプル比較で評価できる。
    """
    a = len(re.findall(r'#[\w-]+', selector))
    b = len(re.findall(r'\.[\w-]+', selector))
    # タグ名 (先頭の英字部分)
    c_match = re.match(r'^([a-zA-Z][\w-]*)', selector.strip())
    c = 1 if c_match else 0
    # tag.class のようなパターンでも c は1扱い
    return (a, b, c)

def _parse_css_text(css_text):
    """
    SVG <style> 内のCSSを (selector, declarations_dict, specificity, order) のリストとしてパースする。
    対応セレクタ:
      - .className
      - #id
      - tag
      - 複合 (空白/カンマ) は単純対応
    @media や複雑な擬似クラスは無視。
    """
    # コメント除去
    css_text = re.sub(r'/\*.*?\*/', '', css_text, flags=re.DOTALL)
    rules = []
    order_counter = 0
    # ルール抽出 : "selector { decls }" 形式
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css_text):
        selector_part = m.group(1).strip()
        decl_part = m.group(2).strip()
        decls = _parse_style_string(decl_part)
        if not decls:
            continue
        # カンマで複数セレクタを分離
        for sel in selector_part.split(','):
            sel = sel.strip()
            if sel:
                spec = _selector_specificity(sel)
                rules.append((sel, decls, spec, order_counter))
                order_counter += 1
    return rules

def _selector_matches(elem, selector, tag_name):
    """ 単純なセレクタマッチ: .cls / #id / tag / tag.cls """
    selector = selector.strip()
    if not selector:
        return False
    # 例: "rect.box" / ".cls" / "#id" / "text"
    # 空白による子孫セレクタはここでは未対応 (個別マッチで近似)
    # まずシンプル単体トークンに分解
    # tag, .cls, #id 部を抽出
    m = re.match(r'^([a-zA-Z]*)((?:[.#][\w-]+)*)$', selector)
    if not m:
        # 複雑セレクタは諦め
        return False
    sel_tag = m.group(1)
    rest = m.group(2)
    if sel_tag and sel_tag != tag_name and sel_tag != '*':
        return False
    elem_class = elem.get('class', '')
    elem_classes = set(elem_class.split()) if elem_class else set()
    elem_id = elem.get('id', '')
    for token in re.findall(r'[.#][\w-]+', rest):
        if token.startswith('.'):
            if token[1:] not in elem_classes:
                return False
        elif token.startswith('#'):
            if token[1:] != elem_id:
                return False
    return True

def _collect_css_rules(root):
    """ root配下の <style> 要素を全て連結してパース """
    rules = []
    for style_elem in root.iter():
        if get_tag_name(style_elem) == 'style':
            txt = ''.join(style_elem.itertext())
            if txt:
                rules.extend(_parse_css_text(txt))
    # specificity 昇順 → 同じなら登場順 でソート (後勝ち = 優先度高)
    rules.sort(key=lambda r: (r[2], r[3]))
    return rules

def _resolve_special_style_values(style, parent_style=None):
    """ currentColor / inherit をできるだけ実色に解決する。 """
    resolved = dict(style)
    parent_style = parent_style or {}
    inherited_color = parent_style.get('color', 'black')

    # color を先に確定
    color = resolved.get('color')
    if color == 'inherit':
        color = inherited_color
    elif color == 'currentColor':
        color = inherited_color
    if color is not None:
        resolved['color'] = color

    # 色系プロパティに currentColor / inherit が入っている場合を解決
    for prop in ('fill', 'stroke', 'stop-color', 'flood-color', 'lighting-color'):
        v = resolved.get(prop)
        if v == 'inherit':
            if prop in parent_style:
                resolved[prop] = parent_style[prop]
        elif v == 'currentColor':
            resolved[prop] = resolved.get('color', inherited_color)
    return resolved

def _compute_inherited_style(elem, parent_style, css_rules):
    """
    要素の実効スタイルを計算する:
      1. parent_styleのうち「継承されるプロパティ」だけを継承
         (filterやopacityは継承されない = 子に伝えない)
      2. CSSルール (specificity昇順で適用 = 詳細度の高いものが後勝ちで上書き)
      3. style属性 を適用 (CSSルールより常に優先)
      4. 個別 presentation 属性 (fill="..." 等) は最優先として扱う
         （厳密にCSS仕様とは異なるが、作者明示指定の意図を尊重し、
           元コードとも互換を保つ）
    """
    # 継承プロパティのみ取り込む
    style = {k: v for k, v in parent_style.items() if k in _INHERITED_PROPS}
    tag_name = get_tag_name(elem)

    # CSS ルール (specificity 昇順済み)
    for rule in css_rules:
        sel, decls = rule[0], rule[1]
        if _selector_matches(elem, sel, tag_name):
            style.update(decls)

    # style属性 (インライン style)
    style.update(_parse_style_string(elem.get('style', '')))

    # 個別 presentation 属性 (最優先扱い)
    for prop in _PRESENTATION_PROPS:
        v = elem.get(prop)
        if v is not None:
            style[prop] = v

    return _resolve_special_style_values(style, parent_style)

def _apply_style_to_elem(elem, style):
    """ 計算済みstyleを個別属性として要素に書き込む。元の class/style属性は残す。 """
    # 元々個別属性として存在しないものだけ、属性として "焼き込む"
    for k, v in style.items():
        if k not in _PRESENTATION_PROPS:
            continue
        if elem.get(k) is None:
            elem.set(k, v)

def _flatten_styles(root):
    """
    SVG 全体に対して CSS / style属性 を解決して個別属性に焼き込む。
    text要素には子のtspanまで含めて伝播。
    """
    css_rules = _collect_css_rules(root)

    # 継承伝播用の DFS
    def walk(elem, parent_style):
        # SVG では <defs> 内も対象 (gradient等の参照先要素もstyle持つことがあるため)
        style = _compute_inherited_style(elem, parent_style, css_rules)
        _apply_style_to_elem(elem, style)
        for child in elem:
            walk(child, style)

    walk(root, {})

# ==========================================
# 3b. フォント解決
# ==========================================

def _split_font_family_candidates(requested_family):
    candidates = []
    if not requested_family:
        return candidates
    for family in requested_family.replace("'", "").replace('"', '').split(','):
        fam = family.strip()
        if fam:
            candidates.append(fam)
    return candidates

def _build_font_properties(requested_family, weight=None, style=None, stretch=None):
    if not USE_MATPLOTLIB:
        return None

    jp_fonts = [
        "Noto Sans CJK JP", "Noto Sans JP",
        "Hiragino Kaku Gothic Pro", "Hiragino Sans",
        "Yu Gothic", "Yu Gothic UI", "Meiryo",
        "MS Gothic", "MS PGothic",
        "IPAGothic", "IPAexGothic",
    ]

    families = []
    seen = set()
    for fam in _split_font_family_candidates(requested_family):
        if fam not in seen:
            families.append(fam)
            seen.add(fam)
    for fam in jp_fonts + ["sans-serif"]:
        if fam not in seen:
            families.append(fam)
            seen.add(fam)

    fp = FontProperties(
        family=families,
        weight=_normalize_weight(weight),
        style=style or 'normal',
        stretch=stretch or 'normal',
    )
    try:
        resolved = findfont(fp, fallback_to_default=True)
        if resolved and os.path.exists(resolved):
            return FontProperties(
                fname=resolved,
                weight=_normalize_weight(weight),
                style=style or 'normal',
                stretch=stretch or 'normal',
            )
    except Exception:
        pass
    return fp

def _text_advance_width(text, font_props, font_size):
    """Return advance width instead of ink bounding box width."""
    if not USE_MATPLOTLIB:
        return 0.0
    try:
        font_path = None
        if hasattr(font_props, "get_file"):
            try:
                font_path = font_props.get_file()
            except Exception:
                font_path = None
        if not font_path:
            font_path = findfont(font_props, fallback_to_default=True)
        if not font_path or not os.path.exists(font_path):
            return 0.0
        font = ft2font.FT2Font(font_path)
        font.set_size(font_size, 72)
        font.set_text(text, 0.0)
        width, _height = font.get_width_height()
        if width > 0:
            return width / 64.0
    except Exception:
        pass
    try:
        tp = TextPath((0, 0), text, size=font_size, prop=font_props)
        return float(tp.get_extents().width)
    except Exception:
        return 0.0

def get_safe_font_family(requested_family, weight=None):
    if not USE_MATPLOTLIB:
        return requested_family
    candidates = _split_font_family_candidates(requested_family)
    available_fonts = {f.name for f in fontManager.ttflist}
    for family in candidates:
        if family in available_fonts:
            return family
    for jf in [
        "Noto Sans CJK JP", "Noto Sans JP",
        "Hiragino Kaku Gothic Pro", "Hiragino Sans",
        "Yu Gothic", "Yu Gothic UI", "Meiryo",
        "MS Gothic", "MS PGothic",
        "IPAGothic", "IPAexGothic",
    ]:
        if jf in available_fonts:
            return jf
    return "sans-serif"

def _normalize_weight(weight_str):
    """ font-weight を matplotlib に渡せる形に変換 """
    if not weight_str:
        return 'normal'
    weight_str = str(weight_str).strip().lower()
    if weight_str in ('normal', 'bold', 'lighter', 'bolder'):
        return weight_str
    try:
        w = int(weight_str)
        # matplotlib は 100,200,...,900 を受け付ける
        return w
    except ValueError:
        return 'normal'

# ==========================================
# 3c. text → path 変換
# ==========================================

def _get_effective_attr(elem, name, default=None):
    """ 個別属性 → style属性 の順で取得 """
    v = elem.get(name)
    if v is not None:
        return v
    sty = _parse_style_string(elem.get('style', ''))
    if name in sty:
        return sty[name]
    return default

def _text_path_for_run(text_run, x, y, font_size, font_family, weight):
    """ 1run分のテキストを path d文字列+幅 で返す """
    prop = _build_font_properties(font_family, weight=weight)
    tp = TextPath((0, 0), text_run, size=font_size, prop=prop)
    bbox = tp.get_extents()

    transform = Affine2D().scale(1, -1).translate(x, y)
    d_parts = []
    for vertices, code in tp.iter_segments():
        v = transform.transform(vertices.reshape(-1, 2)).flatten()
        if code == Path.MOVETO:
            d_parts.append(f"M {v[0]:.3f},{v[1]:.3f}")
        elif code == Path.LINETO:
            d_parts.append(f"L {v[0]:.3f},{v[1]:.3f}")
        elif code == Path.CURVE3:
            d_parts.append(f"Q {v[0]:.3f},{v[1]:.3f} {v[2]:.3f},{v[3]:.3f}")
        elif code == Path.CURVE4:
            d_parts.append(f"C {v[0]:.3f},{v[1]:.3f} {v[2]:.3f},{v[3]:.3f} {v[4]:.3f},{v[5]:.3f}")
        elif code == Path.CLOSEPOLY:
            d_parts.append("Z")
    return " ".join(d_parts), bbox

def _collect_runs(text_elem, parent_attrs):
    """
    <text> / <tspan> を「ラン」のリストに分解する。
    各ランは {text, x, y, font_size, font_family, font_weight, fill, ...} を持つ。
    座標未指定の tspan は前ランの右端に連結。
    """
    runs = []

    def attrs_of(elem, base):
        a = dict(base)
        for k in ('x', 'y', 'dx', 'dy',
                  'font-size', 'font-family', 'font-weight',
                  'font-style', 'fill', 'fill-opacity', 'opacity',
                  'text-anchor', 'dominant-baseline',
                  'letter-spacing'):
            v = _get_effective_attr(elem, k)
            if v is not None:
                a[k] = v
        return a

    text_attrs = attrs_of(text_elem, parent_attrs)

    # text 自体に直接テキストがあるか?
    direct_text = (text_elem.text or '')
    if direct_text.strip():
        runs.append({
            'text': direct_text,
            'attrs': text_attrs,
            'explicit_xy': ('x' in text_elem.attrib, 'y' in text_elem.attrib),
        })

    for child in text_elem:
        if get_tag_name(child) == 'tspan':
            tsp_attrs = attrs_of(child, text_attrs)
            tsp_text = (child.text or '')
            if tsp_text:
                runs.append({
                    'text': tsp_text,
                    'attrs': tsp_attrs,
                    'explicit_xy': ('x' in child.attrib, 'y' in child.attrib),
                })
            # tspanの tail は次のテキスト
            if child.tail and child.tail.strip():
                runs.append({
                    'text': child.tail,
                    'attrs': text_attrs,
                    'explicit_xy': (False, False),
                })

    return runs, text_attrs

def convert_text_to_path_matplotlib(text_elem):
    if not USE_MATPLOTLIB:
        return None

    try:
        runs, text_attrs = _collect_runs(text_elem, {})
        if not runs:
            return None

        # text自体の x,y を基準点とする
        cursor_x = parse_float(text_attrs.get('x', 0))
        cursor_y = parse_float(text_attrs.get('y', 0))

        anchor = text_attrs.get('text-anchor', 'start')
        baseline = text_attrs.get('dominant-baseline', 'auto')

        # 全体の幅を計算 (text-anchor middle/end 対応用)
        # → まず仮レイアウト
        total_advance = 0.0
        ymin = None
        ymax = None
        layout = []  # (run_text, run_x, run_y, font_size, font_family, weight, fill, opacity)
        run_cursor_x = cursor_x
        run_cursor_y = cursor_y

        for run in runs:
            ra = run['attrs']
            font_size = parse_float(ra.get('font-size', 16))
            font_family = get_safe_font_family(ra.get('font-family', ''), ra.get('font-weight'))
            weight = ra.get('font-weight', 'normal')
            fill = ra.get('fill')
            fill_opacity = ra.get('fill-opacity')
            opacity = ra.get('opacity')

            # explicit な x/y があれば cursor を上書き
            ex_x, ex_y = run['explicit_xy']
            if ex_x and 'x' in ra:
                run_cursor_x = parse_float(ra['x'])
            if ex_y and 'y' in ra:
                run_cursor_y = parse_float(ra['y'])

            # dx, dy はオフセット
            if 'dx' in ra:
                run_cursor_x += parse_float(ra['dx'])
            if 'dy' in ra:
                run_cursor_y += parse_float(ra['dy'])

            # 仮にこのrunの幅を計算。外接幅ではなく advance width を使う。
            prop = _build_font_properties(font_family, weight=weight)
            tp = TextPath((0, 0), run['text'], size=font_size, prop=prop)
            bbox = tp.get_extents()
            width = _text_advance_width(run['text'], prop, font_size)
            if width <= 0:
                width = bbox.width

            layout.append({
                'text': run['text'],
                'x': run_cursor_x,
                'y': run_cursor_y,
                'font_size': font_size,
                'font_family': font_family,
                'weight': weight,
                'fill': fill,
                'fill_opacity': fill_opacity,
                'opacity': opacity,
                'bbox': bbox,
                'advance': width,
            })

            if ymin is None or bbox.y0 < ymin: ymin = bbox.y0
            if ymax is None or bbox.y1 > ymax: ymax = bbox.y1
            total_advance += width
            run_cursor_x += width

        # text-anchor によるシフト
        dx_anchor = 0.0
        if anchor == 'middle':
            dx_anchor = -total_advance / 2.0
        elif anchor == 'end':
            dx_anchor = -total_advance

        # baseline によるシフト (text要素全体に対して)
        dy_baseline = 0.0
        if ymin is not None and ymax is not None:
            if baseline in ('central', 'middle'):
                dy_baseline = -(ymin + ymax) / 2.0
            elif baseline in ('text-after-edge', 'bottom'):
                dy_baseline = -ymax
            elif baseline in ('text-before-edge', 'top', 'hanging'):
                dy_baseline = -ymin

        # 各run毎にpath要素を生成し、それを <g> にまとめる
        g_elem = ET.Element(f"{{{SVG_NS}}}g")

        # text要素由来のtransform は g に引き継ぐ
        for attr in ('transform', 'clip-path', 'mask', 'filter', 'opacity'):
            v = text_elem.get(attr)
            if v is not None:
                g_elem.set(attr, v)

        for L in layout:
            d_str, _ = _text_path_for_run(
                L['text'],
                L['x'] + dx_anchor,
                L['y'] + dy_baseline,
                L['font_size'],
                L['font_family'],
                L['weight'],
            )
            if not d_str.strip():
                continue
            p = ET.Element(f"{{{SVG_NS}}}path")
            p.set('d', d_str)
            # fill 指定
            if L['fill'] is not None:
                p.set('fill', L['fill'])
            else:
                # text要素自体や祖先のfillに任せる: 既定はcurrentColor風に黒
                # → ただし _flatten_styles で fill が無ければ親から来ているはず
                p.set('fill', '#000000')
            if L['fill_opacity'] is not None:
                p.set('fill-opacity', L['fill_opacity'])
            if L['opacity'] is not None and 'opacity' not in g_elem.attrib:
                p.set('opacity', L['opacity'])
            g_elem.append(p)

        if len(g_elem) == 0:
            return None
        # 子が1個だけなら g をスキップして直接 path を返す (transform等が無い場合)
        if len(g_elem) == 1 and not g_elem.attrib:
            return g_elem[0]
        return g_elem
    except Exception as e:
        print(f"[Warn] Text conversion error: {e}")
        return None

# ==========================================
# 3d. SVG 共通変換パス
# ==========================================

def python_pass(input_path, output_path, convert_text=True, keep_filters=True):
    """
    Python 側で実施する共通処理:
      - <style>/style属性 を個別属性に焼き込む (CSSフラット化) ← v12 新規
      - Base64埋め込み
      - rect/circle/ellipse/line/polyline/polygon → path
      - pattern fill (#grid / #dots) を実体図形に展開
      - keep_filters=False の場合は filter属性 と defs内 <filter> を削除
        ※デフォルトは True に変更 (光・グロー効果を残す)
      - <style>/<pattern> は不要なので defs から削除
      - convert_text=True の場合は text → path (matplotlib)
        Inkscape 併用モードでは convert_text=False にして Inkscape に任せる
    """
    tree = ET.parse(input_path)
    root = tree.getroot()
    base_dir = os.path.dirname(input_path)

    # 1) まず CSS/style属性を個別属性に焼き込む (これにより <style> 削除後も色等が保持される)
    _flatten_styles(root)

    for parent in root.iter():
        for elem in list(parent):
            tag = get_tag_name(elem)

            if tag == 'image':
                href_attr = f"{{{XLINK_NS}}}href"
                src = elem.get('href') or elem.get(href_attr)
                if src:
                    embedded_src = get_image_base64(src, base_dir)
                    if elem.get('href'): elem.set('href', embedded_src)
                    else: elem.set(href_attr, embedded_src)

            # filter属性: keep_filters=False のときだけ削除
            if not keep_filters and 'filter' in elem.attrib:
                elem.attrib.pop('filter', None)

            if tag == 'rect':
                fill = elem.get('fill', '')
                x = parse_float(elem.get('x', 0)); y = parse_float(elem.get('y', 0))
                w = parse_float(elem.get('width', 0)); h = parse_float(elem.get('height', 0))

                if fill == 'url(#grid)': replace_element(parent, elem, build_grid_overlay(x, y, w, h)); continue
                elif fill == 'url(#dots)': replace_element(parent, elem, build_dots_overlay(x, y, w, h)); continue

                try:
                    rx = parse_float(elem.get('rx', 0)); ry = parse_float(elem.get('ry', rx))
                    path_d = rect_to_path_d(x, y, w, h, rx, ry)
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', path_d)
                    for k, v in elem.attrib.items():
                        if k not in ('x', 'y', 'width', 'height', 'rx', 'ry'):
                            new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag == 'ellipse':
                try:
                    cx = parse_float(elem.get('cx', 0)); cy = parse_float(elem.get('cy', 0))
                    rx = parse_float(elem.get('rx', 0)); ry = parse_float(elem.get('ry', 0))
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', ellipse_to_path_d(cx, cy, rx, ry))
                    for k, v in elem.attrib.items():
                        if k not in ('cx', 'cy', 'rx', 'ry'):
                            new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag == 'line':
                try:
                    x1 = parse_float(elem.get('x1', 0)); y1 = parse_float(elem.get('y1', 0))
                    x2 = parse_float(elem.get('x2', 0)); y2 = parse_float(elem.get('y2', 0))
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', line_to_path_d(x1, y1, x2, y2))
                    for k, v in elem.attrib.items():
                        if k not in ('x1', 'y1', 'x2', 'y2'):
                            new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag in ('polyline', 'polygon'):
                try:
                    d = points_to_path_d(elem.get('points', ''), close=(tag == 'polygon'))
                    if d:
                        new_elem = ET.Element(f"{{{SVG_NS}}}path")
                        new_elem.set('d', d)
                        for k, v in elem.attrib.items():
                            if k not in ('points',):
                                new_elem.set(k, v)
                        replace_element(parent, elem, [new_elem])
                        continue
                except Exception: continue

            elif tag == 'text' and convert_text:
                new_elem = convert_text_to_path_matplotlib(elem)
                if new_elem is not None:
                    replace_element(parent, elem, [new_elem])

    # defs 内の不要要素を削除。<filter> は keep_filters=True の時は残す。
    for defs in root.iter():
        if get_tag_name(defs) == 'defs':
            for child in list(defs):
                ct = get_tag_name(child)
                if ct == 'style' or ct == 'pattern':
                    defs.remove(child)
                elif ct == 'filter' and not keep_filters:
                    defs.remove(child)

    tree.write(output_path, encoding='utf-8', xml_declaration=True)


def inkscape_pass(input_path, output_path, inkscape_cmd):
    """
    Inkscape CLI による処理:
      - Object to Path (select-all → object-to-path)
      - Stroke to Path
      - Clone 解除 (unlink-clones)
      - 不要な defs の削除 (vacuum-defs)
    """
    actions = ";".join([
        "select-all:all",
        "unlink-clones",
        "object-stroke-to-path",
        "object-to-path",
        "vacuum-defs",
        f"export-filename:{output_path}",
        "export-do",
    ])
    cmd = [inkscape_cmd, input_path, "--actions", actions]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Inkscape error (code {result.returncode}): {result.stderr}")
    if not os.path.exists(output_path):
        raise RuntimeError("Inkscape did not produce an output file.")


def _collect_circle_specs(root):
    """Original SVG から circle 要素の属性を id 単位で保持する。"""
    specs = {}
    for elem in root.iter():
        if get_tag_name(elem) != 'circle':
            continue
        cid = elem.get('id')
        if not cid:
            continue
        specs[cid] = {
            'cx': elem.get('cx'),
            'cy': elem.get('cy'),
            'r': elem.get('r'),
            'style': elem.get('style'),
            'fill': elem.get('fill'),
            'fill-opacity': elem.get('fill-opacity'),
            'opacity': elem.get('opacity'),
            'stroke': elem.get('stroke'),
            'stroke-opacity': elem.get('stroke-opacity'),
            'stroke-width': elem.get('stroke-width'),
            'stroke-linecap': elem.get('stroke-linecap'),
            'stroke-linejoin': elem.get('stroke-linejoin'),
            'transform': elem.get('transform'),
            'class': elem.get('class'),
        }
    return specs


def _restore_circles_by_id(root, circle_specs):
    """Inkscapeで path 化された circle を、元の circle 要素に戻す。"""
    if not circle_specs:
        return

    def find_parent(target):
        for parent in root.iter():
            for child in list(parent):
                if child is target:
                    return parent
        return None

    for elem in list(root.iter()):
        cid = elem.get('id')
        if not cid or cid not in circle_specs:
            continue
        if get_tag_name(elem) != 'path':
            continue
        spec = circle_specs[cid]
        circle = ET.Element(f"{{{SVG_NS}}}circle")
        for k, v in elem.attrib.items():
            if k == 'd':
                continue
            circle.set(k, v)
        for k in ('cx', 'cy', 'r', 'style', 'fill', 'fill-opacity', 'opacity', 'stroke', 'stroke-opacity', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'transform', 'class'):
            v = spec.get(k)
            if v is not None and circle.get(k) is None:
                circle.set(k, v)
        # path の d は捨て、元 circle に置換
        parent = find_parent(elem)
        if parent is None:
            continue
        idx = list(parent).index(elem)
        parent.remove(elem)
        parent.insert(idx, circle)


def process_svg_pure(input_path, output_path):
    """Pure Python モード: 全部 Python 側で処理 (circle はそのまま残す)"""
    python_pass(input_path, output_path, convert_text=True, keep_filters=True)


def process_svg_with_inkscape(input_path, output_path, inkscape_cmd):
    """
    Inkscape 併用モード:
      1. 先に Python 側で CSS を個別属性に焼き込み
      2. Inkscape で object-to-path などを実行
      3. Python 後処理で circle だけ元の circle 要素に戻す
    """
    pre_tmp = tempfile.mktemp(suffix="_pre.svg")
    post_tmp = tempfile.mktemp(suffix="_ink.svg")
    try:
        tree = ET.parse(input_path)
        root = tree.getroot()
        _flatten_styles(root)
        circle_specs = _collect_circle_specs(root)
        tree.write(pre_tmp, encoding='utf-8', xml_declaration=True)

        inkscape_pass(pre_tmp, post_tmp, inkscape_cmd)

        python_pass(post_tmp, output_path, convert_text=False, keep_filters=True)

        # Inkscape が circle を path 化しても、元の circle に戻す
        out_tree = ET.parse(output_path)
        out_root = out_tree.getroot()
        _restore_circles_by_id(out_root, circle_specs)
        out_tree.write(output_path, encoding='utf-8', xml_declaration=True)
    finally:
        for p in (pre_tmp, post_tmp):
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass


# ==========================================
# 3e. PNGプレビュー用: cairosvg 用フォント設定
# ==========================================

def _setup_cairosvg_fonts():
    """
    cairosvg は内部的に fontconfig (cairo) を使うため、システムにインストールされた
    フォントなら自動で解決される。matplotlib に登録済みのフォントだけ確認のため列挙。
    """
    # 通常は何もする必要なし。フォントが足りない場合のヒント表示のみ。
    pass


# ==========================================
# 4. GUI
# ==========================================

class AdvancedSVGEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SVG Standalone Flattener (v12)")
        self.root.geometry("1200x650")
        self.inkscape_path = None
        self.original_svg_path = None
        self.processed_pure_path = None
        self.processed_ink_path = None
        self.tk_images = []

        # モード: "pure" or "both" (both = Pure + Inkscape併用 両方生成)
        self.mode_var = tk.StringVar(value="pure")

        self.setup_ui()
        # UI起動後に Inkscape のチェック
        self.root.after(100, self.check_dependencies)

    def setup_ui(self):
        # --- 上段: ボタン ---
        top = tk.Frame(self.root, padx=10, pady=10)
        top.pack(side=tk.TOP, fill=tk.X)
        tk.Button(top, text="1. SVGを開く", command=self.open_svg).pack(side=tk.LEFT, padx=5)
        self.btn_proc = tk.Button(top, text="2. 変換処理", command=self.process_svg, state=tk.DISABLED)
        self.btn_proc.pack(side=tk.LEFT, padx=5)
        self.btn_save = tk.Button(top, text="3. 保存", command=self.save_svg, state=tk.DISABLED)
        self.btn_save.pack(side=tk.LEFT, padx=5)

        # --- 中段: モード切替 ---
        mode_frame = tk.LabelFrame(self.root, text="処理モード", padx=10, pady=5)
        mode_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        self.rb_pure = tk.Radiobutton(
            mode_frame, text="Pure Python のみ (Inkscape不要・軽量)",
            variable=self.mode_var, value="pure", command=self.on_mode_changed
        )
        self.rb_pure.pack(side=tk.LEFT, padx=5)
        self.rb_both = tk.Radiobutton(
            mode_frame, text="Inkscape併用 (Pure版とInkscape版の両方を生成)",
            variable=self.mode_var, value="both", command=self.on_mode_changed
        )
        self.rb_both.pack(side=tk.LEFT, padx=5)
        # 起動直後は Inkscape チェック前なのでとりあえず disable
        self.rb_both.config(state=tk.DISABLED)

        self.lbl_inkscape_status = tk.Label(mode_frame, text="Inkscape: 確認中...", fg="gray")
        self.lbl_inkscape_status.pack(side=tk.LEFT, padx=20)

        # --- 下段: プレビュー ---
        self.main_frame = tk.Frame(self.root)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        self.frame1 = tk.Frame(self.main_frame)
        self.lbl1 = tk.Label(self.frame1, text="元 SVG", bg="#222", fg="white")
        self.lbl1.pack(fill=tk.X)
        self.canvas1 = tk.Canvas(self.frame1, bg="#333")
        self.canvas1.pack(fill=tk.BOTH, expand=True)

        self.frame2 = tk.Frame(self.main_frame)
        self.lbl2 = tk.Label(self.frame2, text="Pure Python 処理後", bg="#222", fg="white")
        self.lbl2.pack(fill=tk.X)
        self.canvas2 = tk.Canvas(self.frame2, bg="#333")
        self.canvas2.pack(fill=tk.BOTH, expand=True)

        self.frame3 = tk.Frame(self.main_frame)
        self.lbl3 = tk.Label(self.frame3, text="Inkscape併用 処理後", bg="#222", fg="white")
        self.lbl3.pack(fill=tk.X)
        self.canvas3 = tk.Canvas(self.frame3, bg="#333")
        self.canvas3.pack(fill=tk.BOTH, expand=True)

        self.relayout_canvases()

    def relayout_canvases(self):
        for f in (self.frame1, self.frame2, self.frame3):
            f.pack_forget()
        if self.mode_var.get() == "pure":
            self.frame1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
            self.frame2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
        else:
            self.frame1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
            self.frame2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
            self.frame3.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)

    def on_mode_changed(self):
        self.relayout_canvases()

    def check_dependencies(self):
        ink = get_inkscape_command()
        if ink:
            self.inkscape_path = ink
            self.lbl_inkscape_status.config(text=f"Inkscape: ✅ 利用可能", fg="green")
            self.rb_both.config(state=tk.NORMAL)
            self.mode_var.set("both")
            self.relayout_canvases()
            return

        self.lbl_inkscape_status.config(text="Inkscape: ❌ 未検出 (Pure Python モードのみ)", fg="red")
        self.rb_both.config(state=tk.DISABLED)
        self.mode_var.set("pure")
        self.relayout_canvases()

        if platform.system() == "Windows":
            ans = messagebox.askyesno(
                "Inkscape が見つかりません",
                "Inkscape が見つかりませんでした。\nダウンロード/インストールしますか？\n"
                "(数分かかります。スキップしても Pure Python モードで動作します)"
            )
            if ans:
                self.start_download()

    def start_download(self):
        self.dl_window = tk.Toplevel(self.root)
        self.dl_window.title("Inkscape セットアップ")
        self.dl_window.geometry("400x150")
        self.dl_window.transient(self.root)
        self.dl_window.grab_set()

        self.lbl_status = tk.Label(self.dl_window, text="準備中...")
        self.lbl_status.pack(pady=10)

        self.progress = ttk.Progressbar(self.dl_window, orient="horizontal", length=300, mode="determinate")
        self.progress.pack(pady=10)

        threading.Thread(target=self._download_task, daemon=True).start()

    def _download_task(self):
        info = INKSCAPE_URLS["Windows"]
        url = info["url"] if isinstance(info, dict) else info
        file_name = os.path.basename(urlparse(url).path) or "inkscape_installer.msi"
        local_path = os.path.abspath(file_name)

        def prog_cb(down, total):
            pct = int(down / total * 100)
            self.root.after(0, lambda: self.progress.config(value=pct))

        try:
            self.root.after(0, lambda: self.lbl_status.config(text="ダウンロード中..."))
            download_file_with_progress(url, local_path, prog_cb)

            self.root.after(0, lambda: self.lbl_status.config(text="インストール/展開中..."))
            self.root.after(0, lambda: self.progress.config(mode="indeterminate"))
            self.root.after(0, self.progress.start)

            if local_path.lower().endswith(".msi"):
                install_inkscape_msi(local_path)
            else:
                extract_dir = "inkscape_portable"
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(local_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

            self.inkscape_path = get_inkscape_command()
            self.root.after(0, self.progress.stop)
            self.root.after(0, self.dl_window.destroy)

            if self.inkscape_path:
                self.root.after(0, lambda: self.lbl_inkscape_status.config(text="Inkscape: ✅ 利用可能", fg="green"))
                self.root.after(0, lambda: self.rb_both.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.mode_var.set("both"))
                self.root.after(0, self.relayout_canvases)
                self.root.after(0, lambda: messagebox.showinfo("完了", "Inkscape のセットアップが完了しました！"))
            else:
                self.root.after(0, lambda: messagebox.showerror("エラー", "インストールに失敗しました。手動で確認してください。"))

        except Exception as e:
            self.root.after(0, self.dl_window.destroy)
            self.root.after(0, lambda: messagebox.showerror("エラー", f"処理中にエラーが発生しました:\n{e}"))

    def display_svg(self, path, canvas):
        canvas.delete("all")
        if not path or not os.path.exists(path):
            return
        try:
            if USE_CAIROSVG:
                # cairosvg は filter効果まで完全には描かないので注意
                # (実体は cairo + libpangocairo に依存)
                png = cairosvg.svg2png(url=path)
                img = Image.open(io.BytesIO(png))
                img.thumbnail((400, 500))
                photo = ImageTk.PhotoImage(img)
                self.tk_images.append(photo)
                canvas.create_image(200, 250, image=photo, anchor=tk.CENTER)
            else:
                img = tksvg.SvgImage(file=path)
                self.tk_images.append(img)
                canvas.create_image(200, 250, image=img, anchor=tk.CENTER)
        except Exception as e:
            print(f"Display error: {e}")

    def open_svg(self):
        p = filedialog.askopenfilename(filetypes=[("SVG Files", "*.svg")])
        if p:
            self.original_svg_path = p
            self.display_svg(p, self.canvas1)
            self.processed_pure_path = None
            self.processed_ink_path = None
            self.canvas2.delete("all")
            self.canvas3.delete("all")
            self.btn_proc.config(state=tk.NORMAL)
            self.btn_save.config(state=tk.DISABLED)

    def process_svg(self):
        if not self.original_svg_path:
            return
        mode = self.mode_var.get()
        try:
            self.processed_pure_path = tempfile.mktemp(suffix="_pure.svg")
            process_svg_pure(self.original_svg_path, self.processed_pure_path)
            self.display_svg(self.processed_pure_path, self.canvas2)

            if mode == "both":
                if not self.inkscape_path:
                    messagebox.showerror("エラー", "Inkscape が見つかりません。Pure モードのみ実行しました。")
                else:
                    self.processed_ink_path = tempfile.mktemp(suffix="_ink.svg")
                    process_svg_with_inkscape(self.original_svg_path, self.processed_ink_path, self.inkscape_path)
                    self.display_svg(self.processed_ink_path, self.canvas3)
            else:
                self.processed_ink_path = None

            self.btn_save.config(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("エラー", f"変換処理中にエラーが発生しました:\n{e}")

    def save_svg(self):
        mode = self.mode_var.get()

        if mode == "pure":
            if not self.processed_pure_path:
                return
            p = filedialog.asksaveasfilename(
                defaultextension=".svg",
                filetypes=[("SVG Files", "*.svg")],
                title="Pure Python 処理結果を保存"
            )
            if p:
                shutil.copy(self.processed_pure_path, p)
                messagebox.showinfo("Success", f"保存しました:\n{p}")
            return

        choice = self._ask_save_choice()
        if choice is None:
            return

        if choice in ("pure", "both") and self.processed_pure_path:
            p = filedialog.asksaveasfilename(
                defaultextension=".svg",
                filetypes=[("SVG Files", "*.svg")],
                title="Pure Python 処理結果を保存",
                initialfile="output_pure.svg"
            )
            if p:
                shutil.copy(self.processed_pure_path, p)

        if choice in ("ink", "both") and self.processed_ink_path:
            p = filedialog.asksaveasfilename(
                defaultextension=".svg",
                filetypes=[("SVG Files", "*.svg")],
                title="Inkscape併用 処理結果を保存",
                initialfile="output_inkscape.svg"
            )
            if p:
                shutil.copy(self.processed_ink_path, p)

        messagebox.showinfo("Success", "保存が完了しました!")

    def _ask_save_choice(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("保存対象の選択")
        dlg.geometry("300x180")
        dlg.transient(self.root)
        dlg.grab_set()

        result = {"choice": None}
        tk.Label(dlg, text="どれを保存しますか?", pady=10).pack()

        def pick(v):
            result["choice"] = v
            dlg.destroy()

        tk.Button(dlg, text="Pure Python 版のみ", width=25, command=lambda: pick("pure")).pack(pady=3)
        tk.Button(dlg, text="Inkscape併用 版のみ", width=25, command=lambda: pick("ink")).pack(pady=3)
        tk.Button(dlg, text="両方", width=25, command=lambda: pick("both")).pack(pady=3)
        tk.Button(dlg, text="キャンセル", width=25, command=dlg.destroy).pack(pady=3)

        self.root.wait_window(dlg)
        return result["choice"]


if __name__ == "__main__":
    root = tk.Tk()
    app = AdvancedSVGEditorApp(root)
    root.mainloop()
