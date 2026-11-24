"""Render assets/icon.ico (every Windows size drawn separately) and assets/icon.png.

Run by build.ps1 when the icon is missing; run it by hand after changing the design.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from neonconvert.theme import ICON_SIZES, app_icon  # noqa: E402

assets = ROOT / "assets"
assets.mkdir(exist_ok=True)
frames = {n: app_icon(n) for n in ICON_SIZES}
big = frames[256]
big.save(assets / "icon.png")
# each size is hand-tuned rather than scaled down from 256 px, so small icons stay crisp
big.save(assets / "icon.ico", sizes=[(n, n) for n in ICON_SIZES],
         append_images=[frames[n] for n in ICON_SIZES if n != 256])
print("wrote", assets / "icon.ico", "sizes:", ", ".join(map(str, ICON_SIZES)))
