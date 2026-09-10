# dualwall

Combine a light-mode image and a dark-mode image into a single macOS
appearance-aware dynamic wallpaper (`.heic`). macOS swaps the displayed image
automatically whenever the system appearance changes — the same mechanism
Apple's own Dynamic Desktop wallpapers use.

## Usage

```sh
uv sync                                   # set up the environment
uv run dualwall.py LIGHT DARK [options]   # build the wallpaper
uv run dualwall.py                        # or omit paths to use file pickers
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `light`, `dark` | positional, optional | picker | Light/dark-mode image paths |
| `-o`, `--output` | path | `~/Pictures/Wallpapers/<stem>-dual.heic` | Output path |
| `--lossless` | flag | off | Lossless encoding |
| `-q`, `--quality` | int 0–100 | 90 | Lossy quality |
| `--fit` | flag | off | Centre-crop dark image to match |
| `--apply` | flag | off | Set as desktop picture (macOS only) |

Without `--apply`, install via System Settings → Wallpaper → Add Photo (or
right-click → Services → Set Desktop Picture), and keep the file at its path —
deleting or moving it removes the wallpaper.

## How it works

The output is a two-image HEIF container carrying Apple's `apple_desktop:apr`
XMP metadata on image 0 (the primary item):

```
HEIF container
├─┬─ image 0 (light mode)   ← primary item
│ ├── HEVC image data
│ └─── XMP metadata
│      └── apple_desktop:apr = base64(binary plist {"l": 0, "d": 1})
└─┬─ image 1 (dark mode)
  └── HEVC image data
```

The base64 plist is derived at runtime from `plistlib` — published copies of
the blob circulate corrupted, so no literal is embedded. Every written file is
reopened and structurally verified (two images, matching dimensions, metadata
present) before success is reported.

## Acceptance

`uv run python acceptance.py` generates fixtures (solid PNGs plus 2560×1440
random-noise JPEGs) and exercises A1–A8 from the plan:

```
[PASS] A0 runtime apr == reference
[PASS] A1 build pair exits 0
[PASS] A1 decoded apr == {'l': 0, 'd': 1}
[PASS] A2 two images, identical dims, apr on image 0 only — n=2, dims=[(60, 40), (60, 40)], apr0=True, apr1=False
[PASS] A3 mismatch exits non-zero, names both sizes and --fit, writes no file
[PASS] A4 --fit succeeds; stored dims == light dims exactly
[PASS] A5 --lossless passes A1+A2
[PASS] A6 --apply off Darwin: skip notice, exit 0, valid file
[PASS] A7 2560x1440 noise pair at -q 85: plausible size, A1+A2 pass — size=9.16 MB
[PASS] A8 cancelled picker exits 0 without traceback

10/10 checks passed
```

A8 simulates picker cancellation (osascript non-zero exit); the remaining
Darwin-only paths — real pickers, `--apply` — need a GUI session.

## Tests

```sh
uv run pytest tests/ -v        # 45 tests
uv run python acceptance.py    # A0–A8 from the plan
```

The suite drove out one real fix: the encoder's default 4:2:0 chroma
subsampling was silently discarding chroma detail even under `quality=-1`
("lossless" is only lossless in the coded YCbCr domain). `--lossless` now
requests 4:4:4 chroma, which decodes back with at most ±1 per-channel
rounding from the RGB↔YCbCr conversion.

## Tested against

| Component | Version |
|---|---|
| Python | 3.12.13 (uv-managed) |
| pillow | 12.3.0 |
| pillow-heif | 1.7.0 |
| libheif (bundled) | 1.23.3 |

## Known limitations

- `--apply` on macOS 26 turns off "Show on all spaces" after the first
  programmatic set; subsequent updates land only on the active Space. Open
  Apple defect, no workaround — install via System Settings instead.
- macOS caches wallpapers by path (Sonoma+): overwriting in place at an
  already-used path does not trigger a redraw. Write to a fresh filename.
- Acceptance checks are structural; final validation is opening the file in
  System Settings → Wallpaper and confirming a Light/Dark control appears
  rather than "Still".
