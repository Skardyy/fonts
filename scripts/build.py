#!/usr/bin/env python3
"""Config-driven font patcher. Reads fonts.toml, builds dist/<name>.tar.gz.

For each [[font]]: download zip, keep files matching `keep`, optionally
Nerd-patch, bake `line_height` into vertical metrics, apply `rename` regex sub
to family name and filename, tar the result. Then regenerate README.md.

    uv run scripts/build.py            build all + regenerate README
    uv run scripts/build.py --readme   regenerate README only
"""
from __future__ import annotations

import io
import re
import os
import sys
import shutil
import tarfile
import zipfile
import argparse
import tomllib
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, TypedDict, cast

from fontTools.ttLib import TTFont

ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG: Path = ROOT / "fonts.toml"
DIST: Path = ROOT / "dist"
WORK: Path = ROOT / ".work"
SELF: Path = Path(__file__).resolve()

WEIGHT_NAMES: dict[int, str] = {
    100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular",
    500: "Medium", 600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black",
}

# Nerd Font glyphs live in these private-use / symbol ranges.
NERD_RANGES: list[tuple[int, int]] = [
    (0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD),
]


class FontInfo(TypedDict):
    family: str
    weight: int
    italic: bool
    line_height: float
    nerd: bool


class Facts(TypedDict):
    families: list[str]
    weights: list[str]
    italics: bool
    line_heights: list[float]
    nerd: bool
    faces: int


def _in_nerd_range(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in NERD_RANGES)


def inspect_font(p: Path) -> FontInfo:
    f = TTFont(p)
    head = cast(Any, f["head"])
    hhea = cast(Any, f["hhea"])
    os2 = cast(Any, f["OS/2"])
    name = cast(Any, f["name"])
    cmap = cast(Any, f["cmap"])

    upm: int = head.unitsPerEm
    fam: str = (
        next((r.toUnicode() for r in name.names if r.nameID == 16), None)
        or next((r.toUnicode() for r in name.names if r.nameID == 1), "?")
    )
    italic: bool = bool(head.macStyle & 0b10) or bool(os2.fsSelection & 0b1)
    cps: set[int] = set()
    for t in cmap.tables:
        cps.update(t.cmap.keys())
    return {
        "family": fam,
        "weight": int(os2.usWeightClass),
        "italic": italic,
        "line_height": round((hhea.ascent - hhea.descent + hhea.lineGap) / upm, 3),
        "nerd": any(_in_nerd_range(c) for c in cps),
    }


def inspect_fonts(files: list[Path]) -> Facts | None:
    """Aggregate facts across a set of font files."""
    infos: list[FontInfo] = [inspect_font(p) for p in files]
    if not infos:
        return None
    weights: list[int] = sorted({i["weight"] for i in infos})
    return {
        "families": sorted({i["family"] for i in infos}),
        "weights": [f"{WEIGHT_NAMES.get(w, str(w))} ({w})" for w in weights],
        "italics": any(i["italic"] for i in infos),
        "line_heights": sorted({i["line_height"] for i in infos}),
        "nerd": all(i["nerd"] for i in infos),
        "faces": len(infos),
    }


def _extract_fonts(tar_or_dir: Path, into: Path) -> list[Path]:
    """Return font files from a loose font, a .tar.gz, or a directory."""
    if tar_or_dir.is_file() and tar_or_dir.suffix.lower() in (".ttf", ".otf"):
        return [tar_or_dir]
    if tar_or_dir.is_dir():
        return sorted(list(tar_or_dir.glob("*.ttf")) + list(tar_or_dir.glob("*.otf")))
    if tar_or_dir.suffixes[-2:] == [".tar", ".gz"] or tar_or_dir.suffix == ".gz":
        into.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_or_dir) as t:
            t.extractall(into)
        return sorted(list(into.rglob("*.ttf")) + list(into.rglob("*.otf")))
    return []


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def nerd_patch_files(files: list[Path], out_dir: Path, args: str) -> list[Path]:
    """Run the Nerd Fonts font-patcher on each file, writing to out_dir.

    Prefers the official Docker image (bundles FontForge + glyphs); falls back
    to a local `fontforge -script font-patcher`. Raises if neither is available.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    extra: list[str] = args.split() if args else []

    if have("docker"):
        in_dir = files[0].parent
        print(f"  nerd-patch via docker (args: {args or 'none'})")
        subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{in_dir}:/in:Z", "-v", f"{out_dir}:/out:Z",
             "nerdfonts/patcher", *extra],
            check=True,
        )
    elif have("fontforge"):
        print(f"  nerd-patch via local fontforge (args: {args or 'none'})")
        patcher = os.environ.get("FONT_PATCHER")
        if not patcher:
            raise SystemExit(
                "fontforge found but FONT_PATCHER env var not set. Point it at "
                "the font-patcher script from FontPatcher.zip, e.g. "
                "FONT_PATCHER=/path/to/font-patcher")
        for f in files:
            subprocess.run(
                ["fontforge", "-script", patcher, str(f),
                 "-out", str(out_dir), "--no-progressbars", *extra],
                check=True,
            )
    else:
        raise SystemExit(
            "nerd_patch enabled but neither `docker` nor `fontforge` is "
            "available. Install Docker (uses nerdfonts/patcher) or FontForge "
            "plus the font-patcher script (set FONT_PATCHER).")

    patched = sorted(p for p in out_dir.iterdir()
                     if p.suffix.lower() in (".ttf", ".otf"))
    if not patched:
        raise SystemExit("nerd patcher produced no output fonts")
    return patched


def patch_font(
    src: Path,
    dst: Path,
    line_height: float | None,
    rename: list[str] | None,
) -> None:
    f = TTFont(src)

    if line_height is not None:
        head = cast(Any, f["head"])
        hhea = cast(Any, f["hhea"])
        os2 = cast(Any, f["OS/2"])
        upm: int = head.unitsPerEm
        asc: int = hhea.ascent
        desc: int = hhea.descent  # negative
        box = asc - desc
        gap = round(upm * line_height) - box
        if gap < 0:
            print(f"    WARN {src.name}: natural {box/upm:.3f}x > {line_height}x; gap=0",
                  file=sys.stderr)
            gap = 0
        hhea.lineGap = gap
        os2.sTypoAscender = asc
        os2.sTypoDescender = desc
        os2.sTypoLineGap = gap
        os2.fsSelection |= (1 << 7)  # USE_TYPO_METRICS
        os2.usWinAscent = max(os2.usWinAscent, asc)
        os2.usWinDescent = max(os2.usWinDescent, -desc)

    if rename:
        pat, repl = rename
        name = cast(Any, f["name"])
        for rec in name.names:
            s = rec.toUnicode()
            new = re.sub(pat, repl, s)
            if new != s:
                rec.string = new

    dst.parent.mkdir(parents=True, exist_ok=True)
    f.save(dst)
    name = cast(Any, f["name"])
    fam = next((r.toUnicode() for r in name.names if r.nameID == 1), "?")
    print(f"    {dst.name}  family='{fam}'")


def download_and_extract(url: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "font-patcher"})
    with urllib.request.urlopen(req) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)


def find_license(src_dir: Path) -> Path | None:
    for p in src_dir.rglob("*"):
        if p.is_file() and p.name.lower().startswith("license"):
            return p
    return None


def build_font(cfg: dict[str, Any]) -> None:
    name: str = cfg["name"]
    print(f">> {name}")
    src_dir = WORK / name / "src"
    out_dir = WORK / name / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    download_and_extract(cfg["url"], src_dir)

    keep = re.compile(cfg["keep"])
    lh: float | None = cfg.get("line_height")
    rename: list[str] | None = cfg.get("rename")
    np: dict[str, Any] | None = cfg.get("nerd_patch")

    kept: list[Path] = [f for f in sorted(src_dir.rglob("*"))
                        if f.is_file() and keep.match(f.name)]
    if not kept:
        raise SystemExit(f"ERROR: {name}: no files matched /{cfg['keep']}/")

    # Optionally Nerd-patch first; downstream steps run on whatever results.
    if np and np.get("enabled"):
        np_dir = WORK / name / "nerd"
        if np_dir.exists():
            shutil.rmtree(np_dir)
        kept = nerd_patch_files(kept, np_dir, np.get("args", ""))

    matched = 0
    for f in kept:
        out_name = re.sub(rename[0], rename[1], f.name) if rename else f.name
        patch_font(f, out_dir / out_name, lh, rename)
        matched += 1

    lic = find_license(src_dir)
    if lic:
        shutil.copy(lic, out_dir / "LICENSE")
    shutil.copy(SELF, out_dir / "patch_font.py")

    DIST.mkdir(exist_ok=True)
    tar_path = DIST / f"{name}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for p in sorted(out_dir.iterdir()):
            tar.add(p, arcname=f"./{p.name}")
    print(f"  -> {tar_path.relative_to(ROOT)}  ({matched} faces)")


def _facts_block(facts: Facts, extra: dict[str, str] | None = None) -> list[str]:
    lines: list[str] = ["```"]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k.ljust(12)} {v}")
    lines.append(f"{'Family:'.ljust(12)} {', '.join(facts['families'])}")
    lines.append(f"{'Faces:'.ljust(12)} {facts['faces']}")
    lines.append(f"{'Weights:'.ljust(12)} {', '.join(facts['weights'])}")
    lines.append(f"{'Italics:'.ljust(12)} {'yes' if facts['italics'] else 'no'}")
    lh = ", ".join(f"{x}" for x in facts["line_heights"])
    lines.append(f"{'Line height:'.ljust(12)} {lh}")
    lines.append(f"{'Nerd Font:'.ljust(12)} {'yes' if facts['nerd'] else 'no'}")
    lines.append("```")
    return lines


def generate_readme(config: dict[str, Any]) -> None:
    fonts: list[dict[str, Any]] = config.get("font", [])
    out: list[str] = ["# fonts\n"]

    scan = WORK / "_scan"
    if scan.exists():
        shutil.rmtree(scan)

    for fnt in fonts:
        name: str = fnt["name"]
        tar = DIST / f"{name}.tar.gz"
        out.append(f"## {name}\n")
        if fnt.get("description"):
            out.append(fnt["description"] + "\n")
        files = _extract_fonts(tar, scan / name) if tar.exists() else []
        facts = inspect_fonts(files)
        if facts:
            out += _facts_block(facts, {"Source:": fnt["url"]})
        else:
            out.append("```")
            out.append(f"Source:      {fnt['url']}")
            out.append("(not built yet -- run scripts/build.py)")
            out.append("```")
        out.append("")

    manual_dir = ROOT / "manual"
    manual_items: list[Path] = []
    if manual_dir.exists():
        manual_items = sorted(
            p for p in manual_dir.iterdir()
            if p.suffix in (".ttf", ".otf") or p.suffixes[-2:] == [".tar", ".gz"])
    for item in manual_items:
        title = item.name.split(".")[0]
        out.append(f"## {title} (manual)\n")
        files = _extract_fonts(item, scan / f"manual_{title}")
        facts = inspect_fonts(files)
        if facts:
            out += _facts_block(facts)
        else:
            out.append("```\n(no inspectable fonts found)\n```")
        out.append("")

    (ROOT / "README.md").write_text("\n".join(out).rstrip() + "\n")
    print(">> wrote README.md")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readme", action="store_true", help="regenerate README only")
    args = ap.parse_args()

    config: dict[str, Any] = tomllib.loads(CONFIG.read_text())

    if not args.readme:
        if WORK.exists():
            shutil.rmtree(WORK)
        if DIST.exists():
            shutil.rmtree(DIST)
        for fnt in config.get("font", []):
            build_font(fnt)

    generate_readme(config)


if __name__ == "__main__":
    main()
