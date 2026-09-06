#!/usr/bin/env python3
"""
修复 Excalidraw 脚本图标（兼容插件 >= 2.23.4）

原因：
  2.23.4 起 Tools Panel 会对脚本 SVG 做 sanitize，非 `#` 开头的 href/xlink:href
  （含 data:image/...;base64,...）会被删除。PandaScripts 里大量「Excalidraw 导出 +
  内嵌图」图标因此显示空白。

本脚本：
  1. 检测含 data:image 内嵌的 .svg
  2. data:image/svg+xml → 直接解包内层 SVG
  3. PNG/JPEG 等 → 转成纯矢量 path（优先 vtracer，否则 rect）
  4. 补齐 viewBox、去掉固定 width/height（否则 Tools Panel 里会「图标过大只露一角」）
  5. 写入 class=\"skip\"，避免主题强制改色/描边
  6. 原文件备份到同目录 _icon_backup/（仅首次覆盖前备份）

用法：
  python fix_script_icons.py
  python fix_script_icons.py --dir PandaScripts
  python fix_script_icons.py --dry-run
  python fix_script_icons.py --restore
  python fix_script_icons.py --method auto|vtracer|rect
  python fix_script_icons.py --also-tag-path

依赖：
  必需: pip install pillow
  推荐: pip install vtracer
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import shutil
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("缺少依赖: pip install pillow", file=sys.stderr)
    sys.exit(1)

try:
    import vtracer  # type: ignore

    HAS_VTRACER = True
except ImportError:
    HAS_VTRACER = False

# 匹配任意 data:image/*;base64,...
DATA_IMAGE_RE = re.compile(
    r"""((?:xlink:)?href)\s*=\s*["']data:image/([a-z0-9.+-]+);base64,([A-Za-z0-9+/=]+)["']""",
    re.IGNORECASE,
)


def find_embedded_payloads(svg_text: str) -> list[tuple[str, bytes]]:
    """返回 [(mime_subtype, raw_bytes), ...]，按出现顺序。"""
    out: list[tuple[str, bytes]] = []
    for m in DATA_IMAGE_RE.finditer(svg_text):
        subtype = m.group(2).lower()
        try:
            raw = base64.b64decode(m.group(3))
        except Exception:
            continue
        out.append((subtype, raw))
    return out


def needs_fix(svg_text: str) -> bool:
    return bool(DATA_IMAGE_RE.search(svg_text))


def is_path_icon(svg_text: str) -> bool:
    if needs_fix(svg_text):
        return False
    return bool(re.search(r"<path\b", svg_text, re.IGNORECASE))


def has_skip_or_lucide(svg_text: str) -> bool:
    m = re.search(r"""<svg\b[^>]*\bclass\s*=\s*["']([^"']*)["']""", svg_text, re.IGNORECASE)
    if not m:
        return False
    return bool(re.search(r"(?:^|\s)(?:lucide|skip)(?:\s|-|$)", m.group(1)))


def ensure_skip_class(svg_text: str) -> str:
    """给根 <svg> 补上 class=\"skip\"（已有 lucide/skip 则不动）。"""
    if has_skip_or_lucide(svg_text):
        return svg_text

    def repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        cm = re.search(r"""\bclass\s*=\s*["']([^"']*)["']""", tag, re.IGNORECASE)
        if cm:
            old = cm.group(1).strip()
            new = f"{old} skip".strip() if old else "skip"
            return re.sub(
                r"""\bclass\s*=\s*["'][^"']*["']""",
                f'class="{new}"',
                tag,
                count=1,
                flags=re.IGNORECASE,
            )
        return tag[:-1] + ' class="skip">'

    return re.sub(r"<svg\b[^>]*>", repl, svg_text, count=1, flags=re.IGNORECASE)


def strip_external_font_faces(svg_text: str) -> str:
    """去掉指向外网的 @font-face，避免无用噪音。"""
    return re.sub(
        r"<style\b[^>]*>.*?</style>",
        "",
        svg_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def strip_unsafe_hrefs(svg_text: str) -> str:
    """删除非 # 开头的 href（与插件 sanitize 行为对齐）。"""

    def repl(m: re.Match[str]) -> str:
        attr, value = m.group(1), m.group(2)
        if value.strip().startswith("#") or value.strip() == "":
            return m.group(0)
        return ""

    return re.sub(
        r"""((?:xlink:)?href)\s*=\s*["']([^"']*)["']""",
        repl,
        svg_text,
        flags=re.IGNORECASE,
    )


def ensure_viewbox(svg_text: str) -> str:
    """
    Tools Panel 用 CSS 把 svg 缩到按钮尺寸；没有 viewBox 时内部坐标不会缩放，
    看起来就像「图标过大、只露出一角」。
    规范：补齐 viewBox，去掉固定 width/height（交给 CSS）。
    """

    def repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        vb = re.search(r"""\bviewBox\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
        width_m = re.search(r"""\bwidth\s*=\s*["']([\d.]+)(?:px)?["']""", tag, re.IGNORECASE)
        height_m = re.search(r"""\bheight\s*=\s*["']([\d.]+)(?:px)?["']""", tag, re.IGNORECASE)

        if vb:
            view_box = " ".join(vb.group(1).split())
        elif width_m and height_m:
            view_box = f"0 0 {width_m.group(1)} {height_m.group(1)}"
        else:
            # 兜底：常见图标画布
            view_box = "0 0 128 128"

        # 清掉固定宽高，避免按像素撑破按钮
        tag = re.sub(r"""\s*\bwidth\s*=\s*["'][^"']*["']""", "", tag, flags=re.IGNORECASE)
        tag = re.sub(r"""\s*\bheight\s*=\s*["'][^"']*["']""", "", tag, flags=re.IGNORECASE)

        if re.search(r"""\bviewBox\s*=""", tag, re.IGNORECASE):
            tag = re.sub(
                r"""\bviewBox\s*=\s*["'][^"']*["']""",
                f'viewBox="{view_box}"',
                tag,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            tag = tag[:-1] + f' viewBox="{view_box}">'
        return tag

    return re.sub(r"<svg\b[^>]*>", repl, svg_text, count=1, flags=re.IGNORECASE)


def finalize_svg(svg_text: str) -> str:
    """规范化输出：补 viewBox、补 skip、去掉危险 href / 外网字体。"""
    svg_text = svg_text.strip()
    if not svg_text.lower().startswith("<svg") and "<svg" in svg_text.lower():
        # 可能带 xml 声明
        idx = svg_text.lower().find("<svg")
        svg_text = svg_text[idx:]
    svg_text = strip_external_font_faces(svg_text)
    # 递归：若仍有 data:image，继续处理一次（解包后仍可能嵌套）
    if needs_fix(svg_text):
        # 避免死循环：把 data:image 属性整段删掉前，尝试优先解包 svg+xml
        payloads = find_embedded_payloads(svg_text)
        for subtype, raw in payloads:
            if subtype in {"svg+xml", "svg"}:
                try:
                    return finalize_svg(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    pass
        svg_text = DATA_IMAGE_RE.sub("", svg_text)
    svg_text = strip_unsafe_hrefs(svg_text)
    svg_text = ensure_viewbox(svg_text)
    svg_text = ensure_skip_class(svg_text)
    if not svg_text.endswith("\n"):
        svg_text += "\n"
    return svg_text


def pick_primary_raster(payloads: list[tuple[str, bytes]]) -> Image.Image:
    best: Image.Image | None = None
    best_area = -1
    for subtype, raw in payloads:
        if subtype in {"svg+xml", "svg"}:
            continue
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception:
            continue
        area = im.width * im.height
        if area > best_area:
            best = im
            best_area = area
    if best is None:
        raise ValueError("无法解码内嵌位图")
    return best


def trim_transparent(im: Image.Image, pad: int = 2) -> Image.Image:
    bbox = im.getchannel("A").getbbox()
    if not bbox:
        return im
    l, t, r, b = bbox
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(im.width, r + pad)
    b = min(im.height, b + pad)
    return im.crop((l, t, r, b))


def image_to_svg_vtracer(im: Image.Image) -> str:
    im = trim_transparent(im)
    # 过大图缩小，加快追踪并减小文件
    max_side = 256
    w, h = im.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        im = im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    svg = vtracer.convert_raw_image_to_svg(
        buf.getvalue(),
        img_format="png",
        colormode="color",
        hierarchical="stacked",
        mode="spline",
        filter_speckle=4,
        color_precision=6,
        layer_difference=16,
        corner_threshold=60,
        length_threshold=4.0,
        max_iterations=10,
        splice_threshold=45,
        path_precision=3,
    )
    return finalize_svg(svg)


def _color_key(rgba: tuple[int, int, int, int], q: int = 32) -> tuple[int, int, int] | None:
    r, g, b, a = rgba
    if a < 96:  # 丢掉半透明锯齿边
        return None
    return (r // q * q, g // q * q, b // q * q)


def image_to_svg_rects(im: Image.Image, max_side: int = 96) -> str:
    """无 vtracer 时的回退：合并同色像素为 rect。"""
    im = trim_transparent(im)
    w, h = im.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        im = im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.NEAREST,
        )
        w, h = im.size

    pixels = im.load()
    rects: list[tuple[int, int, int, int, tuple[int, int, int]]] = []
    for y in range(h):
        x = 0
        while x < w:
            key = _color_key(pixels[x, y])
            if key is None:
                x += 1
                continue
            x0 = x
            x += 1
            while x < w and _color_key(pixels[x, y]) == key:
                x += 1
            rects.append((x0, y, x - x0, 1, key))

    rects.sort(key=lambda t: (t[4], t[0], t[1]))
    merged: list[tuple[int, int, int, int, tuple[int, int, int]]] = []
    for r in rects:
        if (
            merged
            and merged[-1][4] == r[4]
            and merged[-1][0] == r[0]
            and merged[-1][2] == r[2]
            and merged[-1][1] + merged[-1][3] == r[1]
        ):
            x, y, rw, rh, c = merged[-1]
            merged[-1] = (x, y, rw, rh + r[3], c)
        else:
            merged.append(r)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" class="skip">',
    ]
    for x, y, rw, rh, (cr, cg, cb) in merged:
        parts.append(
            f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}" fill="#{cr:02x}{cg:02x}{cb:02x}"/>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def convert_raster(im: Image.Image, method: str) -> str:
    method = method.lower()
    if method == "auto":
        method = "vtracer" if HAS_VTRACER else "rect"
    if method == "vtracer":
        if not HAS_VTRACER:
            raise RuntimeError("未安装 vtracer，请 pip install vtracer 或改用 --method rect")
        return image_to_svg_vtracer(im)
    if method == "rect":
        return finalize_svg(image_to_svg_rects(im))
    raise ValueError(f"未知 method: {method}")


def convert_svg_text(svg_text: str, method: str) -> tuple[str, str]:
    """
    返回 (new_svg, how)
    how: unwrap-svg | vtracer | rect
    """
    payloads = find_embedded_payloads(svg_text)
    if not payloads:
        raise ValueError("未找到 data:image 载荷")

    # 优先解包 svg+xml（质量最好、文件也最小）
    svg_payloads = [(s, r) for s, r in payloads if s in {"svg+xml", "svg"}]
    if svg_payloads:
        # 取最大的内层
        raw = max(svg_payloads, key=lambda x: len(x[1]))[1]
        inner = raw.decode("utf-8", errors="ignore")
        return finalize_svg(inner), "unwrap-svg"

    im = pick_primary_raster(payloads)
    if method == "auto":
        used = "vtracer" if HAS_VTRACER else "rect"
    else:
        used = method
    return convert_raster(im, method), used


def backup_once(path: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / path.name
    if not dest.exists():
        # 优先保留「真正原始」备份；若已有则不覆盖
        shutil.copy2(path, dest)


def process_file(
    path: Path,
    *,
    method: str,
    dry_run: bool,
    backup_dir: Path,
    also_tag_path: bool,
) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    original_backup = backup_dir / path.name

    # 若有原始备份且含内嵌图，始终从备份 reconvert（方便反复跑脚本升级算法）
    source_text = text
    source_from_backup = False
    if original_backup.exists():
        bak = original_backup.read_text(encoding="utf-8", errors="ignore")
        if needs_fix(bak):
            source_text = bak
            source_from_backup = True

    if needs_fix(source_text):
        new_svg, how = convert_svg_text(source_text, method)
        if dry_run:
            return f"would-fix:{how}"
        if not source_from_backup:
            backup_once(path, backup_dir)
        path.write_text(new_svg, encoding="utf-8", newline="\n")
        return f"fixed:{how}"

    if also_tag_path and is_path_icon(text) and not has_skip_or_lucide(text):
        new_svg = ensure_skip_class(text)
        if new_svg != text:
            if dry_run:
                return "would-tag:skip"
            backup_once(path, backup_dir)
            path.write_text(new_svg, encoding="utf-8", newline="\n")
            return "tagged:skip"

    if is_path_icon(text):
        return "ok:path"
    return "ok:other"


def restore_from_backup(target_dir: Path, backup_dir: Path) -> int:
    if not backup_dir.is_dir():
        print(f"无备份目录: {backup_dir}")
        return 0
    n = 0
    for bak in backup_dir.glob("*.svg"):
        dest = target_dir / bak.name
        shutil.copy2(bak, dest)
        print(f"  restored: {dest.name}")
        n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 Excalidraw 脚本 SVG 图标空白问题")
    parser.add_argument("--dir", default="PandaScripts", help="脚本目录（默认 PandaScripts）")
    parser.add_argument(
        "--method",
        choices=("auto", "vtracer", "rect"),
        default="auto",
        help="位图转矢量方式（svg+xml 会直接解包，不受此项影响）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写文件")
    parser.add_argument("--restore", action="store_true", help="从 _icon_backup 还原")
    parser.add_argument(
        "--also-tag-path",
        action="store_true",
        help="给已是 path 的图标补 class=skip",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    target = (root / args.dir).resolve()
    if not target.is_dir():
        print(f"目录不存在: {target}", file=sys.stderr)
        return 1

    backup_dir = target / "_icon_backup"

    if args.restore:
        n = restore_from_backup(target, backup_dir)
        print(f"已还原 {n} 个文件")
        return 0

    if args.method == "auto":
        effective = "vtracer" if HAS_VTRACER else "rect"
    else:
        effective = args.method

    print(f"目录: {target}")
    print(f"方法: {args.method} -> {effective}" + (" (dry-run)" if args.dry_run else ""))
    if effective == "rect" and not HAS_VTRACER:
        print("提示: 安装 vtracer 可得到更平滑矢量: pip install vtracer")

    stats: dict[str, int] = {}
    files = sorted(target.glob("*.svg"))
    if not files:
        print("未找到 .svg 文件")
        return 0

    for path in files:
        try:
            status = process_file(
                path,
                method=args.method,
                dry_run=args.dry_run,
                backup_dir=backup_dir,
                also_tag_path=args.also_tag_path,
            )
        except Exception as e:
            status = f"error:{e}"
        key = status.split(":")[0]
        stats[key] = stats.get(key, 0) + 1
        print(f"  [{status}] {path.name}")

    print("---")
    for k, v in sorted(stats.items()):
        print(f"{k}: {v}")
    if not args.dry_run and stats.get("fixed"):
        print(f"原始备份: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
