#!/usr/bin/env python3
"""Acceptance checks A1-A8 for dualwall.py.

Run with:  uv run python acceptance.py

Fixtures are generated into a temp directory: solid-colour PNGs for A3-A6,
os.urandom noise JPEGs at 2560x1440 for A7 (solid colours compress to near
zero and would mask size-reporting bugs). A8 simulates picker cancellation by
raising CalledProcessError from the osascript call path.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from PIL import Image

import pillow_heif

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dualwall

SCRIPT = Path(__file__).resolve().parent / "dualwall.py"
REF_APR = "YnBsaXN0MDDSAQIDBFFkUWwQARAACA0PERMAAAAAAAABAQAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAFQ=="
TMP = Path(tempfile.mkdtemp(prefix="dualwall-acceptance-"))
RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    line = f"[{'PASS' if cond else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv], capture_output=True, text=True
    )


def solid(name: str, size: tuple[int, int], rgb: tuple[int, int, int]) -> Path:
    path = TMP / name
    Image.new("RGB", size, rgb).save(path, "PNG")
    return path


def noise_jpeg(name: str, size: tuple[int, int]) -> Path:
    path = TMP / name
    data = os.urandom(size[0] * size[1] * 3)
    Image.frombytes("RGB", size, data).save(path, "JPEG", quality=95)
    return path


def decode_apr(path: Path) -> dict:
    f = pillow_heif.open_heif(str(path))
    m = re.search(rb'apple_desktop:apr="([^"]+)"', f[0].info["xmp"])
    return plistlib.loads(base64.b64decode(m.group(1)))


def structural(path: Path) -> tuple[int, list[tuple[int, int]], bool, bool]:
    """Return (image count, dimensions, apr-on-image-0, apr-on-image-1)."""
    f = pillow_heif.open_heif(str(path))
    dims = [tuple(img.size) for img in f]
    apr0 = b"apple_desktop:apr" in f[0].info.get("xmp", b"")
    apr1 = b"apple_desktop:apr" in f[1].info.get("xmp", b"") if len(f) > 1 else False
    return len(f), dims, apr0, apr1


def main() -> int:
    print(f"fixtures in {TMP}")

    # A0 (extra): runtime-derived plist matches the known-correct reference
    # blob, guarding against the corrupted copies circulating online.
    m = re.search(
        rb'apple_desktop:apr="([^"]+)"', dualwall.build_appearance_xmp(0, 1)
    )
    check("A0 runtime apr == reference", m.group(1).decode() == REF_APR)

    # A1 — metadata round-trip (via CLI on a matched solid pair).
    l1, d1 = solid("l1.png", (60, 40), (255, 255, 255)), solid(
        "d1.png", (60, 40), (20, 20, 30)
    )
    out1 = TMP / "a1.heic"
    r = cli(str(l1), str(d1), "-o", str(out1))
    check("A1 build pair exits 0", r.returncode == 0, r.stderr.strip())
    check(
        "A1 decoded apr == {'l': 0, 'd': 1}",
        decode_apr(out1) == {"l": 0, "d": 1},
    )

    # A2 — container shape.
    n, dims, apr0, apr1 = structural(out1)
    check("A2 two images, identical dims, apr on image 0 only",
          n == 2 and dims[0] == dims[1] and apr0 and not apr1,
          f"n={n}, dims={dims}, apr0={apr0}, apr1={apr1}")

    # A3 — mismatch rejection, no --fit.
    l3, d3 = solid("l3.png", (60, 40), (255, 255, 255)), solid(
        "d3.png", (40, 60), (20, 20, 30)
    )
    out3 = TMP / "a3.heic"
    r = cli(str(l3), str(d3), "-o", str(out3))
    err = r.stderr
    check(
        "A3 mismatch exits non-zero, names both sizes and --fit, writes no file",
        r.returncode != 0
        and "60x40" in err
        and "40x60" in err
        and "--fit" in err
        and not out3.exists(),
        f"rc={r.returncode}, stderr={err.strip()!r}",
    )

    # A4 — fit path.
    out4 = TMP / "a4.heic"
    r = cli(str(l3), str(d3), "-o", str(out4), "--fit")
    n, dims, _, _ = structural(out4)
    check(
        "A4 --fit succeeds; stored dims == light dims exactly",
        r.returncode == 0 and n == 2 and dims == [(60, 40), (60, 40)],
        f"rc={r.returncode}, dims={dims}",
    )

    # A5 — lossless path on matched PNGs.
    out5 = TMP / "a5.heic"
    r = cli(str(l1), str(d1), "-o", str(out5), "--lossless")
    check(
        "A5 --lossless passes A1+A2",
        r.returncode == 0
        and decode_apr(out5) == {"l": 0, "d": 1}
        and structural(out5)[:2] == (2, [(60, 40), (60, 40)]),
        f"rc={r.returncode}, size={out5.stat().st_size} bytes",
    )

    # A6 — non-Darwin --apply guard (module flag flipped; no GUI involved).
    out6 = TMP / "a6.heic"
    captured = io.StringIO()
    with mock.patch.object(dualwall, "IS_DARWIN", False):
        with contextlib.redirect_stdout(captured):
            rc = dualwall.main([str(l1), str(d1), "-o", str(out6), "--apply"])
    check(
        "A6 --apply off Darwin: skip notice, exit 0, valid file",
        rc == 0
        and "skipping" in captured.getvalue()
        and structural(out6)[:2] == (2, [(60, 40), (60, 40)]),
        f"rc={rc}",
    )

    # A7 — photographic case: 2560x1440 noise JPEGs at -q 85.
    l7, d7 = noise_jpeg("l7.jpg", (2560, 1440)), noise_jpeg("d7.jpg", (2560, 1440))
    out7 = TMP / "a7.heic"
    r = cli(str(l7), str(d7), "-o", str(out7), "-q", "85")
    size = out7.stat().st_size if out7.exists() else 0
    check(
        "A7 2560x1440 noise pair at -q 85: plausible size, A1+A2 pass",
        r.returncode == 0
        and size > 200_000
        and decode_apr(out7) == {"l": 0, "d": 1}
        and structural(out7)[:2] == (2, [(2560, 1440), (2560, 1440)]),
        f"rc={r.returncode}, size={size / 1_048_576:.2f} MB",
    )

    # A8 — picker cancellation exits cleanly (simulated osascript cancel).
    captured = io.StringIO()
    with mock.patch.object(dualwall, "IS_DARWIN", True):
        with mock.patch(
            "dualwall.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "osascript"),
        ):
            with contextlib.redirect_stdout(captured):
                rc = dualwall.main([])
    check(
        "A8 cancelled picker exits 0 without traceback",
        rc == 0 and "Cancelled" in captured.getvalue(),
        f"rc={rc}, stdout={captured.getvalue().strip()!r}",
    )

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
