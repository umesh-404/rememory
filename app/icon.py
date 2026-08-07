"""The tray icon image, drawn in code.

Generated at runtime rather than shipped as a binary asset: it keeps the repo
text-only, renders crisply at whatever size the OS asks for, and can follow
state -- the icon carries a small status dot so a glance at the tray tells you
whether rememory is actually running.
"""

from __future__ import annotations

INDIGO = (99, 102, 241, 255)
VIOLET = (139, 92, 246, 255)
WHITE = (255, 255, 255, 255)
GREEN = (34, 197, 94, 255)
AMBER = (245, 158, 11, 255)


def make_icon(state: str = "unknown", size: int = 64):
    """Rounded indigo tile + three connected nodes (the 'memory graph' mark),
    with a status dot: green = running, amber = degraded, none = unknown."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # "RGBA" mode makes semi-transparent draws BLEND with the tile. The
    # default mode writes raw RGBA values instead, so the alpha-150 edge
    # lines below punched see-through slots into the icon rather than
    # drawing soft white lines over the indigo.
    d = ImageDraw.Draw(img, "RGBA")
    s = size / 64.0  # everything below is authored at 64px and scaled

    # Tile with a simple vertical gradient (indigo -> violet).
    radius = int(14 * s)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=INDIGO)
    for y in range(size):
        t = y / max(size - 1, 1)
        if t < 0.45:
            continue
        blend = (t - 0.45) / 0.55
        colour = tuple(
            int(INDIGO[i] + (VIOLET[i] - INDIGO[i]) * blend) for i in range(3)
        ) + (255,)
        d.line([(0, y), (size, y)], fill=colour)
    # Re-apply the rounded corners after the gradient lines squared them off.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # Node graph: two lower nodes, one upper node, connected.
    nodes = [(32 * s, 20 * s), (18 * s, 44 * s), (46 * s, 44 * s)]
    line_w = max(1, int(2.6 * s))
    for a in range(len(nodes)):
        for b in range(a + 1, len(nodes)):
            d.line([nodes[a], nodes[b]], fill=(255, 255, 255, 150), width=line_w)
    r = 6 * s
    for (cx, cy) in nodes:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)

    # Status dot, bottom-right, ringed so it reads on any tray background.
    if state in ("ok", "warn"):
        colour = GREEN if state == "ok" else AMBER
        dr = 10 * s
        box = [size - dr - 2 * s, size - dr - 2 * s, size - 2 * s, size - 2 * s]
        d.ellipse(box, fill=colour, outline=(10, 12, 17, 255), width=max(1, int(2 * s)))

    return img


def write_ico(path) -> bool:
    """Write a multi-resolution .ico for the Start-menu shortcut.

    Without this the shortcut inherits the icon of whatever binary launches it
    (uv.exe / powershell.exe), which is why a launcher-script approach looks
    like clutter rather than an app. Generated at setup time from the same
    drawing code as the tray icon, so they always match.
    """
    from pathlib import Path

    try:
        base = make_icon("unknown", 256)
        sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        base.save(str(path), format="ICO", sizes=sizes)
        return True
    except Exception:
        return False
