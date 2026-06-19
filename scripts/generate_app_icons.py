from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOURCES_DIR = PROJECT_ROOT / "resources"


def render_master_png(source_path: Path, size: int = 1024, corner_ratio: float = 0.205) -> Image.Image:
    image = Image.open(source_path).convert("RGBA")
    scale = max(size / image.width, size / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    left = (resized.width - size) // 2
    top = (resized.height - size) // 2
    square = resized.crop((left, top, left + size, top + size))

    radius = int(size * corner_ratio)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)

    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(square, (0, 0), mask)
    return rounded


def _render_windows_icon_frame(master: Image.Image, size: int) -> Image.Image:
    frame = master.resize((size, size), Image.LANCZOS).convert("RGBA")
    alpha = frame.getchannel("A")

    # Windows small-size icon rendering is sensitive to residual near-transparent
    # corner pixels. Clamp them to true transparency so the taskbar/titlebar icon
    # does not pick up a white fringe from premultiplication.
    cutoff = 0 if size >= 64 else 16
    alpha = alpha.point(lambda value: 0 if value <= cutoff else value)
    frame.putalpha(alpha)
    return frame


def build_windows_ico(master: Image.Image, target: Path) -> None:
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    prepared_master = master.convert("RGBA")
    prepared_master.save(target, format="ICO", sizes=sizes)


def build_macos_iconset(master: Image.Image, iconset_dir: Path) -> None:
    iconset_dir.mkdir(parents=True, exist_ok=True)
    sizes = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    for name, size in sizes:
        master.resize((size, size), Image.LANCZOS).save(iconset_dir / name)


def build_macos_icns(master: Image.Image, target: Path) -> None:
    master.save(
        target,
        format="ICNS",
        sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)],
    )


def build_linux_pngs(master: Image.Image, linux_dir: Path) -> None:
    linux_dir.mkdir(parents=True, exist_ok=True)
    for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        master.resize((size, size), Image.LANCZOS).save(linux_dir / f"chromium_profile_manager_{size}.png")


def generate_icons(source_path: Path, resources_dir: Path) -> dict[str, str]:
    resources_dir.mkdir(parents=True, exist_ok=True)
    master = render_master_png(source_path)

    master_png = resources_dir / "chromium_profile_manager.png"
    windows_ico = resources_dir / "chromium_profile_manager.ico"
    macos_icns = resources_dir / "chromium_profile_manager.icns"
    macos_iconset = resources_dir / "chromium_profile_manager.iconset"
    linux_dir = resources_dir / "linux_icons"

    master.save(master_png)
    build_windows_ico(master, windows_ico)
    build_macos_iconset(master, macos_iconset)
    build_macos_icns(master, macos_icns)
    build_linux_pngs(master, linux_dir)

    return {
        "source": str(source_path),
        "master_png": str(master_png),
        "windows_ico": str(windows_ico),
        "macos_icns": str(macos_icns),
        "macos_iconset": str(macos_iconset),
        "linux_dir": str(linux_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate app icon assets for Windows/macOS/Linux.")
    parser.add_argument("--source", required=True, help="Path to the source image used to generate app icons.")
    parser.add_argument("--resources-dir", default=str(DEFAULT_RESOURCES_DIR))
    args = parser.parse_args()

    result = generate_icons(Path(args.source), Path(args.resources_dir))
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
