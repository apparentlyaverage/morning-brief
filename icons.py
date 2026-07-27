"""Real icons for the shortcuts panel: site favicons and app icons.

Three sources, because Windows keeps them in three different places:

  Sites       The browser already cached a favicon for every site you've
              visited. Reading its Favicons database is instant and needs no
              network - the alternative, fetching /favicon.ico per domain,
              would be slow and would tell every one of those sites that you
              opened your dashboard.

  Apps (exe)  SHGetFileInfoW hands back an HICON, which is converted to a PNG
              here rather than with Pillow - the project ships no image
              library, and the PWA icons are hand-written the same way.

  Store apps  There is no .exe to point at. The install location comes from
              Get-AppxPackage and the logo is picked out of the package's own
              assets. That's a subprocess, so the answer is cached on disk.

Anything without an icon returns None, and the panel falls back to a lettered
tile - which is what browsers do for a site with no favicon anyway.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import zlib
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "icon_cache"
STORE_MAP = CACHE_DIR / "_packages.json"

LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
FAVICON_DBS = [
    LOCAL / "Microsoft/Edge/User Data/Default/Favicons",
    LOCAL / "Google/Chrome/User Data/Default/Favicons",
    LOCAL / "BraveSoftware/Brave-Browser/User Data/Default/Favicons",
]


# ------------------------------------------------------------------ PNG

def _png(width: int, height: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4]
                   for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ------------------------------------------------------------- favicons

def site_icon(domain: str) -> bytes | None:
    """Largest cached favicon for a domain, across every browser installed."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None

    best: tuple[int, bytes] | None = None
    for db in FAVICON_DBS:
        if not db.exists():
            continue
        tmp_dir = tempfile.mkdtemp(prefix="mb-fav-")
        tmp = Path(tmp_dir) / "Favicons"
        try:
            shutil.copy2(db, tmp)       # locked while the browser runs
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT b.width, b.image_data FROM favicon_bitmaps b "
                    "JOIN icon_mapping m ON m.icon_id = b.icon_id "
                    "WHERE (m.page_url LIKE ? OR m.page_url LIKE ?) "
                    "AND LENGTH(b.image_data) > 0",
                    (f"%//{domain}/%", f"%//www.{domain}/%")).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        for width, blob in rows:
            # Prefer the biggest, but not so big it's a wasteful download.
            if blob and (best is None or (width or 0) > best[0]) and (width or 0) <= 256:
                best = (width or 0, bytes(blob))
    return best[1] if best else None


# ----------------------------------------------------------- app icons

def _win32():
    """Handle-typed bindings.

    argtypes are not optional here: without them ctypes marshals a 64-bit
    HBITMAP as a plain int and raises "int too long to convert" the moment a
    handle lands above 2^31, which is most of the time on a real desktop.
    """
    shell32, user32, gdi32 = (ctypes.windll.shell32, ctypes.windll.user32,
                              ctypes.windll.gdi32)

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                    ("dwAttributes", wintypes.DWORD),
                    ("szDisplayName", wintypes.WCHAR * 260),
                    ("szTypeName", wintypes.WCHAR * 80)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD),
                    ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                    ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.POINTER(SHFILEINFOW), ctypes.c_uint,
                                       ctypes.c_uint]
    shell32.SHGetFileInfoW.restype = ctypes.c_void_p
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ICONINFO)]
    user32.DestroyIcon.argtypes = [ctypes.c_void_p]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
    return shell32, user32, gdi32, SHFILEINFOW, ICONINFO, BITMAP, BITMAPINFO, BITMAPINFOHEADER


def file_icon(path: str) -> bytes | None:
    """The icon Explorer shows for a file - works for .exe and .lnk alike."""
    if not path or not os.path.exists(path):
        return None
    try:
        (shell32, user32, gdi32, SHFILEINFOW, ICONINFO,
         BITMAP, BITMAPINFO, BITMAPINFOHEADER) = _win32()
    except Exception:
        return None

    SHGFI_ICON, SHGFI_LARGEICON = 0x100, 0x0
    info = SHFILEINFOW()
    if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(info),
                                  ctypes.sizeof(info), SHGFI_ICON | SHGFI_LARGEICON):
        return None
    if not info.hIcon:
        return None

    try:
        ii = ICONINFO()
        if not user32.GetIconInfo(info.hIcon, ctypes.byref(ii)):
            return None
        bm = BITMAP()
        gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None

        hdc = gdi32.CreateCompatibleDC(None)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # negative height: top-down rows
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bi), 0)
        gdi32.DeleteDC(hdc)
        gdi32.DeleteObject(ii.hbmColor)
        gdi32.DeleteObject(ii.hbmMask)
        if not got:
            return None

        bgra = buf.raw
        rgba = bytearray(len(bgra))
        rgba[0::4], rgba[1::4] = bgra[2::4], bgra[1::4]
        rgba[2::4], rgba[3::4] = bgra[0::4], bgra[3::4]
        # A few older icons come back fully transparent; treat that as opaque
        # rather than rendering an invisible tile.
        if not any(rgba[3::4]):
            rgba[3::4] = b"\xff" * (w * h)
        return _png(w, h, bytes(rgba))
    finally:
        user32.DestroyIcon(info.hIcon)


def _package_logos() -> dict:
    """AppUserModelId -> logo file, from Get-AppxPackage. Cached on disk."""
    try:
        return json.loads(STORE_MAP.read_text(encoding="utf-8-sig"))
    except Exception:
        pass

    script = (
        "Get-AppxPackage | ForEach-Object { "
        "  $p=$_; try { Get-AppxPackageManifest $p -ErrorAction Stop | "
        "  ForEach-Object { $_.Package.Applications.Application } | ForEach-Object { "
        "    if ($_.Id) { [pscustomobject]@{ "
        "      aumid = $p.PackageFamilyName + '!' + $_.Id; "
        "      root  = $p.InstallLocation; "
        "      logo  = $_.VisualElements.Square44x44Logo } } } } catch {} } | ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=120)
        rows = json.loads(proc.stdout or "[]")
    except Exception:
        rows = []
    if isinstance(rows, dict):
        rows = [rows]

    out = {}
    for row in rows:
        aumid, root, logo = row.get("aumid"), row.get("root"), row.get("logo")
        if not (aumid and root and logo):
            continue
        base = Path(root) / logo
        # Manifests name "Square44x44Logo.png"; what ships is a family of
        # scaled variants beside it, so take whichever exists and is biggest.
        found = sorted(base.parent.glob(base.stem + "*" + base.suffix),
                       key=lambda p: p.stat().st_size if p.exists() else 0)
        if found:
            out[aumid] = str(found[-1])

    CACHE_DIR.mkdir(exist_ok=True)
    try:
        STORE_MAP.write_text(json.dumps(out, indent=0), encoding="utf-8")
    except OSError:
        pass
    return out


def store_icon(aumid: str) -> bytes | None:
    logo = _package_logos().get(aumid)
    if not logo:
        return None
    try:
        return Path(logo).read_bytes()
    except OSError:
        return None


# ------------------------------------------------------------- assembly

def for_shortcut(row: dict) -> bytes | None:
    """Best icon for a panel row, cached on disk so this is cheap to poll."""
    key = "".join(c if c.isalnum() else "_" for c in row.get("id", ""))[:80]
    if not key:
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    cached = CACHE_DIR / f"{key}.png"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()

    target = row.get("target", "")
    data = None
    if row.get("kind") == "site":
        host = target.split("//", 1)[-1].split("/", 1)[0]
        data = site_icon(host[4:] if host.startswith("www.") else host)
    elif target.startswith("shell:AppsFolder\\"):
        data = store_icon(target.split("\\", 1)[1])
    else:
        data = file_icon(target)

    if data:
        try:
            cached.write_bytes(data)
        except OSError:
            pass
    return data
