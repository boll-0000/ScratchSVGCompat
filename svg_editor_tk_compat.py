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
XML_NS = "http://www.w3.org/XML/1998/namespace"
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

def parse_number_list(value):
    """SVG の x/y/dx/dy のような数値リストを float 配列にする。"""
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    parts = re.split(r'[\s,]+', str(value).strip())
    return [parse_float(p) for p in parts if p]

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
    'white-space', 'line-height',
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
    'white-space', 'line-height',
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
    要素の実効スタイルを計算する (SVG2 / CSS Cascade 準拠):
      1. parent_styleのうち「継承されるプロパティ」だけを継承
      2. 個別 presentation 属性 (fill="..." 等) は specificity 0 の
         author 宣言として最初に適用 (CSSルールより弱い)
      3. CSSルール (specificity昇順で適用 = 詳細度の高いものが後勝ち)
      4. style属性 (インライン style) は最優先
    """
    # 継承プロパティのみ取り込む
    style = {k: v for k, v in parent_style.items() if k in _INHERITED_PROPS}
    tag_name = get_tag_name(elem)

    # 個別 presentation 属性 (specificity 0 として先に適用)
    for prop in _PRESENTATION_PROPS:
        v = elem.get(prop)
        if v is not None:
            style[prop] = v

    # CSS ルール (specificity 昇順済み) — presentation 属性より優先
    for rule in css_rules:
        sel, decls = rule[0], rule[1]
        if _selector_matches(elem, sel, tag_name):
            style.update(decls)

    # style属性 (インライン style) が最優先
    style.update(_parse_style_string(elem.get('style', '')))

    return _resolve_special_style_values(style, parent_style)

def _apply_style_to_elem(elem, style):
    """ 計算済みstyleを個別属性として要素に書き込む。元の class/style属性は残す。 """
    # 元々個別属性として存在しないものだけ、属性として "焼き込む"
    for k, v in style.items():
        if k not in _PRESENTATION_PROPS:
            continue
        # CSS解決値を常に適用（SVG仕様準拠: cascaded value を焼き込む）
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
    # TextPath/FT2Font に改行・タブ・制御文字を直接渡すと、フォントによって
    # .notdef の四角や謎のサブパスが混ざるため、幅計測は必ず安全な文字列で行う。
    text = _textpath_safe_text(text)
    if not text:
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

def _textpath_safe_text(text):
    """TextPath に渡してよい、描画可能文字だけの文字列へ変換する。"""
    if text is None:
        return ''
    safe = []
    for ch in str(text):
        # 改行/CR/タブ/その他C0制御文字は TextPath に渡さない。
        # 空白は advance として別処理するため、通常スペースも除外する。
        if ch in ('\n', '\r', '\t') or (ord(ch) < 32):
            continue
        if ch.isspace():
            continue
        safe.append(ch)
    return ''.join(safe)

def _has_preserve_space(attrs):
    """xml:space / CSS white-space から空白保持モードかを判定。"""
    xml_space = (
        attrs.get(f"{{{XML_NS}}}space")
        or attrs.get('xml:space')
        or attrs.get('space')
    )
    if xml_space == 'preserve':
        return True
    white_space = str(attrs.get('white-space', '')).strip().lower()
    return white_space in ('pre', 'pre-wrap', 'pre-line', 'break-spaces')

def _normalize_text_for_layout(text, preserve_space=False):
    """
    SVGテキストを TextPath に直渡しせず、自前レイアウト用に正規化する。
    - CRLF/CR は LF に統一
    - タブは保持モードではタブ停止、通常モードではスペース扱い
    - ソース整形由来の前後改行・インデントは通常モードで取り除く
    """
    if text is None:
        return ''
    s = str(text).replace('\r\n', '\n').replace('\r', '\n')
    # TextPath が苦手な制御文字は、改行/タブ以外を空文字化する。
    s = ''.join(ch for ch in s if ch in ('\n', '\t') or ord(ch) >= 32)
    if preserve_space:
        return s

    if '\n' in s:
        lines = s.split('\n')
        # <text>\n  文字\n</text> のようなSVG整形インデントを除去する。
        while lines and not lines[0].strip(' \t'):
            lines.pop(0)
        while lines and not lines[-1].strip(' \t'):
            lines.pop()
        lines = [re.sub(r'[ \t]+', ' ', line.strip(' \t')) for line in lines]
        return '\n'.join(lines)

    return re.sub(r'[ \t]+', ' ', s.replace('\t', ' '))

def _line_height_value(value, font_size):
    """line-height をユーザー単位に変換。未指定/normal は 1.2em。"""
    if value is None:
        return font_size * 1.2
    s = str(value).strip().lower()
    if not s or s == 'normal':
        return font_size * 1.2
    try:
        if s.endswith('%'):
            return font_size * float(s[:-1]) / 100.0
        if re.fullmatch(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', s):
            return font_size * float(s)
    except Exception:
        pass
    return parse_float(value, font_size * 1.2)

def _spacing_value(value, font_size, default=0.0):
    """letter-spacing / word-spacing 用。normal は 0。"""
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s or s == 'normal':
        return default
    if s.endswith('em'):
        try:
            return float(s[:-2]) * font_size
        except Exception:
            return default
    return parse_float(value, default)

def _text_bbox_for_safe_text(text, font_props, font_size):
    """安全な文字列だけで bbox を取得。空白/制御文字だけなら None。"""
    safe = _textpath_safe_text(text)
    if not safe:
        return None
    try:
        return TextPath((0, 0), safe, size=font_size, prop=font_props).get_extents()
    except Exception:
        return None

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

_FONT_WEIGHT_CACHE = {}
def _font_file_weight_class(path):
    """
    resolved フォントファイルの実効 usWeightClass (100..900) を返す。
    OS/2 テーブルが優先。無い場合は style_flags / 名前から推定。
    判定不能なら 400 (Regular) とみなす。
    """
    if not path:
        return 400
    if path in _FONT_WEIGHT_CACHE:
        return _FONT_WEIGHT_CACHE[path]
    weight = None
    try:
        f = ft2font.FT2Font(path)
        try:
            os2 = f.get_sfnt_table('OS/2')
            if os2:
                w = int(os2.get('usWeightClass', 0) or 0)
                if 1 <= w <= 1000:
                    # usWeightClass 1..1000 を CSS 100..900 相当に丸める
                    weight = max(100, min(900, int(round(w / 100.0) * 100)))
        except Exception:
            pass
        if weight is None:
            try:
                if bool(f.style_flags & 2):  # FT_STYLE_FLAG_BOLD
                    weight = 700
            except Exception:
                pass
        if weight is None:
            name = ((getattr(f, 'postscript_name', '') or '') + ' ' +
                    (getattr(f, 'style_name', '') or '')).lower()
            # 順序重要: より重いキーワードを先に見る
            name_map = [
                ('black', 900), ('heavy', 900), ('extrabold', 800), ('ultrabold', 800),
                ('extra bold', 800), ('ultra bold', 800),
                ('semibold', 600), ('demibold', 600), ('semi bold', 600), ('demi bold', 600),
                ('bold', 700),
                ('medium', 500),
                ('light', 300), ('thin', 100), ('hairline', 100),
            ]
            for kw, w in name_map:
                if kw in name:
                    weight = w
                    break
    except Exception:
        weight = None
    if weight is None:
        weight = 400
    _FONT_WEIGHT_CACHE[path] = weight
    return weight

def _font_file_is_bold(path):
    """後方互換: usWeightClass >= 600 を bold とみなす。"""
    return _font_file_weight_class(path) >= 600

def _requested_weight_value(weight):
    """font-weight 属性を数値 (100..900) に正規化。既定は 400。"""
    if weight is None:
        return 400
    s = str(weight).strip().lower()
    if not s or s == 'normal':
        return 400
    if s == 'bold':
        return 700
    if s == 'bolder':
        return 700
    if s == 'lighter':
        return 300
    try:
        w = int(float(s))
        return max(100, min(900, w))
    except (ValueError, TypeError):
        return 400

def _weight_requests_bold(weight):
    return _requested_weight_value(weight) >= 600

def _synthetic_bold_stroke(font_props, weight, font_size):
    """
    合成ボールドは廃止。

    matplotlib TextPath は変数フォント / .ttc / システムのフォールバックを
    使うと、要求 weight と実際に描画される glyph outline の太さの関係が
    まったく予測できない (usWeightClass が 100 でも Regular ~ Bold で
    描画されるなど)。

    その状態で stroke を上乗せすると「Inkscape 版よりも明らかに太い」
    という症状 (ユーザ報告: pure 版は太くなり過ぎ) が発生する。

    リスクの低い唯一の選択肢は合成ボールドを行わないこと。matplotlib が
    実フォントの Bold を拾えないケースでは若干細めに出るが、
    "太すぎる" よりは "実物どおり" の方が期待に近い。
    """
    return 0.0




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

def _raw_advance_width(text, font_props, font_size, fallback=None):
    """空白も含めた advance 幅。TextPath には空白/制御文字を渡さない。"""
    if fallback is None:
        fallback = font_size * 0.5
    if not text:
        return 0.0
    safe_raw = ''.join(ch for ch in str(text) if ord(ch) >= 32 and ch not in ('\n', '\r', '\t'))
    if not safe_raw:
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
        if font_path and os.path.exists(font_path):
            font = ft2font.FT2Font(font_path)
            font.set_size(font_size, 72)
            font.set_text(safe_raw, 0.0)
            width, _height = font.get_width_height()
            if width > 0:
                return width / 64.0
    except Exception:
        pass

    width = 0.0
    for ch in safe_raw:
        if ch == ' ':
            width += font_size * 0.33
        elif ch == '\u3000':
            width += font_size
        elif ch.isspace():
            width += font_size * 0.33
        else:
            width += _text_advance_width(ch, font_props, font_size) or fallback
    return width

def _text_path_for_run(text_run, x, y, font_size, font_family, weight, style=None):
    """ 1run分のテキストを path d文字列+幅 で返す """
    safe_text = _textpath_safe_text(text_run)
    if not safe_text:
        return "", None
    prop = _build_font_properties(font_family, weight=weight, style=style)
    tp = TextPath((0, 0), safe_text, size=font_size, prop=prop)
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
                  'stroke', 'stroke-width', 'stroke-opacity',
                  'stroke-linejoin', 'stroke-linecap', 'paint-order',
                  'text-anchor', 'dominant-baseline',
                  'letter-spacing', 'word-spacing', 'white-space', 'line-height'):

            v = _get_effective_attr(elem, k)
            if v is not None:
                a[k] = v
        for k in (f"{{{XML_NS}}}space", 'xml:space'):
            v = elem.get(k)
            if v is not None:
                a[k] = v
        return a

    text_attrs = attrs_of(text_elem, parent_attrs)

    # text 自体に直接テキストがあるか?
    direct_text = (text_elem.text or '')
    if direct_text:
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
            if child.tail:
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

        # TextPath に改行/タブ/先頭空白を直渡しせず、ここでSVG風にレイアウトする。
        ymin = None
        ymax = None
        layout = []
        line_metrics = [{'start_x': cursor_x, 'max_x': cursor_x}]
        current_line = 0
        line_start_x = cursor_x
        run_cursor_x = cursor_x
        run_cursor_y = cursor_y

        def ensure_line(line_index):
            while len(line_metrics) <= line_index:
                line_metrics.append({'start_x': cursor_x, 'max_x': cursor_x})

        def update_line_width(line_index, x_end):
            ensure_line(line_index)
            if x_end > line_metrics[line_index]['max_x']:
                line_metrics[line_index]['max_x'] = x_end

        def append_text_segment(text_value, x, y, line_index, font_size, font_family,
                                weight, style, fill, fill_opacity, opacity,
                                stroke=None, stroke_width=None, stroke_opacity=None,
                                stroke_linejoin=None, stroke_linecap=None,
                                paint_order=None):
            nonlocal ymin, ymax
            safe_value = _textpath_safe_text(text_value)
            if not safe_value:
                return 0.0
            prop = _build_font_properties(font_family, weight=weight, style=style)
            bbox = _text_bbox_for_safe_text(safe_value, prop, font_size)
            advance = _text_advance_width(safe_value, prop, font_size)
            if advance <= 0 and bbox is not None:
                advance = bbox.width
            if advance <= 0:
                return 0.0

            layout.append({
                'text': safe_value,
                'x': x,
                'y': y,
                'line': line_index,
                'font_size': font_size,
                'font_family': font_family,
                'weight': weight,
                'style': style,
                'fill': fill,
                'fill_opacity': fill_opacity,
                'opacity': opacity,
                'stroke': stroke,
                'stroke_width': stroke_width,
                'stroke_opacity': stroke_opacity,
                'stroke_linejoin': stroke_linejoin,
                'stroke_linecap': stroke_linecap,
                'paint_order': paint_order,
                'advance': advance,
            })
            update_line_width(line_index, x + advance)
            if bbox is not None:
                if ymin is None or bbox.y0 < ymin:
                    ymin = bbox.y0
                if ymax is None or bbox.y1 > ymax:
                    ymax = bbox.y1
            return advance


        for run in runs:
            ra = run['attrs']
            font_size = parse_float(ra.get('font-size', 16))
            font_family = get_safe_font_family(ra.get('font-family', ''), ra.get('font-weight'))
            weight = ra.get('font-weight', 'normal')
            style = ra.get('font-style', 'normal')
            # PureモードでもCSS解決後の最終値（#222など）を確実に使う
            # CSS解決後の最終値（ra）を優先する
            fill = ra.get('fill') or text_elem.get('fill')
            fill_opacity = ra.get('fill-opacity')
            opacity = ra.get('opacity')
            stroke = ra.get('stroke')
            if stroke in ('none', ''):
                stroke = None
            stroke_width = ra.get('stroke-width')
            stroke_opacity = ra.get('stroke-opacity')
            stroke_linejoin = ra.get('stroke-linejoin')
            stroke_linecap = ra.get('stroke-linecap')
            paint_order = ra.get('paint-order')

            preserve_space = _has_preserve_space(ra)
            normalized_text = _normalize_text_for_layout(run['text'], preserve_space)
            if not normalized_text:
                continue
            prop = _build_font_properties(font_family, weight=weight, style=style)
            letter_spacing = _spacing_value(ra.get('letter-spacing'), font_size, 0.0)
            word_spacing = _spacing_value(ra.get('word-spacing'), font_size, 0.0)
            line_height = _line_height_value(ra.get('line-height'), font_size)
            space_width = _raw_advance_width(' ', prop, font_size, fallback=font_size * 0.33)
            if space_width <= 0:
                space_width = font_size * 0.33

            # explicit な x/y があれば cursor を上書き
            ex_x, ex_y = run['explicit_xy']
            if ex_x and 'x' in ra:
                x_values = parse_number_list(ra['x'])
                if x_values:
                    run_cursor_x = x_values[0]
                    line_start_x = run_cursor_x
                    ensure_line(current_line)
                    line_metrics[current_line]['start_x'] = line_start_x
                    line_metrics[current_line]['max_x'] = max(line_metrics[current_line]['max_x'], line_start_x)
            if ex_y and 'y' in ra:
                y_values = parse_number_list(ra['y'])
                if y_values:
                    run_cursor_y = y_values[0]

            # dx, dy はオフセット
            if 'dx' in ra:
                dx_values = parse_number_list(ra['dx'])
                if dx_values:
                    run_cursor_x += dx_values[0]
            if 'dy' in ra:
                dy_values = parse_number_list(ra['dy'])
                if dy_values:
                    run_cursor_y += dy_values[0]

            i = 0
            while i < len(normalized_text):
                ch = normalized_text[i]
                if ch == '\n':
                    current_line += 1
                    run_cursor_y += line_height
                    run_cursor_x = cursor_x
                    line_start_x = cursor_x
                    ensure_line(current_line)
                    line_metrics[current_line]['start_x'] = line_start_x
                    line_metrics[current_line]['max_x'] = line_start_x
                    i += 1
                    continue

                if ch == '\t':
                    tab_width = max(space_width * 4.0, font_size)
                    rel_x = max(0.0, run_cursor_x - line_start_x)
                    next_tab = line_start_x + ((int(rel_x / tab_width) + 1) * tab_width)
                    update_line_width(current_line, next_tab)
                    run_cursor_x = next_tab
                    i += 1
                    continue

                if ch.isspace():
                    j = i
                    while j < len(normalized_text) and normalized_text[j].isspace() and normalized_text[j] not in ('\n', '\t'):
                        j += 1
                    spaces = normalized_text[i:j]
                    width = sum(_raw_advance_width(c, prop, font_size, fallback=space_width) for c in spaces)
                    width += word_spacing * len(spaces)
                    run_cursor_x += width
                    update_line_width(current_line, run_cursor_x)
                    i = j
                    continue

                if letter_spacing == 0:
                    j = i
                    while j < len(normalized_text) and not normalized_text[j].isspace() and normalized_text[j] not in ('\n', '\t'):
                        j += 1
                    segment = normalized_text[i:j]
                else:
                    j = i + 1
                    segment = ch

                width = append_text_segment(
                    segment,
                    run_cursor_x,
                    run_cursor_y,
                    current_line,
                    font_size,
                    font_family,
                    weight,
                    style,
                    fill,
                    fill_opacity,
                    opacity,
                    stroke=stroke,
                    stroke_width=stroke_width,
                    stroke_opacity=stroke_opacity,
                    stroke_linejoin=stroke_linejoin,
                    stroke_linecap=stroke_linecap,
                    paint_order=paint_order,
                )

                run_cursor_x += width
                if letter_spacing != 0 and j < len(normalized_text) and normalized_text[j] not in ('\n', '\t'):
                    run_cursor_x += letter_spacing
                    update_line_width(current_line, run_cursor_x)
                i = j

        # text-anchor によるシフト。複数行は各行ごとに揃える。
        line_anchor_shift = []
        for metric in line_metrics:
            line_width = max(0.0, metric['max_x'] - metric['start_x'])
            if anchor == 'middle':
                line_anchor_shift.append(-line_width / 2.0)
            elif anchor == 'end':
                line_anchor_shift.append(-line_width)
            else:
                line_anchor_shift.append(0.0)

        # baseline によるシフト (text要素全体に対して)
        # matplotlib TextPath は Y-up 座標系で bbox(ymin,ymax) を返す
        # scale(1,-1) で Y-down に変換後: 画面上の top=y-ymax, bottom=y-ymin
        dy_baseline = 0.0
        if ymin is not None and ymax is not None:
            if baseline in ('central', 'middle'):
                # 中心を y 位置に合わせる
                dy_baseline = (ymin + ymax) / 2.0
            elif baseline in ('text-after-edge', 'bottom'):
                # 下端(画面上の底)を y 位置に
                dy_baseline = ymin
            elif baseline in ('text-before-edge', 'top', 'hanging'):
                # 上端(画面上の頂)を y 位置に
                dy_baseline = ymax

        # 各run毎にpath要素を生成し、それを <g> にまとめる
        g_elem = ET.Element(f"{{{SVG_NS}}}g")

        # text要素由来のtransform は g に引き継ぐ
        for attr in ('transform', 'clip-path', 'mask', 'filter', 'opacity'):
            v = text_elem.get(attr)
            if v is not None:
                g_elem.set(attr, v)

        for L in layout:
            dx_anchor = line_anchor_shift[L['line']] if L['line'] < len(line_anchor_shift) else 0.0
            d_str, _ = _text_path_for_run(
                L['text'],
                L['x'] + dx_anchor,
                L['y'] + dy_baseline,
                L['font_size'],
                L['font_family'],
                L['weight'],
                L.get('style'),
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
            # 元 <text> に指定されていた stroke (例: .pseudo-bold クラス由来) は
            # そのまま path に転写する。合成ボールドは廃止 (太くなり過ぎるため)。
            explicit_stroke = L.get('stroke')
            if explicit_stroke:
                p.set('stroke', explicit_stroke)
                if L.get('stroke_width'):
                    p.set('stroke-width', str(L['stroke_width']))
                if L.get('stroke_opacity'):
                    p.set('stroke-opacity', str(L['stroke_opacity']))
                if L.get('stroke_linejoin'):
                    p.set('stroke-linejoin', str(L['stroke_linejoin']))
                if L.get('stroke_linecap'):
                    p.set('stroke-linecap', str(L['stroke_linecap']))
                if L.get('paint_order'):
                    p.set('paint-order', str(L['paint_order']))
                else:
                    p.set('paint-order', 'stroke fill')

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
                if not fill or 'url(' not in fill:
                    style = elem.get('style', '') or ''
                    m = re.search(r'(?:^|;)\s*fill\s*:\s*([^;]+)', style)
                    if m: fill = m.group(1).strip()
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
    # 注意: object-stroke-to-path は fill=url(#gradient) と併用すると
    # gradient fill が欠落するバグがあるため除外。stroke はそのまま残す。
    # vacuum-defs は pattern 参照 (fill attribute のみ) を誤検知して削除する
    # ことがあるため使用しない。パターンは事前に Python 側で実体化済み。
    actions = ";".join([
        "select-all:all",
        "unlink-clones",
        "object-to-path",
        f"export-filename:{output_path}",
        "export-do",
    ])
    cmd = [inkscape_cmd, input_path, "--actions", actions]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Inkscape error (code {result.returncode}): {result.stderr}")
    if not os.path.exists(output_path):
        raise RuntimeError("Inkscape did not produce an output file.")



def _expand_pattern_fills(root):
    """rect (fill=url(#grid|#dots)) を、実体の格子/ドット図形に置換する。
    Inkscape 側で pattern 定義が失われても描画が壊れないようにする防御策。
    style="fill:url(#..)" 形式にも対応。"""
    _pat = re.compile(r'(?:^|;)\s*fill\s*:\s*([^;]+)')
    for parent in list(root.iter()):
        for elem in list(parent):
            if get_tag_name(elem) != 'rect':
                continue
            fill = elem.get('fill', '') or ''
            if 'url(' not in fill:
                m = _pat.search(elem.get('style', '') or '')
                if m: fill = m.group(1).strip()
            if fill not in ('url(#grid)', 'url(#dots)'):
                continue
            x = parse_float(elem.get('x', 0)); y = parse_float(elem.get('y', 0))
            w = parse_float(elem.get('width', 0)); h = parse_float(elem.get('height', 0))
            if fill == 'url(#grid)':
                replace_element(parent, elem, build_grid_overlay(x, y, w, h))
            else:
                replace_element(parent, elem, build_dots_overlay(x, y, w, h))


def _mirror_url_paint_to_style(root):
    """fill/stroke="url(#..)" 属性を style にも複製する。
    Inkscape は style 側の paint を優先して保持するため、gradient 参照の
    欠落を防げる。"""
    for elem in root.iter():
        for prop in ('fill', 'stroke'):
            val = elem.get(prop, '') or ''
            if not val.startswith('url(#'):
                continue
            style = elem.get('style', '') or ''
            if re.search(rf'(?:^|;)\s*{prop}\s*:', style):
                continue
            style = (f'{prop}:{val};' + style).rstrip(';')
            elem.set('style', style)


def _fix_filter_regions(root):
    """Inkscape によって <filter> の region が objectBoundingBox 既定
    (x=0,y=0,width=1,height=1) に正規化されるとドロップシャドウ/グロー
    が要素境界でクリップされるため、余裕のある region に戻す。"""
    for elem in root.iter():
        if get_tag_name(elem) != 'filter':
            continue
        # filterUnits が明示されていて userSpaceOnUse ならスキップ
        if (elem.get('filterUnits') or 'objectBoundingBox') != 'objectBoundingBox':
            continue
        try:
            fx = parse_float(elem.get('x', '0'))
            fy = parse_float(elem.get('y', '0'))
            fw = parse_float(elem.get('width', '1'))
            fh = parse_float(elem.get('height', '1'))
        except Exception:
            continue
        # 標的 (=クリップされる領域) は「x,y が 0 付近以上 かつ w,h が 1.05 以下」
        if fx > -0.05 and fy > -0.05 and fw < 1.05 and fh < 1.05:
            elem.set('x', '-0.5')
            elem.set('y', '-0.5')
            elem.set('width', '2')
            elem.set('height', '2')


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
        # Inkscape が触る前にパターン (#grid/#dots) を実体図形へ展開しておく。
        # そうしないと Inkscape が pattern 定義を破棄して塗りが失われる。
        _expand_pattern_fills(root)
        # fill=url(#...) 参照を style にも複製しておくと Inkscape が保持しやすい。
        _mirror_url_paint_to_style(root)
        circle_specs = _collect_circle_specs(root)
        tree.write(pre_tmp, encoding='utf-8', xml_declaration=True)

        inkscape_pass(pre_tmp, post_tmp, inkscape_cmd)

        python_pass(post_tmp, output_path, convert_text=False, keep_filters=True)

        # Inkscape が circle を path 化しても、元の circle に戻す
        out_tree = ET.parse(output_path)
        out_root = out_tree.getroot()
        _restore_circles_by_id(out_root, circle_specs)
        # Inkscape が <filter> の region を objectBoundingBox 既定 (0,0,1,1)
        # に正規化して shadow/glow が切れる問題を復元する。
        _fix_filter_regions(out_root)
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
