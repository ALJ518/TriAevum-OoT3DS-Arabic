import argparse
import hashlib
import json
import os
import shutil
import struct
import tempfile
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog
from typing import Mapping
game_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
try:
    from PIL import Image, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

INVALID = 0xFFFFFFFF
PAYLOAD_ALIGNMENT = 16
MAXIMUM_MEMBER_BYTES = 64 * 1024 * 1024
UPSCALE_FACTOR = 4


# تقريب الإزاحة أو الحجم إلى مضاعفات 16 بايت لتوافق صيغة O3RO
def align(value: int, alignment: int = PAYLOAD_ALIGNMENT) -> int:
    return (value + alignment - 1) // alignment * alignment


# حساب بصمة SHA-256 لبيانات في الذاكرة
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# حساب بصمة SHA-256 لملف على القرص لتحديث سجل التحقق في Forge
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# استخراج موضع وحجم الـ RomFS الأصلي من ملف process-manifest.json
def romfs_service_view(process_manifest: Path) -> tuple[int, int]:
    data = json.loads(process_manifest.read_text(encoding="utf-8"))
    source = data.get("source", {})
    offset = source.get("romfs_service_offset")
    size = source.get("romfs_service_size")
    if not isinstance(offset, int) or not isinstance(size, int) or offset < 0 or size <= 0:
        raise ValueError("The process manifest has no RomFS service view")
    return offset, size


# تحليل جدول المجلدات والملفات داخل صورة RomFS الأصلية وإرجاع مواقعها وأحجامها
def romfs_file_records(stream, base: int, size: int) -> tuple[tuple[int, ...], dict[str, tuple[int, int, int]]]:
    def read(offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError("RomFS extent outside the service view")
        stream.seek(base + offset)
        data = stream.read(length)
        if len(data) != length:
            raise ValueError("Truncated RomFS")
        return data

    header = struct.unpack("<10I", read(0, 40))
    if header[0] != 40:
        raise ValueError("Invalid RomFS Level-3 header")
    for offset, length in ((header[1], header[2]), (header[3], header[4]),
                           (header[5], header[6]), (header[7], header[8])):
        if offset % 4 or not 0 < length <= 16 * 1024 * 1024 or offset + length > size:
            raise ValueError("Invalid RomFS metadata bounds")
    if header[9] > size:
        raise ValueError("Invalid RomFS data offset")
    directories, files = read(header[3], header[4]), read(header[7], header[8])

    def name_of(table: bytes, offset: int, fixed: int) -> str:
        if offset % 4 or offset + fixed > len(table):
            raise ValueError("Invalid RomFS record")
        length = struct.unpack_from("<I", table, offset + fixed - 4)[0]
        if length % 2 or offset + fixed + length > len(table):
            raise ValueError("Invalid RomFS name")
        name = table[offset + fixed:offset + fixed + length].decode("utf-16-le")
        if "/" in name or "\\" in name or "\0" in name or name in (".", ".."):
            raise ValueError("Invalid RomFS name")
        return name

    records: dict[str, tuple[int, int, int]] = {}
    visited_directories: set[int] = set()
    visited_files: set[int] = set()
    pending = [(0, "")]
    while pending:
        offset, prefix = pending.pop()
        if offset in visited_directories:
            raise ValueError("Cyclic RomFS directory")
        visited_directories.add(offset)
        name = name_of(directories, offset, 24)
        _parent, sibling, child, file, _chain, _ = struct.unpack_from("<6I", directories, offset)
        path = f"{prefix}/{name}" if prefix and name else (name or prefix)
        if sibling != INVALID:
            pending.append((sibling, prefix))
        if child != INVALID:
            pending.append((child, path))
        while file != INVALID:
            if file in visited_files:
                raise ValueError("Cyclic RomFS file")
            visited_files.add(file)
            text = name_of(files, file, 32)
            owner, following, payload, length, _chain, _ = struct.unpack_from("<IIQQII", files, file)
            if owner != offset or header[9] + payload + length > size:
                raise ValueError("Invalid RomFS file record")
            full = f"{path}/{text}" if path else text
            if full in records:
                raise ValueError("Duplicate RomFS path")
            records[full] = (file, payload, length)
            file = following
    return header, records


# بناء ملف romfs_overlay.o3ro عبر إلحاق ملفات التعريب في النهاية وتعديل مؤشرات الجدول فقط
def build_romfs_overlay(
    romfs: Path,
    *,
    service_offset: int,
    service_size: int,
    replacements: Mapping[str, Path],
    output: Path,
) -> dict:
    if not replacements:
        raise ValueError("A RomFS overlay needs at least one replaced file")
    with romfs.open("rb") as stream:
        if service_offset < 0 or service_size <= 0 or service_offset + service_size > romfs.stat().st_size:
            raise ValueError("The RomFS service view is outside its image")
        header, records = romfs_file_records(stream, service_offset, service_size)
        patches = []
        tail_layout = []
        cursor = service_size
        for path in sorted(replacements):
            if path not in records:
                raise ValueError(f"The RomFS has no {path} to replace")
            record, _payload, _length = records[path]
            source = replacements[path]
            length = source.stat().st_size
            if not 0 < length <= MAXIMUM_MEMBER_BYTES:
                raise ValueError(f"Invalid replacement size for {path}")
            cursor = align(cursor)
            field = header[7] + record + 8
            stream.seek(service_offset + field)
            expected = stream.read(16)
            patches.append((field, expected, struct.pack("<QQ", cursor - header[9], length)))
            tail_layout.append((cursor, source, length))
            cursor += length
    patches.sort()
    view_size = cursor
    table_bytes = 64 + 32 * len(patches)
    data_offset = table_bytes
    tail_offset = table_bytes + sum(len(expected) + len(new) for _, expected, new in patches)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(struct.pack("<4sIQQIIQ24x", b"O3RO", 1, service_size, view_size,
                                     len(patches), 0, tail_offset))
            for field, expected, new in patches:
                stream.write(struct.pack("<QIIQQ", field, len(expected), 0,
                                         data_offset, data_offset + len(expected)))
                data_offset += len(expected) + len(new)
            for _field, expected, new in patches:
                stream.write(expected)
                stream.write(new)
            position = service_size
            for view_offset, source, length in tail_layout:
                stream.write(bytes(view_offset - position))
                with source.open("rb") as payload:
                    shutil.copyfileobj(payload, stream, 4 * 1024 * 1024)
                position = view_offset + length
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != tail_offset + (view_size - service_size):
            raise ValueError("The RomFS overlay was not written completely")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"files": len(patches), "base_view_bytes": service_size, "appended_bytes": view_size - service_size}


# معادلة Catmull-Rom لحساب الاستيفاء ثنائي التكعيب عند تكبير الحروف
def catmull_rom(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )


# تكبير صورة الحرف بالبايثون الصافي مع زيادة الحدة عند عدم توفر مكتبة Pillow
def upscale_alpha_pure(pixels: bytes, width: int, height: int, scale: int) -> bytes:
    new_w = width * scale
    new_h = height * scale

    def sample(x: int, y: int) -> float:
        cx = max(0, min(width - 1, x))
        cy = max(0, min(height - 1, y))
        return float(pixels[cy * width + cx])

    out = bytearray(new_w * new_h)
    for ny in range(new_h):
        sy = (ny + 0.5) / scale - 0.5
        iy = int(sy) if sy >= 0 else int(sy) - 1
        ty = sy - iy
        rows = []
        for dy in (-1, 0, 1, 2):
            row = []
            for nx in range(new_w):
                sx = (nx + 0.5) / scale - 0.5
                ix = int(sx) if sx >= 0 else int(sx) - 1
                tx = sx - ix
                v = catmull_rom(
                    sample(ix - 1, iy + dy),
                    sample(ix, iy + dy),
                    sample(ix + 1, iy + dy),
                    sample(ix + 2, iy + dy),
                    tx,
                )
                row.append(v)
            rows.append(row)
        for nx in range(new_w):
            val = catmull_rom(rows[0][nx], rows[1][nx], rows[2][nx], rows[3][nx], ty)
            val = max(0.0, min(255.0, val))
            if val > 128.0:
                val = min(255.0, 128.0 + (val - 128.0) * 1.18)
            else:
                val = max(0.0, 128.0 - (128.0 - val) * 1.18)
            out[ny * new_w + nx] = int(round(val))
    return bytes(out)


# تكبير صورة الحرف عبر فلتر Lanczos و UnsharpMask باستخدام مكتبة Pillow
def upscale_alpha_pil(pixels: bytes, width: int, height: int, scale: int) -> bytes:
    img = Image.frombytes("L", (width, height), pixels)
    up = img.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
    up = up.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
    return up.tobytes()


# تفكيك ملف الخط QBF1 وتكبير كل حرف داخله بالمعامل المحدد وإعادة تغليفه
def upscale_qbf_font(qbf_bytes: bytes, scale: int = UPSCALE_FACTOR) -> tuple[bytes, dict]:
    if len(qbf_bytes) < 16 or qbf_bytes[:4] != b"QBF1":
        raise ValueError("Invalid QBF1 font header")
    count, glyphs, fallback = struct.unpack_from("<HHH", qbf_bytes, 4)
    bpp, width, height, encoding = struct.unpack_from("4B", qbf_bytes, 12)
    if bpp != 4 or encoding != 2:
        raise ValueError("Unsupported QBF encoding")
    header_dir_size = 16 + count * 8
    src_glyph_bytes = width * height // 2
    dst_glyph_bytes = (width * scale) * (height * scale) // 2

    directory = bytearray(qbf_bytes[:header_dir_size])
    struct.pack_into("4B", directory, 12, 4, width * scale, height * scale, 2)
    out = bytearray(directory)
    out.extend(bytes(glyphs * dst_glyph_bytes))

    for idx in range(glyphs):
        start = header_dir_size + idx * src_glyph_bytes
        raw = qbf_bytes[start:start + src_glyph_bytes]
        alpha = bytearray(width * height)
        for i, byte in enumerate(raw):
            alpha[2 * i] = (byte >> 4) * 17
            alpha[2 * i + 1] = (byte & 15) * 17
        up = (
            upscale_alpha_pil(bytes(alpha), width, height, scale)
            if HAS_PIL
            else upscale_alpha_pure(bytes(alpha), width, height, scale)
        )
        dst_start = header_dir_size + idx * dst_glyph_bytes
        for i in range(dst_glyph_bytes):
            hi = min(15, (up[2 * i] + 8) // 17)
            lo = min(15, (up[2 * i + 1] + 8) // 17)
            out[dst_start + i] = (hi << 4) | lo

    mappings = []
    for i in range(count):
        pos = 16 + i * 8
        code, glyph_idx = struct.unpack_from("<HH", qbf_bytes, pos)
        metrics = qbf_bytes[pos + 4:pos + 8]
        mappings.append({
            "code": code,
            "native_glyph": glyph_idx,
            "coverage_glyph": glyph_idx,
            "native_metrics_hex": metrics.hex(),
        })

    hd_bytes = bytes(out)
    contract = {
        "native_sha256": sha256_bytes(qbf_bytes),
        "coverage_sha256": sha256_bytes(hd_bytes),
        "logical_cell": [width, height],
        "coverage_cell": [width * scale, height * scale],
        "density": scale,
        "fallback_code": fallback,
        "characters": mappings,
        "replacement_metric_differences": 0,
    }
    return hd_bytes, contract


# إنشاء حزمة font_coverage.zip بصيغة TriAevum الرسمية مع حفظ نسخة احتياطية من الأصل
def build_font_coverage_zip(native_qbf: bytes, hd_qbf: bytes, contract: dict, output_zip: Path) -> None:
    native_asset = f"native/{contract['native_sha256']}.qbf"
    hd_asset = f"coverage/{contract['coverage_sha256']}.qbf"
    manifest = {
        "format": "oot3d_qbf_coverage_v1",
        "runtime_enabled": True,
        "minimum_output_height": 480,
        "status": "native_atlas_coverage_ready",
        "fonts": [{"romfs_path": "message/eu/ltn16.qbf", "coverage_file": hd_asset,
                   "native_file": native_asset, **contract}],
        "source_font_sha256": contract["coverage_sha256"],
        "attribution": "Arabic HD Upscaled Font (4x)",
    }
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    backup = output_zip.with_suffix(".zip.bak")
    if output_zip.is_file() and not backup.is_file():
        shutil.copy2(output_zip, backup)
    tmp = output_zip.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo(hd_asset), hd_qbf)
        zf.writestr(zipfile.ZipInfo(native_asset), native_qbf)
        zf.writestr(zipfile.ZipInfo("manifest.json"), json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8"))
    tmp.replace(output_zip)


# تحديد مسار ملف font_coverage.zip داخل مجلد بيانات اللعبة
def find_font_coverage_target(prepared_dir: Path, game_root: Path) -> Path | None:
    state_path = prepared_dir / "forge-state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        runtime = state.get("runtime", {})
        profile_ref = runtime.get("launch_profile")
        if isinstance(profile_ref, str) and profile_ref:
            profile_path = Path(profile_ref)
            if not profile_path.is_absolute():
                profile_path = (prepared_dir / profile_path).resolve()
            if profile_path.is_file():
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                args = profile.get("arguments", [])
                if "--topscreen-texture-overrides" in args:
                    idx = args.index("--topscreen-texture-overrides") + 1
                    if idx < len(args):
                        override_path = Path(args[idx])
                        if not override_path.is_absolute():
                            override_path = (profile_path.parent / override_path).resolve()
                        return override_path.parent / "font_coverage.zip"
    for candidate in sorted((game_root / "data").rglob("font_coverage.zip")):
        return candidate
    return None


# تكبير أي صور PNG مرفقة في أرشيف المود ووضعها في مجلد تكستشر Azahar الخاص باللعبة
def upscale_texture_pack(source_root: Path, game_root: Path, scale: int = UPSCALE_FACTOR) -> int:
    if not HAS_PIL:
        return 0
    src_dir = None
    for candidate in sorted(source_root.rglob("0004000000033600")):
        if candidate.is_dir() and "textures" in [p.name.lower() for p in candidate.parents]:
            src_dir = candidate
            break
    if src_dir is None:
        for candidate in sorted(source_root.rglob("textures")):
            if candidate.is_dir():
                src_dir = candidate
                break
    if src_dir is None:
        return 0
    dst_dir = game_root / "data" / "load" / "textures" / "0004000000033600"
    count = 0
    for png in sorted(src_dir.rglob("*.png")):
        rel = png.relative_to(src_dir)
        out_png = dst_dir / rel
        out_png.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(png) as img:
            rgba = img.convert("RGBA")
            up = rgba.resize((rgba.width * scale, rgba.height * scale), Image.Resampling.LANCZOS)
            up = up.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=2))
            up.save(out_png, "PNG")
        count += 1
    return count


# جمع جميع ملفات مجلد romfs الخاص بالتعريب بمساراتها النسبية
def collect_replacements(mod_romfs: Path) -> dict[str, Path]:
    replacements: dict[str, Path] = {}
    for item in sorted(mod_romfs.rglob("*")):
        if item.is_file():
            rel = item.relative_to(mod_romfs).as_posix()
            replacements[rel] = item
    return replacements


# إيجاد مسار صورة RomFS الأصلية من ملف process-manifest.json
def find_romfs_image(prepared_dir: Path, manifest: Path) -> Path:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    source = data.get("source", {})
    raw_path = source.get("romfs_image_path")
    if isinstance(raw_path, str) and raw_path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (prepared_dir / candidate).resolve()
        if candidate.is_file():
            return candidate
    for name in ("romfs.bin", "romfs.romfs", "romfs"):
        path = prepared_dir / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"No RomFS image found in {prepared_dir}")


# البحث عن مجلد romfs داخل مجلد مستخرج أو مفكوك الضغط
def search_mod_romfs_in(root: Path) -> Path | None:
    if root.name.lower() == "romfs" and root.is_dir():
        return root
    for candidate in sorted(root.rglob("romfs")):
        if candidate.is_dir():
            return candidate
    return None


# البحث عن مجلد البيانات المحضّرة المحتوي على process-manifest.json داخل روت اللعبة
def find_prepared_dir_in_game_root(game_root: Path) -> Path:
    data_dir = game_root / "data"
    if data_dir.is_dir():
        for manifest in sorted(data_dir.rglob("process-manifest.json")):
            return manifest.parent
    for manifest in sorted(game_root.rglob("process-manifest.json")):
        return manifest.parent
    raise FileNotFoundError(f"process-manifest.json not found in {game_root}. Run TriAevumForge Prepare first.")


# فتح نافذة لاختيار ملف التعريب المضغوط zip عند تشغيل السكربت بدون سحب وإفلات
def pick_zip_file() -> Path:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askopenfilename(
        title="اختر ملف التعريب المضغوط (ZIP)",
        filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
    )
    root.destroy()
    if not selected:
        raise FileNotFoundError("No ZIP file selected")
    return Path(selected).expanduser().resolve()


# تفعيل ملف romfs_overlay.o3ro في ملف الإطلاق وتحديث بصمات SHA-256 في forge-state.json
def register_overlay(prepared_dir: Path, overlay_path: Path) -> None:
    state_path = prepared_dir / "forge-state.json"
    if not state_path.is_file():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    runtime = state.get("runtime")
    if not isinstance(runtime, dict):
        return
    profile_ref = runtime.get("launch_profile")
    if not isinstance(profile_ref, str) or not profile_ref:
        return
    profile_path = Path(profile_ref)
    if not profile_path.is_absolute():
        profile_path = (prepared_dir / profile_path).resolve()
    if not profile_path.is_file():
        return

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    args = profile.get("arguments", [])
    cleaned = []
    skip_next = False
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == "--romfs-overlay":
            skip_next = True
            continue
        cleaned.append(item)
    resolved_overlay = str(overlay_path.resolve())
    cleaned.extend(["--romfs-overlay", resolved_overlay])
    profile["arguments"] = cleaned
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    runtime["romfs_overlay"] = {
        "path": resolved_overlay,
        "sha256": sha256_file(overlay_path),
    }
    runtime["launch_profile_sha256"] = sha256_file(profile_path)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# تنفيذ المسار الكامل: فك الـ zip وتحديد روت اللعبة وبناء o3ro وتفعيله وتكبير الخط والصور
def process_source(source_root: Path, game_root: Path, scale: int) -> None:
    mod_romfs = search_mod_romfs_in(source_root)
    if mod_romfs is None:
        raise FileNotFoundError(f"romfs folder not found inside {source_root}")

    prepared_dir = find_prepared_dir_in_game_root(game_root)
    output_path = (game_root / "Mod" / "romfs_overlay.o3ro").resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = prepared_dir / "process-manifest.json"
    romfs_image = find_romfs_image(prepared_dir, manifest)

    service_offset, service_size = romfs_service_view(manifest)
    replacements = collect_replacements(mod_romfs)
    summary = build_romfs_overlay(
        romfs_image,
        service_offset=service_offset,
        service_size=service_size,
        replacements=replacements,
        output=output_path,
    )
    register_overlay(prepared_dir, output_path)
    print(f"Created and activated: {output_path}")
    print(summary)

    qbf_path = mod_romfs / "message" / "eu" / "ltn16.qbf"
    font_target = find_font_coverage_target(prepared_dir, game_root)
    if qbf_path.is_file() and font_target is not None:
        native_qbf = qbf_path.read_bytes()
        hd_qbf, contract = upscale_qbf_font(native_qbf, scale=scale)
        build_font_coverage_zip(native_qbf, hd_qbf, contract, font_target)
        print(f"HD Font ({scale}x) written to: {font_target}")

    tex_count = upscale_texture_pack(source_root, game_root, scale=scale)
    if tex_count:
        print(f"HD Textures ({scale}x) upscaled: {tex_count} PNG files")


# نقطة الدخول: استقبال ملف الـ zip المسحوب أو اختياره يدوياً وفكه مؤقتاً ثم تشغيل المعالجة
def main() -> None:
    game_root = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", nargs="?", default=None, type=Path)
    parser.add_argument("--scale", default=UPSCALE_FACTOR, type=int)
    args = parser.parse_args()

    target = args.input_path.expanduser().resolve() if args.input_path is not None else pick_zip_file()

    if target.is_file() and target.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="triaevum_mod_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            with zipfile.ZipFile(target, "r") as zf:
                zf.extractall(tmp_path)
            process_source(tmp_path, game_root, args.scale)
    elif target.is_dir():
        process_source(target, game_root, args.scale)
    else:
        raise ValueError(f"Unsupported input: {target}")


if __name__ == "__main__":
    main()
