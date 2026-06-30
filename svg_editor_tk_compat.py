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

# --- テキストのパス化用ライブラリ ---
try:
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties, fontManager
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
# 3. SVG変換ロジック
# ==========================================

def get_tag_name(elem):
    return elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

def parse_float(value, default=0.0):
    try:
        return float(value)
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
    return f"M{cx - r},{cy} A{r},{r} 0 1 0 {cx + r},{cy} A{r},{r} 0 1 0 {cx - r},{cy} Z"

def ellipse_to_path_d(cx, cy, rx, ry):
    return f"M{cx - rx},{cy} A{rx},{ry} 0 1 0 {cx + rx},{cy} A{rx},{ry} 0 1 0 {cx - rx},{cy} Z"

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

def get_safe_font_family(requested_family):
    if not USE_MATPLOTLIB: return requested_family
    jp_fonts = ["MS Gothic", "MS PGothic", "Yu Gothic", "Meiryo", "Hiragino Kaku Gothic Pro", "Noto Sans CJK JP", "IPAGothic", "Noto Sans JP"]
    available_fonts = [f.name for f in fontManager.ttflist]
    for family in requested_family.replace("'", "").split(','):
        f = family.strip()
        if f in available_fonts: return f
    for jf in jp_fonts:
        if jf in available_fonts: return jf
    return "sans-serif"

def convert_text_to_path_matplotlib(text_elem):
    if not USE_MATPLOTLIB:
        return None

    raw_text = "".join(text_elem.itertext())
    text_content = raw_text.rstrip("\n")
    if not text_content.strip():
        return None

    try:
        x = parse_float(text_elem.get('x', 0))
        y = parse_float(text_elem.get('y', 0))
        
        # 修正1: tspan内部に座標指定がある場合の考慮を追加
        tspans = list(text_elem.findall(f"{{{SVG_NS}}}tspan"))
        if tspans:
            if 'x' in tspans[0].attrib: x = parse_float(tspans[0].get('x'))
            if 'y' in tspans[0].attrib: y = parse_float(tspans[0].get('y'))

        font_size = parse_float(text_elem.get('font-size', 16))
        anchor = text_elem.get('text-anchor', 'start')
        baseline = text_elem.get('dominant-baseline', 'auto')

        font_family = get_safe_font_family(text_elem.get('font-family', 'sans-serif'))
        prop = FontProperties(family=font_family, weight=text_elem.get('font-weight', 'normal'))

        tp = TextPath((0, 0), text_content, size=font_size, prop=prop)
        bbox = tp.get_extents()

        dx = 0.0
        if anchor == 'middle': dx = -bbox.width / 2.0
        elif anchor == 'end': dx = -bbox.width

        dy = 0.0
        if baseline in ('central', 'middle'): dy = -(bbox.y0 + bbox.y1) / 2.0
        elif baseline in ('text-after-edge', 'bottom'): dy = -bbox.y1
        elif baseline in ('text-before-edge', 'top', 'hanging'): dy = -bbox.y0

        combined_d = []
        transform = Affine2D().scale(1, -1).translate(x + dx, y + dy)
        for vertices, code in tp.iter_segments():
            v = transform.transform(vertices.reshape(-1, 2)).flatten()
            if code == Path.MOVETO: combined_d.append(f"M {v[0]:.3f},{v[1]:.3f}")
            elif code == Path.LINETO: combined_d.append(f"L {v[0]:.3f},{v[1]:.3f}")
            elif code == Path.CURVE3: combined_d.append(f"Q {v[0]:.3f},{v[1]:.3f} {v[2]:.3f},{v[3]:.3f}")
            elif code == Path.CURVE4: combined_d.append(f"C {v[0]:.3f},{v[1]:.3f} {v[2]:.3f},{v[3]:.3f} {v[4]:.3f},{v[5]:.3f}")
            elif code == Path.CLOSEPOLY: combined_d.append("Z")

        path_elem = ET.Element(f"{{{SVG_NS}}}path")
        path_elem.set('d', " ".join(combined_d))

        # 修正2: 位置ズレ防止のため 'transform', 'style' 等の属性を正しくコピーする
        for attr in ['fill', 'fill-opacity', 'opacity', 'stroke', 'stroke-opacity', 'stroke-width', 'fill-rule',
                     'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset',
                     'transform', 'style']:
            if attr in text_elem.attrib:
                path_elem.set(attr, text_elem.get(attr))

        if 'fill' not in path_elem.attrib:
            path_elem.set('fill', '#000000')

        return path_elem
    except Exception as e:
        print(f"[Warn] Text conversion error: {e}")
        return None

def process_svg_logic(input_path, output_path, inkscape_path):
    tree = ET.parse(input_path)
    root = tree.getroot()
    base_dir = os.path.dirname(input_path)

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

            if 'filter' in elem.attrib:
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
                        if k not in ('x', 'y', 'width', 'height', 'rx', 'ry', 'filter'): new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag == 'circle':
                try:
                    cx = parse_float(elem.get('cx', 0)); cy = parse_float(elem.get('cy', 0))
                    r = parse_float(elem.get('r', 0))
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', circle_to_path_d(cx, cy, r))
                    for k, v in elem.attrib.items():
                        if k not in ('cx', 'cy', 'r', 'filter'): new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag == 'ellipse':
                try:
                    cx = parse_float(elem.get('cx', 0)); cy = parse_float(elem.get('cy', 0))
                    rx = parse_float(elem.get('rx', 0)); ry = parse_float(elem.get('ry', 0))
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', ellipse_to_path_d(cx, cy, rx, ry))
                    for k, v in elem.attrib.items():
                        if k not in ('cx', 'cy', 'rx', 'ry', 'filter'): new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag == 'line':
                try:
                    x1 = parse_float(elem.get('x1', 0)); y1 = parse_float(elem.get('y1', 0))
                    x2 = parse_float(elem.get('x2', 0)); y2 = parse_float(elem.get('y2', 0))
                    new_elem = ET.Element(f"{{{SVG_NS}}}path")
                    new_elem.set('d', line_to_path_d(x1, y1, x2, y2))
                    for k, v in elem.attrib.items():
                        if k not in ('x1', 'y1', 'x2', 'y2', 'filter'): new_elem.set(k, v)
                    replace_element(parent, elem, [new_elem]); continue
                except Exception: continue

            elif tag in ('polyline', 'polygon'):
                try:
                    d = points_to_path_d(elem.get('points', ''), close=(tag=='polygon'))
                    if d:
                        new_elem = ET.Element(f"{{{SVG_NS}}}path")
                        new_elem.set('d', d)
                        for k, v in elem.attrib.items():
                            if k not in ('points', 'filter'): new_elem.set(k, v)
                        replace_element(parent, elem, [new_elem])
                        continue
                except Exception: continue

            elif tag == 'text':
                new_elem = convert_text_to_path_matplotlib(elem)
                if new_elem is not None: replace_element(parent, elem, [new_elem])

    for defs in root.iter():
        if get_tag_name(defs) == 'defs':
            for child in list(defs):
                if get_tag_name(child) in ('style', 'pattern', 'filter'): defs.remove(child)

    tree.write(output_path, encoding='utf-8', xml_declaration=True)

# ==========================================
# 4. GUI
# ==========================================

class AdvancedSVGEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SVG Standalone Flattener (v10)")
        self.root.geometry("900x600")
        self.inkscape_path = None
        self.original_svg_path = None
        self.processed_svg_path = None
        self.tk_images = []
        
        self.setup_ui()
        # UI起動後にInkscapeのチェックを走らせる
        self.root.after(100, self.check_dependencies)
        
    def setup_ui(self):
        top = tk.Frame(self.root, padx=10, pady=10)
        top.pack(side=tk.TOP, fill=tk.X)
        tk.Button(top, text="1. SVGを開く", command=self.open_svg).pack(side=tk.LEFT, padx=5)
        self.btn_proc = tk.Button(top, text="2. 変換処理", command=self.process_svg, state=tk.DISABLED)
        self.btn_proc.pack(side=tk.LEFT, padx=5)
        self.btn_save = tk.Button(top, text="3. 保存", command=self.save_svg, state=tk.DISABLED)
        self.btn_save.pack(side=tk.LEFT, padx=5)
        
        main = tk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True)
        self.canvas1 = tk.Canvas(main, bg="#333")
        self.canvas1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.canvas2 = tk.Canvas(main, bg="#333")
        self.canvas2.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=2, pady=2)

    def check_dependencies(self):
        ink = get_inkscape_command()
        if ink:
            self.inkscape_path = ink
            return
            
        if platform.system() == "Windows":
            ans = messagebox.askyesno("Inkscapeが必要です", "Inkscapeが見つかりません。自動ダウンロードを開始しますか？\n(数分かかる場合があります)")
            if ans:
                self.start_download()

    def start_download(self):
        self.dl_window = tk.Toplevel(self.root)
        self.dl_window.title("Inkscapeセットアップ")
        self.dl_window.geometry("400x150")
        self.dl_window.transient(self.root)
        self.dl_window.grab_set()

        self.lbl_status = tk.Label(self.dl_window, text="準備中...")
        self.lbl_status.pack(pady=10)

        self.progress = ttk.Progressbar(self.dl_window, orient="horizontal", length=300, mode="determinate")
        self.progress.pack(pady=10)

        # ダウンロードでUIが固まらないよう別スレッドで実行
        threading.Thread(target=self._download_task, daemon=True).start()

    def _download_task(self):
        info = INKSCAPE_URLS["Windows"]
        url = info["url"] if isinstance(info, dict) else info
        file_name = os.path.basename(urlparse(url).path) or "inkscape_installer.msi"
        local_path = os.path.abspath(file_name)

        def prog_cb(down, total):
            pct = int(down / total * 100)
            # スレッドから安全にUIを更新
            self.root.after(0, lambda: self.progress.config(value=pct))

        try:
            self.root.after(0, lambda: self.lbl_status.config(text="ダウンロード中..."))
            download_file_with_progress(url, local_path, prog_cb)
            
            self.root.after(0, lambda: self.lbl_status.config(text="インストール/展開中... (しばらくお待ちください)"))
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
                self.root.after(0, lambda: messagebox.showinfo("完了", "Inkscapeのセットアップが完了しました！"))
            else:
                self.root.after(0, lambda: messagebox.showerror("エラー", "インストールに失敗しました。手動で確認してください。"))
                
        except Exception as e:
            self.root.after(0, self.dl_window.destroy)
            self.root.after(0, lambda: messagebox.showerror("エラー", f"処理中にエラーが発生しました:\n{e}"))

    def display_svg(self, path, canvas):
        canvas.delete("all")
        try:
            if USE_CAIROSVG:
                png = cairosvg.svg2png(url=path)
                img = Image.open(io.BytesIO(png))
                img.thumbnail((400, 500))
                photo = ImageTk.PhotoImage(img)
                self.tk_images.append(photo)
                canvas.create_image(200, 300, image=photo, anchor=tk.CENTER)
            else:
                img = tksvg.SvgImage(file=path)
                self.tk_images.append(img)
                canvas.create_image(200, 300, image=img, anchor=tk.CENTER)
        except Exception as e:
            print(f"Display error: {e}")

    def open_svg(self):
        p = filedialog.askopenfilename(filetypes=[("SVG Files", "*.svg")])
        if p:
            self.original_svg_path = p
            self.display_svg(p, self.canvas1)
            self.btn_proc.config(state=tk.NORMAL)

    def process_svg(self):
        if not self.original_svg_path: return
        self.processed_svg_path = tempfile.mktemp(suffix=".svg")
        process_svg_logic(self.original_svg_path, self.processed_svg_path, self.inkscape_path)
        self.display_svg(self.processed_svg_path, self.canvas2)
        self.btn_save.config(state=tk.NORMAL)

    def save_svg(self):
        p = filedialog.asksaveasfilename(defaultextension=".svg")
        if p and self.processed_svg_path:
            shutil.copy(self.processed_svg_path, p)
            messagebox.showinfo("Success", "Saved successfully!")

if __name__ == "__main__":
    root = tk.Tk()
    app = AdvancedSVGEditorApp(root)
    root.mainloop()