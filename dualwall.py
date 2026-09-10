#!/usr/bin/env python3
"""dualwall — combine a light- and a dark-mode image into one macOS
appearance-aware dynamic wallpaper (.heic).

Usage:
    uv run dualwall.py LIGHT DARK [-o OUT.heic] [--lossless] [-q N] [--fit] [--apply]

    Omit LIGHT/DARK to pick them interactively (native file pickers on macOS,
    typed paths elsewhere). macOS swaps the displayed image automatically when
    the system appearance changes between Light and Dark.

Container structure produced (the same mechanism Apple's Dynamic Desktop
wallpapers use):

    HEIF container
    ├─┬─ image 0 (light mode)   ← primary item
    │ ├── HEVC image data
    │ └─── XMP metadata
    │      └── apple_desktop:apr = base64(binary plist {"l": 0, "d": 1})
    └─┬─ image 1 (dark mode)
      └── HEVC image data

Rules encoded below: the XMP packet attaches to image 0 only (image 0 must be
the primary item); "l"/"d" are indices into the image sequence; the plist is
binary format, then base64. The base64 blob is always derived at runtime —
published copies circulate corrupted (one has a placeholder spliced in, another
inverts the indices), so no literal is hardcoded here.

Known limitations:
  * --apply on macOS 26 (Tahoe) can turn off "Show on all spaces" after a
    programmatic set. This is an open Apple defect with no workaround:
    WallpaperAgent's store plist is a projection of the toggle, not its source
    of truth, so rewriting it changes nothing. After applying, dualwall prints
    a warning — re-check the toggle under System Settings → Wallpaper.
    Installing through System Settings avoids the path entirely.
  * macOS caches wallpapers by path (Sonoma and later): overwriting a file in
    place at an already-used path does not trigger a redraw. Write to a fresh
    filename instead.
  * The post-write verification is structural, not behavioural: it confirms the
    container is well-formed but cannot confirm macOS accepts it. Final
    validation is System Settings → Wallpaper → the file, confirming a
    Light/Dark control appears rather than "Still". (If it shows as Still,
    selecting an Apple Dynamic Desktop once and then reselecting the file
    clears the stale state.)

Dependencies: pillow, pillow-heif.
"""

from __future__ import annotations

import argparse
import base64
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps

    import pillow_heif
except ImportError:  # pragma: no cover
    sys.exit(
        "Missing dependencies. Install with:\n"
        "    pip3 install pillow pillow-heif"
    )

# Let PIL open .heic/.heif inputs alongside JPEG and PNG.
pillow_heif.register_heif_opener()

IS_DARWIN = sys.platform == "darwin"
DEFAULT_QUALITY = 90


class Cancelled(Exception):
    """The user cancelled an interactive prompt."""


class DualwallError(Exception):
    """A user-facing error."""


class VerificationError(DualwallError):
    """The written file failed structural verification."""


# ---------------------------------------------------------------- metadata --

def build_appearance_xmp(light_index: int = 0, dark_index: int = 1) -> bytes:
    """Build the apple_desktop:apr XMP packet linking light/dark image indices.

    The plist is serialised as binary and base64-encoded at runtime; the value
    is never hardcoded (see module docstring).
    """
    apr = base64.b64encode(
        plistlib.dumps(
            {"l": light_index, "d": dark_index}, fmt=plistlib.FMT_BINARY
        )
    ).decode("ascii")
    packet = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '<rdf:Description xmlns:apple_desktop="http://ns.apple.com/namespace/1.0/"\n'
        f'apple_desktop:apr="{apr}"/>\n'
        "</rdf:RDF></x:xmpmeta>"
    )
    return packet.encode("utf-8")


# -------------------------------------------------------------- image input --

def load_image(path: Path) -> Image.Image:
    """Open an image, applying EXIF orientation and preserving the ICC profile."""
    try:
        img = Image.open(path)
    except FileNotFoundError:
        raise DualwallError(f"image not found: {path}") from None
    except OSError as exc:
        raise DualwallError(f"cannot open image {path}: {exc}") from None

    # Capture the profile before any conversion — convert() drops it and
    # wide-gamut sources would be flattened to sRGB.
    icc_profile = img.info.get("icc_profile")

    img = ImageOps.exif_transpose(img)  # phone photos otherwise encode rotated
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    if icc_profile:
        img.info["icc_profile"] = icc_profile
    return img


def _fit_to_light(dark: Image.Image, light_size: tuple[int, int]) -> Image.Image:
    """Centre-crop and rescale the dark image to the light image's exact size."""
    icc_profile = dark.info.get("icc_profile")
    dark = ImageOps.fit(
        dark, light_size, method=Image.LANCZOS, centering=(0.5, 0.5)
    )
    if icc_profile:
        dark.info["icc_profile"] = icc_profile
    return dark


# ----------------------------------------------------------- input collection --

def choose_file_darwin(prompt: str) -> Path:
    """Show a native file picker; a cancellation raises Cancelled."""
    script = (
        f'POSIX path of (choose file with prompt "{prompt}"'
        ' of type {"public.image"})'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # Non-zero exit from choose file means the user clicked Cancel.
        raise Cancelled(prompt) from None
    return Path(proc.stdout.strip())


def prompt_path(prompt: str) -> Path:
    """Ask for a path on the terminal, expanding ~ and stripping quotes."""
    raw = input(f"{prompt}: ").strip()
    raw = raw.strip('"').strip("'").strip()
    if not raw:
        raise Cancelled(prompt) from None
    return Path(raw).expanduser()


def collect_image(label: str, arg: Optional[str]) -> Path:
    """Resolve an image path from a CLI arg or interactive prompting."""
    if arg:
        return Path(arg).expanduser()
    prompt = f"Choose the {label}-mode image"
    if IS_DARWIN:
        return choose_file_darwin(prompt)
    return prompt_path(prompt)


# ------------------------------------------------------------------ encoding --

def encode_wallpaper(
    light: Image.Image,
    dark: Image.Image,
    out_path: Path,
    *,
    lossless: bool,
    quality: int,
) -> None:
    """Write the two-image HEIF container with appearance metadata."""
    heif = pillow_heif.HeifFile()
    heif.add_from_pillow(light)  # insertion order = image index; light is 0
    heif.add_from_pillow(dark)  # dark is 1
    # XMP must sit on image 0, the primary item — anywhere else and macOS
    # treats the file as a still.
    heif[0].info["xmp"] = build_appearance_xmp(0, 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        heif.save(
            str(out_path),
            save_all=True,
            quality=-1 if lossless else quality,  # -1 selects lossless
            # quality=-1 is lossless only in the coded (YCbCr) domain; the
            # encoder's default 4:2:0 chroma subsampling would still discard
            # chroma detail, so request 4:4:4 to make --lossless honest.
            chroma=444 if lossless else 420,
        )
    except (OSError, ValueError) as exc:
        raise DualwallError(f"failed to encode {out_path}: {exc}") from exc


def verify_wallpaper(out_path: Path) -> None:
    """Reopen the written file and assert its structure. Raises on failure."""
    f = pillow_heif.open_heif(str(out_path))
    if len(f) != 2:
        raise VerificationError(
            f"verification failed: expected 2 images in the container, found {len(f)}"
        )
    xmp = f[0].info.get("xmp", b"")
    if b"apple_desktop:apr" not in xmp:
        raise VerificationError(
            "verification failed: apple_desktop:apr XMP missing from image 0"
        )
    dims = {tuple(image.size) for image in f}
    if len(dims) != 1:
        raise VerificationError(
            f"verification failed: stored image dimensions differ: {sorted(dims)}"
        )


# -------------------------------------------------------------- installation --

def set_desktop_picture(path: Path) -> None:
    """Set the desktop picture on every desktop via System Events."""
    posix = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "System Events" to '
        f'set picture of every desktop to "{posix}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise DualwallError(
            "could not set the desktop picture"
            + (f": {detail}" if detail else "")
            + "\nAutomation permission may be required — grant it under "
            "System Settings → Privacy & Security → Automation "
            "(allow this terminal app to control System Events), then retry."
        ) from exc


# ----------------------------------------------------------------------- CLI --

def resolve_output(output: Optional[str], light_path: Path) -> Path:
    if output:
        out = Path(output).expanduser()
    else:
        out = Path("~/Pictures/Wallpapers").expanduser() / (
            f"{light_path.stem}-dual.heic"
        )
    if not out.suffix:
        out = out.with_suffix(".heic")
    return out


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dualwall",
        description=(
            "Combine light- and dark-mode images into one macOS "
            "appearance-aware dynamic wallpaper (.heic)."
        ),
    )
    parser.add_argument(
        "light", nargs="?", help="light-mode image path (default: file picker)"
    )
    parser.add_argument(
        "dark", nargs="?", help="dark-mode image path (default: file picker)"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="output path (default: ~/Pictures/Wallpapers/<light-stem>-dual.heic)",
    )
    parser.add_argument(
        "--lossless",
        action="store_true",
        help="encode losslessly with 4:4:4 chroma (suits flat or graphic sources)",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        help="lossy quality, 0-100 (default: 90)",
    )
    parser.add_argument(
        "--fit",
        action="store_true",
        help="centre-crop the dark image to the light image's dimensions",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="set the result as the desktop picture (macOS only)",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.quality <= 100:
        parser.error("--quality must be between 0 and 100")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        light_path = collect_image("light", args.light)
        dark_path = collect_image("dark", args.dark)
    except Cancelled:
        print("Cancelled.")
        return 0

    try:
        light = load_image(light_path)
        dark = load_image(dark_path)

        if dark.size != light.size:
            if not args.fit:
                lw, lh = light.size
                dw, dh = dark.size
                raise DualwallError(
                    f"image dimensions differ: light is {lw}x{lh}, dark is "
                    f"{dw}x{dh} — pass --fit to centre-crop the dark image "
                    "to the light image's size, or crop the images to match"
                )
            dark = _fit_to_light(dark, light.size)

        out_path = resolve_output(args.output, light_path)
        if out_path.exists():
            print(
                f"note: {out_path} already exists — macOS caches wallpapers "
                "by path (Sonoma and later), so overwriting in place may not "
                "trigger a redraw; prefer a fresh filename"
            )

        encode_wallpaper(
            light, dark, out_path, lossless=args.lossless, quality=args.quality
        )
        verify_wallpaper(out_path)
    except DualwallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size_mb = out_path.stat().st_size / 1_048_576
    mode = "lossless" if args.lossless else f"quality {args.quality}"
    print(
        f"wrote {out_path} — 2 images, {light.size[0]}x{light.size[1]}, "
        f"{mode}, {size_mb:.1f} MB (verified)"
    )

    applied = False
    apply_failed = False
    if args.apply:
        if IS_DARWIN:
            try:
                set_desktop_picture(out_path)
                applied = True
                print(f"desktop picture set to {out_path}")
                print(
                    'warning: setting the wallpaper programmatically can turn '
                    'off "Show on all spaces" — open System Settings → '
                    'Wallpaper and confirm the toggle is still on; if macOS '
                    'switched it off, turn it back on (installing the file via '
                    "System Settings → Wallpaper → Add Photo avoids this "
                    "entirely)"
                )
            except DualwallError as exc:
                apply_failed = True
                print(f"error: {exc}", file=sys.stderr)
        else:
            print(
                "note: --apply is macOS-only; skipping "
                "(the wallpaper file itself was written and verified)"
            )
    if not applied:
        print(
            "to use it: System Settings → Wallpaper → Add Photo, or "
            "right-click the file → Services → Set Desktop Picture"
        )
    print(
        f"keep {out_path} at this path permanently — deleting or moving it "
        "removes the wallpaper"
    )
    return 1 if apply_failed else 0


if __name__ == "__main__":
    sys.exit(main())
