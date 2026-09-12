# wallwell

Combine a light-mode image and a dark-mode image into a single macOS
appearance-aware dynamic wallpaper (`.heic`). macOS swaps the displayed image
automatically whenever the system appearance changes — the same mechanism
Apple's own Dynamic Desktop wallpapers use.

## Usage

```sh
uv sync                                   # set up the environment
uv run wallwell.py LIGHT DARK [options]   # build the wallpaper
uv run wallwell.py                        # or omit paths to use file pickers
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `light`, `dark` | positional, optional | picker | Light/dark-mode image paths |
| `-o`, `--output` | path | `~/Pictures/Wallpapers/<stem>-dual.heic` | Output path |
| `--lossless` | flag | off | Lossless encoding (4:4:4 chroma) |
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
present) before success is reported. On `--apply`, wallwell prints a warning
about the "Show on all spaces" toggle — see Known limitations.

## Tests

```sh
uv run pytest tests/ -v    # 47 tests, covering the plan's A0–A8 acceptance criteria
```

The acceptance-criteria mapping is documented in the test module docstring
(A7 uses 2560×1440 random-noise JPEGs since solid colours compress to near
zero and would mask size bugs; A8 simulates picker cancellation — real pickers
and `--apply` need a GUI session).

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

- `--apply` on macOS 26 can turn off "Show on all spaces" (open Apple defect,
  no workaround: WallpaperAgent's store plist is a projection of the toggle,
  not its source of truth, so rewriting it changes nothing). After applying,
  wallwell prints a warning on stdout — re-check the toggle under
  System Settings → Wallpaper, or install the file via
  System Settings → Wallpaper → Add Photo to avoid the path entirely.
- macOS caches wallpapers by path (Sonoma+): overwriting in place at an
  already-used path does not trigger a redraw. Write to a fresh filename.
- Acceptance checks are structural; final validation is opening the file in
  System Settings → Wallpaper and confirming a Light/Dark control appears
  rather than "Still".

## License

MIT — see [LICENSE](LICENSE).
