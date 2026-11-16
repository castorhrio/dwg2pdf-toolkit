# -*- coding: utf-8 -*-
"""dwg2pdf — 把整版 CAD 图纸(DWG/DXF)转换为按图框分页、中文清晰的矢量 PDF。

流程: DWG ->(ODA File Converter)-> DXF ->(ezdxf 解析) 图框识别/字体修复 ->
      (ezdxf drawing + PyMuPDF 后端) 逐页矢量渲染 -> 合并 PDF。

用法:
    python dwg2pdf.py 图纸.dwg [-o 输出.pdf] [--oda ODAFileConverter.exe路径]
                        [--keep-rotation] [--no-extra-pages] [--mono]

依赖: pip install ezdxf pymupdf
ODA File Converter 免费官方下载(仅 DWG 输入需要): https://www.opendesign.com/guestfiles/oda_file_converter
首次使用未指定 --oda 时会自动下载并以便携方式解压到 %LOCALAPPDATA%/dwg2pdf/oda，不安装到系统。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict

import ezdxf
from ezdxf import bbox
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing import layout as L
from ezdxf.addons.drawing import pipeline as PL
from ezdxf.addons.drawing.config import (BackgroundPolicy, ColorPolicy,
                                        Configuration, TextPolicy)
from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
from ezdxf.math import Matrix44, Vec2
import pymupdf

ODA_MSI_URL = ("https://www.opendesign.com/guestfiles/get?"
               "filename=ODAFileConverter_QT6_vc16_amd64dll.msi")
# 标准纸幅 (mm): 短边 x 长边
PAPERS = [(210, 297), (297, 420), (420, 594), (594, 841), (841, 1189)]
TOL = 0.01  # 图框尺寸匹配容差 1%


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- ODA 转换
def find_oda(explicit):
    if explicit:
        if os.path.exists(explicit):
            return explicit
        sys.exit("指定的 ODA 转换器不存在: %s" % explicit)
    env = os.environ.get("DWG2PDF_ODA")
    if env and os.path.exists(env):
        return env
    cache = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "dwg2pdf", "oda")
    exe = os.path.join(cache, "ODAFileConverter.exe")
    if os.path.exists(exe):
        return exe
    for root in (r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA"):
        for dirpath, _, files in os.walk(root):
            if "ODAFileConverter.exe" in files:
                return os.path.join(dirpath, "ODAFileConverter.exe")
    log("未找到 ODA File Converter，开始下载官方便携包…")
    os.makedirs(cache, exist_ok=True)
    msi = os.path.join(cache, "oda.msi")
    req = urllib.request.Request(ODA_MSI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(msi, "wb") as f:
        shutil.copyfileobj(r, f)
    subprocess.run(["msiexec", "/a", msi, "/qn", "TARGETDIR=" + cache],
                   check=True, timeout=600)
    if not os.path.exists(exe):
        sys.exit("ODA 便携解压失败，请手动安装后用 --oda 指定路径")
    return exe


def dwg_to_dxf(dwg, oda_exe, workdir):
    src = os.path.join(workdir, "in")
    dst = os.path.join(workdir, "out")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    # 纯 ASCII 文件名，避免转换器对中文路径/文件名的兼容问题
    tmp = os.path.join(src, "input.dwg")
    shutil.copyfile(dwg, tmp)
    subprocess.run([oda_exe, src, dst, "ACAD2018", "DXF", "0", "1", "*.DWG"],
                   check=True, timeout=1800)
    out = os.path.join(dst, "input.dxf")
    if not os.path.exists(out):
        sys.exit("ODA 转换未生成 DXF，请检查 DWG 是否损坏")
    return out


# ---------------------------------------------------------------- 图框识别
def layer_colors(doc):
    return {l.dxf.name: l.dxf.color for l in doc.layers}


def entity_color(e, lay):
    c = e.dxf.get("color", 256)
    return lay.get(e.dxf.layer, 7) if c == 256 else c


def detect_frames(doc):
    """返回 [(x0,y0,x1,y1), ...] 外层图框矩形（模型坐标）。"""
    lay = layer_colors(doc)
    rects = []
    for e in doc.modelspace():
        if e.dxftype() == "LWPOLYLINE" and e.closed and entity_color(e, lay) == 4:
            pts = list(e.get_points("xy"))
            if len(pts) > 8:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w > 1000 and h > 1000 and max(w / h, h / w) <= 4:
                rects.append((min(xs), min(ys), max(xs), max(ys)))
    # 图块形式的图框：块内含>=2条青色长线，按插入点展开
    blocks = {}
    for b in doc.blocks:
        if b.name.startswith("*"):
            continue
        xs, ys, n = [], [], 0
        for e in b:
            if e.dxftype() == "LINE" and e.dxf.get("color", 256) == 4:
                s, t = e.dxf.start, e.dxf.end
                if abs(s.x - t.x) > 8000 or abs(s.y - t.y) > 8000:
                    n += 1
                xs += [s.x, t.x]
                ys += [s.y, t.y]
        if n >= 2 and xs:
            blocks[b.name] = (min(xs), min(ys), max(xs), max(ys))
    for e in doc.modelspace():
        if e.dxftype() == "INSERT" and e.dxf.name in blocks:
            x0, y0, x1, y1 = blocks[e.dxf.name]
            m = (Matrix44.translate(e.dxf.insert.x, e.dxf.insert.y, 0)
                 @ Matrix44.z_rotate(e.dxf.rotation * 3.141111111189793 / 180)
                 @ Matrix44.scale(e.dxf.xscale, e.dxf.yscale, 1))
            cs = [m.transform(Vec2(x, y)) for x in (x0, x1) for y in (y0, y1)]
            r = (min(c.x for c in cs), min(c.y for c in cs),
                 max(c.x for c in cs), max(c.y for c in cs))
            w, h = r[2] - r[0], r[3] - r[1]
            if max(w / h, h / w) <= 4:
                rects.append(r)
    # 去嵌套：保留外层
    outer = []
    for r in rects:
        if not any(o != r and o[0] <= r[0] + 1 and o[1] <= r[1] + 1
                   and o[2] >= r[2] - 1 and o[3] >= r[3] - 1 for o in rects):
            outer.append(r)
    return outer


def match_paper(w, h):
    """按 1% 容差把图框尺寸匹配到标准纸幅(尝试 mm/cm/0.01mm/m 单位)。"""
    for u in (100, 1, 10, 1000):
        for a, b in PAPERS:
            for pw, ph in ((a * u, b * u), (b * u, a * u)):
                if abs(w - pw) <= TOL * pw and abs(h - ph) <= TOL * ph:
                    return (pw / u, ph / u)  # mm
    return None


def frame_pages(frames):
    pages = []
    unmatched = []
    for r in frames:
        w, h = r[2] - r[0], r[3] - r[1]
        paper = match_paper(w, h)
        if paper is None:
            unmatched.append(r)
            continue
        pages.append((r, paper))
    # 未匹配标准纸幅的图框：按宽高比选最接近纸幅，避免漏图
    for r in unmatched:
        w, h = r[2] - r[0], r[3] - r[1]
        ratio = w / h
        best = min(PAPERS, key=lambda p: min(abs(ratio - p[0] / p[1]),
                                             abs(ratio - p[1] / p[0])))
        paper = best if ratio < 1 else (best[1], best[0])
        pages.append((r, paper))
    return pages


# ---------------------------------------------------------------- 额外内容页
def extra_pages(msp, bbs, frames, giant_thr):
    """图框之外的内容(封面/目录/说明/图表)聚簇成附加页。"""
    uncovered = []
    for i, b in enumerate(bbs):
        if b is None:
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if any(f[0] <= cx <= f[2] and f[1] <= cy <= f[3] for f in frames):
            continue
        uncovered.append(i)
    pad = 3000
    clusters = []
    for i in uncovered:
        b = bbs[i]
        r = [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
        hit = None
        for c in clusters:
            if not (r[2] < c["r"][0] or r[0] > c["r"][2]
                    or r[3] < c["r"][1] or r[1] > c["r"][3]):
                hit = c
                break
        if hit is None:
            clusters.append({"r": r, "idx": [i]})
        else:
            hit["r"] = [min(hit["r"][0], r[0]), min(hit["r"][1], r[1]),
                        max(hit["r"][2], r[2]), max(hit["r"][3], r[3])]
            hit["idx"].append(i)
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i]["r"], clusters[j]["r"]
                if not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3]):
                    clusters[i]["r"] = [min(a[0], b[0]), min(a[1], b[1]),
                                        max(a[2], b[2]), max(a[3], b[3])]
                    clusters[i]["idx"] += clusters[j]["idx"]
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break
    # 间距<=20000 的簇二次合并(同一段说明/同一张表不拆页)
    gap = 20000
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i]["r"], clusters[j]["r"]
                if (a[0] - gap <= b[2] and b[0] - gap <= a[2]
                        and a[1] - gap <= b[3] and b[1] - gap <= a[3]):
                    clusters[i]["r"] = [min(a[0], b[0]), min(a[1], b[1]),
                                        max(a[2], b[2]), max(a[3], b[3])]
                    clusters[i]["idx"] += clusters[j]["idx"]
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break
    pages = []
    for c in clusters:
        idx = c["idx"]
        if len(idx) < 2:
            continue  # 游离单实体(轴号标签/分隔线/杂点)不成页
        heights = [msp[i].dxf.get("height", 0) for i in idx
                   if msp[i].dxftype() in ("TEXT", "MTEXT")]
        all_giant = bool(heights) and all(h > giant_thr for h in heights)
        keep = [i for i in idx if all_giant
                or not (msp[i].dxftype() in ("TEXT", "MTEXT")
                        and msp[i].dxf.get("height", 0) > giant_thr)]
        if not keep:
            continue
        # 过滤杂散标注簇：小簇且文字仅为短标签(面积/标高/编号/房间)不成页；
        # 含较长中文句子(说明/注释)的小簇保留
        texts = [(msp[i].dxf.text if msp[i].dxftype() == "TEXT" else msp[i].text).strip()
                 for i in keep if msp[i].dxftype() in ("TEXT", "MTEXT")]
        if len(keep) < 150 and texts:
            long_cjk = any(has_cjk(t) and len(t) >= 8 for t in texts)
            short_labels = all(len(t) <= 10 and
                               (not has_cjk(t) or t in ("房间", "屋面", "露台"))
                               for t in texts)
            if short_labels and not long_cjk and len(keep) < 200:
                continue
        bb = [bbs[i] for i in keep]
        r = (min(b[0] for b in bb), min(b[1] for b in bb),
             max(b[2] for b in bb), max(b[3] for b in bb))
        w, h = r[2] - r[0], r[3] - r[1]
        ratio = w / h
        best = min(PAPERS, key=lambda p: min(abs(ratio - p[0] / p[1]),
                                             abs(ratio - p[1] / p[0])))
        paper = best if ratio < 1 else (best[1], best[0])
        pages.append((r, paper, keep))
    return pages


# ---------------------------------------------------------------- 字体/旋转
def has_cjk(t):
    return any("一" <= ch <= "鿿" for ch in t)


def fix_fonts(doc):
    cjk = set()

    def scan(container):
        for e in container:
            t = e.dxftype()
            if t in ("TEXT", "ATTRIB", "ATTDEF") and has_cjk(e.dxf.text):
                cjk.add(e.dxf.style)
            elif t == "MTEXT" and has_cjk(e.text):
                cjk.add(e.dxf.style)
    scan(doc.modelspace())
    for b in doc.blocks:
        scan(b)
    n = 0
    for s in doc.styles:
        f = (s.dxf.font or "").lower()
        bf = (s.dxf.bigfont or "").lower()
        if "hei" in f:
            continue
        if f.endswith(".shx") or bf or not f or s.dxf.name in cjk:
            s.dxf.font = "simsun.ttc"
            s.dxf.bigfont = ""
            n += 1
    return n


def normalize_rotation(doc):
    """图框块以270°插入时，块内文字+90°抵消、顶层270°文字转正（几何不变）。"""
    rot_blocks = set()
    for e in doc.modelspace():
        if e.dxftype() == "INSERT" and abs(e.dxf.rotation - 270) < 1:
            rot_blocks.add(e.dxf.name)
    for name in rot_blocks:
        blk = doc.blocks.get(name)
        if blk is None:
            continue
        for e in blk:
            if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                e.dxf.rotation = (e.dxf.get("rotation", 0) + 90) % 360
    for e in doc.modelspace():
        if e.dxftype() in ("TEXT", "MTEXT") and abs(e.dxf.get("rotation", 0) - 270) < 1:
            e.dxf.rotation = 0


def patch_darken():
    orig = PL.apply_color_policy

    def darken(color, policy, custom):
        c = orig(color, policy, custom)
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if lum > 150:
            f = 150.0 / lum
            return "#%02x%02x%02x%s" % (int(r * f), int(g * f), int(b * f), c[7:9])
        return c
    PL.apply_color_policy = darken


# ---------------------------------------------------------------- 渲染
def select_idx(msp, bbs, frame, giant_thr):
    x0, y0, x1, y1 = frame
    w, h = x1 - x0, y1 - y0
    hx = 0.5 * w
    vy = 0.005 * h
    ex0, ey0, ex1, ey1 = x0 - hx, y0 - vy, x1 + hx, y1 + vy
    idx = []
    small_boxes = []
    # 图框外孤立房间标注框：小型多段线且内部含"房间/屋面"等短标签文字
    room_texts = []
    for i, b in enumerate(bbs):
        if b is None or msp[i].dxftype() not in ("TEXT", "MTEXT"):
            continue
        t = (msp[i].dxf.text if msp[i].dxftype() == "TEXT" else msp[i].text).strip()
        if t in ("房间", "屋面", "露台", "阳台"):
            room_texts.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
    for i, b in enumerate(bbs):
        if b is None or msp[i].dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        bx0, by0, bx1, by1 = b
        bw, bh = bx1 - bx0, by1 - by0
        if not (500 < bw < 0.2 * w and 500 < bh < 0.2 * h):
            continue
        if bx1 >= x0 - 1000 and bx0 <= x1 + 1000:  # 只处理图框外侧，图内楼梯间框不动
            continue
        small_boxes.append(b)
    for i, b in enumerate(bbs):
        if b is None:
            continue
        bx0, by0, bx1, by1 = b
        if bx1 < x0 - hx or bx0 > x1 + hx or by1 < y0 - vy or by0 > y1 + vy:
            continue
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        if not (ex0 <= cx <= ex1 and ey0 <= cy <= ey1):
            continue
        bw, bh = bx1 - bx0, by1 - by0
        if bw > 0.2 * w and bh > 0.2 * h:
            if not (y0 <= cy <= y1):
                continue
        if bh > 2 * h and bw < 0.01 * w:
            continue
        if bw > 2 * w and bh < 0.01 * h:
            continue
        # 超大编号标签(B8/C3等)不参与图框页版面，图号已在标题栏
        if msp[i].dxftype() in ("TEXT", "MTEXT") and msp[i].dxf.get("height", 0) > giant_thr:
            continue
        # 图框外超大面积数字(字高>5%图框高)不参与版面
        if msp[i].dxftype() in ("TEXT", "MTEXT") and msp[i].dxf.get("height", 0) > 0.05 * h \
                and (bx1 < x0 or bx0 > x1):
            continue
        # 孤立小房间标注框(宽<12%图框宽的闭合多段线)不参与版面
        if msp[i].dxftype() == "LWPOLYLINE" and msp[i].closed and bw < 0.12 * w and bh < 0.12 * h:
            small_boxes.append(b)
            continue
        idx.append(i)
    # 小标注框的边线与框内文字一并排除
    if small_boxes:
        def in_small(b):
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            return any(sb[0] - 500 <= cx <= sb[2] + 500
                       and sb[1] - 500 <= cy <= sb[3] + 500 for sb in small_boxes)
        idx = [i for i in idx if not (
            msp[i].dxftype() in ("TEXT", "MTEXT", "LINE", "LWPOLYLINE", "POLYLINE")
            and in_small(bbs[i]))]
    return idx


def render(pages, doc, msp, bbs, dst, giant, mono=False):
    patch_darken()
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE,
                        color_policy=(ColorPolicy.MONOCHROME_LIGHT_BG if mono
                                      else ColorPolicy.COLOR),
                        text_policy=TextPolicy.FILLING)
    ctx = RenderContext(doc)
    out = pymupdf.open()
    settings = L.Settings(fit_page=True, crop_at_margins=False)
    for item in pages:
        frame, paper = item[0], item[1]
        idx = item[2] if len(item) > 2 else select_idx(msp, bbs, frame, giant)
        sel = [msp[i] for i in idx]
        if not sel:
            continue
        bb = [bbs[i] for i in idx]
        ux0, uy0 = min(b[0] for b in bb), min(b[1] for b in bb)
        ux1, uy1 = max(b[2] for b in bb), max(b[3] for b in bb)
        m = 0.015 * max(ux1 - ux0, uy1 - uy0)
        rb = L.BoundingBox2d([Vec2(ux0 - m, uy0 - m), Vec2(ux1 + m, uy1 + m)])
        W, H = paper
        page = L.Page(width=W, height=H, margins=L.Margins(10, 10, 10, 10))
        be = PyMuPdfBackend()
        fe = Frontend(ctx, be, config=cfg)
        fe.set_background(ctx.current_layout_properties.background_color)
        fe.draw_entities(sel)
        data = be.get_pdf_bytes(page, settings=settings, render_box=rb)
        src = pymupdf.open(stream=data, filetype="pdf")
        out.insert_pdf(src)
        src.close()
    if len(out) == 0:
        sys.exit("没有可渲染的内容")
    out.save(dst, garbage=3, deflate=True)
    return len(out)


def main():
    ap = argparse.ArgumentParser(description="整版CAD图纸 -> 按图框分页的清晰矢量PDF")
    ap.add_argument("input", help="DWG 或 DXF 文件路径")
    ap.add_argument("-o", "--output", help="输出 PDF 路径(默认: 输入名_分页清晰版.pdf)")
    ap.add_argument("--oda", help="ODAFileConverter.exe 路径(仅 DWG 输入需要)")
    ap.add_argument("--keep-rotation", action="store_true",
                    help="保持原始文字旋转排布，不做标题栏转正")
    ap.add_argument("--no-extra-pages", action="store_true",
                    help="只输出图框页，不附加封面/目录/说明等框外内容页")
    ap.add_argument("--mono", action="store_true", help="浅底单色(灰度)输出")
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        sys.exit("输入文件不存在: %s" % src)
    dst = args.output or os.path.splitext(src)[0] + "_分页清晰版.pdf"
    work = os.path.join(os.environ.get("TEMP", "."), "dwg2pdf_work")
    os.makedirs(work, exist_ok=True)

    t0 = time.time()
    if src.lower().endswith(".dwg"):
        dxf = dwg_to_dxf(src, find_oda(args.oda), work)
    else:
        dxf = src
    doc = ezdxf.readfile(dxf)
    log("DXF 实体读取完成 %.1fs" % (time.time() - t0))

    n = fix_fonts(doc)
    log("修复中文字体样式 %d 个" % n)
    if not args.keep_rotation:
        normalize_rotation(doc)

    msp = list(doc.modelspace())
    cache = bbox.Cache()
    bbs = []
    for e in msp:
        try:
            b = bbox.extents([e], cache=cache)
            bbs.append((b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
                       if b.has_data else None)
        except Exception:
            bbs.append(None)

    frames = detect_frames(doc)
    log("识别图框 %d 个" % len(frames))
    short = min((min(f[2] - f[0], f[3] - f[1]) for f in frames), default=30000)
    giant = 0.2 * short
    pages = [(r, p) for r, p in frame_pages(frames)]
    pages.sort(key=lambda t: (0 if t[0][0] < (min(f[0] for f in frames)
                                              + max(f[2] for f in frames)) / 2
                              else 1, -t[0][1]))
    if not args.no_extra_pages and frames:
        ymax = max(f[3] for f in frames)
        ymin = min(f[1] for f in frames)
        ex = extra_pages(msp, bbs, frames, giant)
        above = [p for p in ex if p[0][1] > ymax]   # 封面/目录/说明
        below = [p for p in ex if p[0][3] < ymin]   # 索引/图表
        other = [p for p in ex if p not in above and p not in below]
        above.sort(key=lambda p: (p[0][0], -p[0][1]))
        below.sort(key=lambda p: (p[0][0], -p[0][1]))
        pages = above + pages + other + below
    if not pages:  # 无图框：整幅一页
        bb = [b for b in bbs if b]
        r = (min(b[0] for b in bb), min(b[1] for b in bb),
             max(b[2] for b in bb), max(b[3] for b in bb))
        pages = [(r, (594, 841) if r[3] - r[1] > r[2] - r[0] else (841, 594))]
    log("总页数 %d，开始渲染…" % len(pages))
    n = render(pages, doc, msp, bbs, dst, giant, mono=args.mono)
    log("完成: %s (%d 页, %.1f MB, %.1fs)"
        % (dst, n, os.path.getsize(dst) / 1e6, time.time() - t0))


if __name__ == "__main__":
    main()
