"""Test suite for dualwall.py.

Covers the metadata builder, image loading (EXIF/ICC), dimension
reconciliation, encoding + structural verification, interactive input paths,
desktop application, and CLI behaviour (in-process and via subprocess).
"""

from __future__ import annotations

import base64
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pillow_heif
import pytest
from PIL import Image, ImageChops

import dualwall

REF_APR = "YnBsaXN0MDDSAQIDBFFkUWwQARAACA0PERMAAAAAAAABAQAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAFQ=="
SCRIPT = Path(__file__).resolve().parent.parent / "dualwall.py"


# ------------------------------------------------------------------ helpers --

def extract_apr(xmp: bytes) -> str:
    m = re.search(rb'apple_desktop:apr="([^"]+)"', xmp)
    assert m, f"no apr attribute in {xmp!r}"
    return m.group(1).decode("ascii")


def decode_plist(xmp: bytes) -> dict:
    return plistlib.loads(base64.b64decode(extract_apr(xmp)))


def make_pair(tmp_path: Path, size=(60, 40), name="img"):
    light = tmp_path / f"{name}-l.png"
    dark = tmp_path / f"{name}-d.png"
    Image.new("RGB", size, (255, 255, 255)).save(light, "PNG")
    Image.new("RGB", size, (16, 16, 24)).save(dark, "PNG")
    return light, dark


@pytest.fixture
def solid(tmp_path):
    def make(name, size, rgb):
        path = tmp_path / name
        Image.new("RGB", size, rgb).save(path, "PNG")
        return path

    return make


@pytest.fixture
def built_pair(tmp_path, solid):
    """A standard matched pair already built through encode_wallpaper."""
    light, dark = make_pair(tmp_path)
    out = tmp_path / "out.heic"
    light_img = dualwall.load_image(light)
    dark_img = dualwall.load_image(dark)
    dualwall.encode_wallpaper(
        light_img, dark_img, out, lossless=False, quality=90
    )
    return light, dark, out


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv], capture_output=True, text=True
    )


# ------------------------------------------------------- metadata builder --

def test_apr_matches_reference_blob():
    # Guards against the corrupted / index-inverted copies circulating online.
    assert extract_apr(dualwall.build_appearance_xmp(0, 1)) == REF_APR


def test_apr_indices_are_parameterised():
    assert decode_plist(dualwall.build_appearance_xmp(2, 5)) == {"l": 2, "d": 5}


def test_apr_plist_is_binary_format():
    raw = base64.b64decode(extract_apr(dualwall.build_appearance_xmp(0, 1)))
    assert raw.startswith(b"bplist00")


def test_xmp_packet_structure():
    xmp = dualwall.build_appearance_xmp(0, 1).decode("utf-8")
    assert '<x:xmpmeta xmlns:x="adobe:ns:meta/">' in xmp
    assert "http://www.w3.org/1999/02/22-rdf-syntax-ns#" in xmp
    assert 'xmlns:apple_desktop="http://ns.apple.com/namespace/1.0/"' in xmp
    assert xmp.count("rdf:Description") == 1
    assert xmp.startswith("<x:xmpmeta") and xmp.endswith("</x:xmpmeta>")


# ------------------------------------------------------------- load_image --

def test_load_image_rgb_passthrough(solid, tmp_path):
    img = dualwall.load_image(solid("a.png", (32, 16), (1, 2, 3)))
    assert img.mode == "RGB" and img.size == (32, 16)


def test_load_image_applies_exif_transpose(tmp_path):
    path = tmp_path / "rot.png"
    img = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[274] = 6  # orientation: rotated 90°
    img.save(path, exif=exif)
    loaded = dualwall.load_image(path)
    assert loaded.size == (20, 40)


def test_load_image_preserves_icc_across_mode_conversion(tmp_path):
    path = tmp_path / "pal.png"
    img = Image.new("P", (10, 10))
    img.putpalette([1, 2, 3] * 256)
    img.save(path, icc_profile=b"fake-icc-bytes")
    loaded = dualwall.load_image(path)
    assert loaded.mode == "RGB"
    assert loaded.info["icc_profile"] == b"fake-icc-bytes"


def test_load_image_missing_file_raises(tmp_path):
    with pytest.raises(dualwall.DualwallError, match="not found"):
        dualwall.load_image(tmp_path / "nope.png")


def test_load_image_corrupt_file_raises(tmp_path):
    path = tmp_path / "bad.png"
    path.write_bytes(b"this is not an image")
    with pytest.raises(dualwall.DualwallError, match="cannot open"):
        dualwall.load_image(path)


def test_load_image_accepts_heic_input(tmp_path):
    src = tmp_path / "src.heic"
    Image.new("RGB", (20, 10), (9, 9, 9)).save(src)  # register_heif_opener active
    loaded = dualwall.load_image(src)
    assert loaded.size == (20, 10)


# -------------------------------------------------------- dimension fitting --

def test_fit_matches_light_dims_exactly():
    light = Image.new("RGB", (100, 50), "white")
    dark = Image.new("RGB", (50, 100), "black")
    fitted = dualwall._fit_to_light(dark, light.size)
    assert fitted.size == (100, 50)


def test_fit_preserves_icc():
    dark = Image.new("RGB", (50, 100), "black")
    dark.info["icc_profile"] = b"prof"
    fitted = dualwall._fit_to_light(dark, (100, 50))
    assert fitted.info["icc_profile"] == b"prof"


def test_fit_is_centre_crop_not_distortion():
    # A 2:1 dark image fitted to 1:1 keeps its middle square: top and bottom
    # rows are cropped away, so the result's corner pixels still match centre
    # pixels of the original halves.
    top = Image.new("RGB", (100, 100), "red")
    bottom = Image.new("RGB", (100, 100), "blue")
    dark = Image.new("RGB", (100, 200))
    dark.paste(top, (0, 0))
    dark.paste(bottom, (0, 100))
    fitted = dualwall._fit_to_light(dark, (100, 100))
    assert fitted.getpixel((5, 5)) == (255, 0, 0)
    assert fitted.getpixel((5, 95)) == (0, 0, 255)


# ------------------------------------------------------------ output paths --

def test_default_output_path():
    out = dualwall.resolve_output(None, Path("/somewhere/Aqua.jpg"))
    assert out == Path.home() / "Pictures" / "Wallpapers" / "Aqua-dual.heic"
    assert not str(out).startswith("~")


def test_output_flag_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = dualwall.resolve_output("~/w/pair.heic", Path("x.jpg"))
    assert out == tmp_path / "w" / "pair.heic"


def test_output_appends_heic_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = dualwall.resolve_output("~/pair", Path("x.jpg"))
    assert out.name == "pair.heic"


# ------------------------------------------------- encoding + verification --

def test_roundtrip_two_images_xmp_on_image0_only(built_pair):
    _, _, out = built_pair
    f = pillow_heif.open_heif(str(out))
    assert len(f) == 2
    assert [tuple(im.size) for im in f] == [(60, 40), (60, 40)]
    assert b"apple_desktop:apr" in f[0].info["xmp"]
    assert "xmp" not in f[1].info


def test_written_apr_decodes_to_expected_dict(built_pair):
    _, _, out = built_pair
    f = pillow_heif.open_heif(str(out))
    assert decode_plist(f[0].info["xmp"]) == {"l": 0, "d": 1}


def test_verify_accepts_valid_file(built_pair):
    dualwall.verify_wallpaper(built_pair[2])  # no exception


def test_verify_rejects_wrong_image_count(tmp_path):
    out = tmp_path / "single.heic"
    h = pillow_heif.HeifFile()
    h.add_from_pillow(Image.new("RGB", (10, 10)))
    h.save(str(out))
    with pytest.raises(dualwall.VerificationError, match="2 images"):
        dualwall.verify_wallpaper(out)


def test_verify_rejects_missing_xmp(tmp_path):
    out = tmp_path / "noxmp.heic"
    h = pillow_heif.HeifFile()
    h.add_from_pillow(Image.new("RGB", (10, 10)))
    h.add_from_pillow(Image.new("RGB", (10, 10)))
    h.save(str(out))
    with pytest.raises(dualwall.VerificationError, match="apr"):
        dualwall.verify_wallpaper(out)


def test_encode_creates_parent_dirs(tmp_path, solid):
    out = tmp_path / "deep" / "nested" / "dirs" / "out.heic"
    dualwall.encode_wallpaper(
        dualwall.load_image(solid("l.png", (10, 10), "white")),
        dualwall.load_image(solid("d.png", (10, 10), "black")),
        out,
        lossless=False,
        quality=90,
    )
    assert out.exists()


def _max_channel_diff(a: Image.Image, b: Image.Image) -> int:
    return max(hi for _, hi in ImageChops.difference(a, b).getextrema())


def test_lossless_is_near_exact_and_lossy_is_not(tmp_path):
    # quality=-1 is lossless in the coded (YCbCr) domain; the RGB<->YCbCr
    # conversion itself rounds, so only a tiny residue is allowed — while
    # q50 on noise must show clearly larger deviations.
    size = (128, 128)
    noise = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    ll, q50 = tmp_path / "ll.heic", tmp_path / "q50.heic"
    dualwall.encode_wallpaper(noise, noise, ll, lossless=True, quality=90)
    dualwall.encode_wallpaper(noise, noise, q50, lossless=False, quality=50)
    f_ll = pillow_heif.open_heif(str(ll))[0].to_pillow()
    f_q50 = pillow_heif.open_heif(str(q50))[0].to_pillow()
    assert _max_channel_diff(f_ll, noise) <= 2
    assert _max_channel_diff(f_q50, noise) > 2


def test_lossy_compresses_smooth_content_more_than_lossless(tmp_path):
    img = Image.new("RGB", (256, 256))
    for y in range(256):
        for x in range(256):
            img.putpixel((x, y), (x, y, (x + y) // 2))
    lossy, lossless = tmp_path / "q.heic", tmp_path / "ll.heic"
    dualwall.encode_wallpaper(img, img, lossy, lossless=False, quality=90)
    dualwall.encode_wallpaper(img, img, lossless, lossless=True, quality=90)
    # On compressible content the lossy file must be clearly smaller.
    assert lossy.stat().st_size < lossless.stat().st_size


# ------------------------------------------------------- interactive input --

def test_choose_file_darwin_success(monkeypatch):
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="/tmp/pic 1.jpg\n")

    monkeypatch.setattr(dualwall.subprocess, "run", fake_run)
    assert dualwall.choose_file_darwin("prompt") == Path("/tmp/pic 1.jpg")
    assert "choose file" in recorded["cmd"][2]


def test_choose_file_darwin_cancel_raises_cancelled(monkeypatch):
    monkeypatch.setattr(
        dualwall.subprocess,
        "run",
        mock.Mock(
            side_effect=subprocess.CalledProcessError(1, "osascript")
        ),
    )
    with pytest.raises(dualwall.Cancelled):
        dualwall.choose_file_darwin("prompt")


def test_prompt_path_strips_quotes_and_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda _: '  "~/pics/a b.jpg"  ')
    assert dualwall.prompt_path("give") == tmp_path / "pics" / "a b.jpg"


def test_prompt_path_empty_input_cancels(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: '   ""  ')
    with pytest.raises(dualwall.Cancelled):
        dualwall.prompt_path("give")


def test_collect_image_prefers_arg_with_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    got = dualwall.collect_image("light", "~/imgs/l.jpg")
    assert got == tmp_path / "imgs" / "l.jpg"


# ---------------------------------------------------- desktop application --

def test_set_desktop_picture_builds_script(monkeypatch, tmp_path):
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(dualwall.subprocess, "run", fake_run)
    target = tmp_path / "my wall\"quote.heic"
    dualwall.set_desktop_picture(target)
    script = recorded["cmd"][2]
    assert 'tell application "System Events"' in script
    assert "set picture of every desktop" in script
    assert str(target.resolve()).replace('"', '\\"') in script


def test_set_desktop_picture_failure_mentions_permission(monkeypatch):
    monkeypatch.setattr(
        dualwall.subprocess,
        "run",
        mock.Mock(
            side_effect=subprocess.CalledProcessError(
                1, "osascript", stderr="not authorized"
            )
        ),
    )
    with pytest.raises(dualwall.DualwallError, match="Automation"):
        dualwall.set_desktop_picture(Path("/tmp/x.heic"))


def test_main_apply_off_darwin_skips_and_exits_zero(
    monkeypatch, tmp_path, solid, capsys
):
    monkeypatch.setattr(dualwall, "IS_DARWIN", False)
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (30, 20), "white")),
        str(solid("d.png", (30, 20), "black")),
        "-o", str(out), "--apply",
    ])
    assert rc == 0
    assert "skipping" in capsys.readouterr().out
    assert len(pillow_heif.open_heif(str(out))) == 2


def test_main_apply_on_darwin_success(monkeypatch, tmp_path, solid, capsys):
    monkeypatch.setattr(dualwall, "IS_DARWIN", True)
    monkeypatch.setattr(
        dualwall.subprocess,
        "run",
        mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="")),
    )
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (30, 20), "white")),
        str(solid("d.png", (30, 20), "black")),
        "-o", str(out), "--apply",
    ])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "desktop picture set" in out_text
    assert "Show on all spaces" in out_text  # macOS 26 defect note


def test_main_apply_failure_exits_one(monkeypatch, tmp_path, solid, capsys):
    monkeypatch.setattr(dualwall, "IS_DARWIN", True)
    monkeypatch.setattr(
        dualwall.subprocess,
        "run",
        mock.Mock(
            side_effect=subprocess.CalledProcessError(1, "osascript", stderr="x")
        ),
    )
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (30, 20), "white")),
        str(solid("d.png", (30, 20), "black")),
        "-o", str(out), "--apply",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Automation" in captured.err
    assert "Add Photo" in captured.out  # manual route still shown
    assert out.exists()  # the wallpaper itself was written and verified


# ----------------------------------------------------------- main / CLI --

def test_main_happy_path_messages(tmp_path, solid, capsys):
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (40, 30), "white")),
        str(solid("d.png", (40, 30), "black")),
        "-o", str(out),
    ])
    out_text = capsys.readouterr().out
    assert rc == 0
    assert "2 images, 40x30, quality 90" in out_text
    assert "verified" in out_text
    assert "Add Photo" in out_text
    assert "permanently" in out_text


def test_main_mismatch_without_fit_fails_and_writes_no_file(
    tmp_path, solid, capsys
):
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (60, 40), "white")),
        str(solid("d.png", (40, 60), "black")),
        "-o", str(out),
    ])
    err = capsys.readouterr().err
    assert rc == 1
    assert "60x40" in err and "40x60" in err and "--fit" in err
    assert not out.exists()


def test_main_fit_succeeds_and_stores_light_dims(tmp_path, solid, capsys):
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (60, 40), "white")),
        str(solid("d.png", (40, 60), "black")),
        "-o", str(out), "--fit",
    ])
    assert rc == 0
    f = pillow_heif.open_heif(str(out))
    assert [tuple(im.size) for im in f] == [(60, 40), (60, 40)]


def test_main_lossless_flag(tmp_path, solid, capsys):
    out = tmp_path / "o.heic"
    rc = dualwall.main([
        str(solid("l.png", (10, 10), "white")),
        str(solid("d.png", (10, 10), "black")),
        "-o", str(out), "--lossless",
    ])
    assert rc == 0
    assert "lossless" in capsys.readouterr().out
    dualwall.verify_wallpaper(out)


def test_main_verification_failure_exits_one(
    monkeypatch, tmp_path, solid, capsys
):
    def boom(path):
        raise dualwall.VerificationError("bad container")

    monkeypatch.setattr(dualwall, "verify_wallpaper", boom)
    rc = dualwall.main([
        str(solid("l.png", (10, 10), "white")),
        str(solid("d.png", (10, 10), "black")),
        "-o", str(tmp_path / "o.heic"),
    ])
    assert rc == 1
    assert "bad container" in capsys.readouterr().err


def test_main_existing_output_prints_cache_warning(
    monkeypatch, tmp_path, solid, capsys
):
    monkeypatch.setattr(dualwall, "IS_DARWIN", False)
    out = tmp_path / "o.heic"
    args = [
        str(solid("l.png", (10, 10), "white")),
        str(solid("d.png", (10, 10), "black")),
        "-o", str(out),
    ]
    assert dualwall.main(args) == 0
    capsys.readouterr()
    assert dualwall.main(args) == 0
    assert "already exists" in capsys.readouterr().out


def test_main_cancelled_picker_exits_cleanely(monkeypatch, capsys):
    monkeypatch.setattr(dualwall, "IS_DARWIN", True)
    monkeypatch.setattr(
        dualwall.subprocess,
        "run",
        mock.Mock(
            side_effect=subprocess.CalledProcessError(1, "osascript")
        ),
    )
    rc = dualwall.main([])
    assert rc == 0
    assert "Cancelled" in capsys.readouterr().out


def test_quality_bounds():
    assert dualwall.parse_args(["l", "d", "-q", "0"]).quality == 0
    assert dualwall.parse_args(["l", "d", "-q", "100"]).quality == 100
    with pytest.raises(SystemExit):
        dualwall.parse_args(["l", "d", "-q", "101"])
    with pytest.raises(SystemExit):
        dualwall.parse_args(["l", "d", "-q", "-1"])


def test_parse_args_defaults():
    args = dualwall.parse_args([])
    assert args.light is None and args.dark is None
    assert args.output is None
    assert args.quality == 90
    assert not (args.lossless or args.fit or args.apply)


# ------------------------------------------------------- subprocess wiring --

def test_cli_end_to_end(tmp_path):
    light, dark = make_pair(tmp_path, (50, 30), "e2e")
    out = tmp_path / "e2e.heic"
    r = run_cli(str(light), str(dark), "-o", str(out))
    assert r.returncode == 0, r.stderr
    f = pillow_heif.open_heif(str(out))
    assert len(f) == 2
    assert decode_plist(f[0].info["xmp"]) == {"l": 0, "d": 1}


def test_cli_mismatch_exits_nonzero(tmp_path):
    light, _ = make_pair(tmp_path, (50, 30), "mis")
    dark = tmp_path / "mis-d2.png"
    Image.new("RGB", (30, 50), "black").save(dark, "PNG")
    out = tmp_path / "mis.heic"
    r = run_cli(str(light), str(dark), "-o", str(out))
    assert r.returncode != 0
    assert "--fit" in r.stderr
    assert not out.exists()
