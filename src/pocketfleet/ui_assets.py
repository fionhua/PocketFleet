"""PocketFleet Modern UI Assets & Icon Engine.

Provides super-sampled, anti-aliased vector-style icons, pills, and button
renderers for Tkinter, faithfully matching the modern SaaS visual design.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple, Optional
from PIL import Image, ImageDraw, ImageTk

# ==============================================================================
# Modern Daylight Palette (Exact alignment with reference UI design)
# ==============================================================================
COLOR_WIN_BG = "#f6f8fc"          # Soft light cool-gray background
COLOR_CARD_BG = "#ffffff"         # Pure white card surface
COLOR_CARD_BORDER = "#e2e8f0"     # Slate-200 subtle gray border
COLOR_CARD_BORDER_ACTIVE = "#22c55e" # Emerald border for active selection
COLOR_ACTIVE_BG = "#f0fdf4"       # Emerald-50 light background

# Emerald brand / action accents
COLOR_EMERALD_PRIMARY = "#059669" # Emerald-600 main button green
COLOR_EMERALD_HOVER = "#047857"   # Emerald-700 hover green
COLOR_EMERALD_DARK = "#065f46"    # Emerald-800 text / badge
COLOR_EMERALD_LIGHT = "#e2f6eb"   # Emerald-100 light badge / pill bg
COLOR_EMERALD_PILL_BG = "#ebf8f2" # Reference pill bg
COLOR_EMERALD_PILL_TEXT = "#0f5132"

# Telegram brand blue
COLOR_TG_BLUE = "#2aabee"
COLOR_TG_LIGHT_BG = "#e0f2fe"

# Danger / Remove accent
COLOR_DANGER_TEXT = "#ef4444"     # Red-500
COLOR_DANGER_BG = "#fef2f2"       # Red-50
COLOR_DANGER_BORDER = "#fee2e2"   # Red-100
COLOR_DANGER_HOVER = "#fecaca"    # Red-200

# Outline / Neutral Button
COLOR_BTN_OUTLINE_BG = "#ffffff"
COLOR_BTN_OUTLINE_BORDER = "#d1d5db"
COLOR_BTN_OUTLINE_TEXT = "#374151"
COLOR_BTN_OUTLINE_HOVER = "#f3f4f6"

# Typography
COLOR_TEXT_TITLE = "#0f2942"      # Deep navy blue for headings
COLOR_TEXT_MAIN = "#1e293b"       # Slate-800
COLOR_TEXT_MUTED = "#64748b"      # Slate-500
COLOR_TEXT_LIGHT = "#94a3b8"      # Slate-400
COLOR_LINK_BLUE = "#2563eb"       # Blue-600 for @username and links

# Cache for PhotoImage objects to avoid garbage collection and redundant rendering
_IMAGE_CACHE: Dict[str, ImageTk.PhotoImage] = {}
_PIL_CACHE: Dict[str, Image.Image] = {}


def render_supersampled(draw_fn, size: Tuple[int, int], scale: int = 4) -> Image.Image:
    """Renders a drawing function at 4x resolution and scales down with Lanczos."""
    w, h = size[0] * scale, size[1] * scale
    hi_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(hi_img)
    draw_fn(d, w, h, scale)
    return hi_img.resize(size, Image.Resampling.LANCZOS)


# ------------------------------------------------------------------------------
# Icon Drawing Functions
# ------------------------------------------------------------------------------

def _draw_logo_rocket(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Deep blue / cyan tilted rocket logo."""
    cx, cy = w * 0.48, h * 0.52
    # Rocket body tilted ~ 45 deg
    body_col = "#0b3b60"
    fin_col = "#0e9f6e"
    flame_col = "#10b981"

    # Exhaust flame / fins
    d.polygon([(cx - 7*s, cy + 3*s), (cx - 10*s, cy + 8*s), (cx - 4*s, cy + 7*s)], fill=fin_col)
    d.polygon([(cx + 3*s, cy - 7*s), (cx + 8*s, cy - 10*s), (cx + 7*s, cy - 4*s)], fill=fin_col)
    d.ellipse([cx - 8*s, cy + 4*s, cx - 2*s, cy + 10*s], fill=flame_col)

    # Rocket main capsule
    capsule = [
        (cx + 8*s, cy - 8*s),
        (cx + 5*s, cy - 2*s),
        (cx - 3*s, cy + 4*s),
        (cx - 5*s, cy + 3*s),
        (cx - 4*s, cy - 3*s),
        (cx + 2*s, cy - 5*s),
    ]
    d.polygon(capsule, fill=body_col)
    # Nose cone
    d.ellipse([cx + 3*s, cy - 9*s, cx + 9*s, cy - 3*s], fill=body_col)
    # Porch window
    d.ellipse([cx + 1*s, cy - 3*s, cx + 5*s, cy + 1*s], fill="#ffffff")
    d.ellipse([cx + 2*s, cy - 2*s, cx + 4*s, cy + 0*s], fill="#38bdf8")


def _draw_telegram_circle(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Blue circle with crisp white paper airplane."""
    m = 2 * s
    d.ellipse([m, m, w - m, h - m], fill=COLOR_TG_BLUE)
    cx, cy = w * 0.5, h * 0.5
    pts = [
        (cx + 7.5 * s, cy - 6.5 * s),
        (cx - 8.5 * s, cy - 1.0 * s),
        (cx - 2.0 * s, cy + 2.0 * s),
        (cx - 2.0 * s, cy + 6.5 * s),
        (cx + 1.8 * s, cy + 2.8 * s),
        (cx + 7.5 * s, cy - 6.5 * s),
    ]
    d.polygon(pts, fill="#ffffff")
    fold = [
        (cx - 2.0 * s, cy + 2.0 * s),
        (cx - 2.0 * s, cy + 6.5 * s),
        (cx + 0.8 * s, cy + 3.8 * s),
    ]
    d.polygon(fold, fill="#cde4f7")


def _draw_human_circle(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Soft green circle with dark emerald human silhouette."""
    m = 2 * s
    d.ellipse([m, m, w - m, h - m], fill=COLOR_EMERALD_LIGHT)
    hcx, hcy = w * 0.5, h * 0.38
    hr = 4.2 * s
    d.ellipse([hcx - hr, hcy - hr, hcx + hr, hcy + hr], fill=COLOR_EMERALD_PRIMARY)
    d.chord([w * 0.24, h * 0.55, w * 0.76, h * 0.90], start=180, end=0, fill=COLOR_EMERALD_PRIMARY)


def _draw_code_circle(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Soft mint green circle with dark emerald </> symbol."""
    m = 2 * s
    d.ellipse([m, m, w - m, h - m], fill="#dcfce7")
    cx, cy = w * 0.5, h * 0.5
    fg = COLOR_EMERALD_DARK
    sw = max(2, int(2.2 * s))
    d.line([(cx - 7 * s, cy), (cx - 3 * s, cy - 5 * s)], fill=fg, width=sw)
    d.line([(cx - 7 * s, cy), (cx - 3 * s, cy + 5 * s)], fill=fg, width=sw)
    d.line([(cx - 1 * s, cy + 6 * s), (cx + 1 * s, cy - 6 * s)], fill=fg, width=sw)
    d.line([(cx + 7 * s, cy), (cx + 3 * s, cy - 5 * s)], fill=fg, width=sw)
    d.line([(cx + 7 * s, cy), (cx + 3 * s, cy + 5 * s)], fill=fg, width=sw)


def _draw_chat_circle(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Soft indigo/blue circle with speech bubble."""
    m = 2 * s
    d.ellipse([m, m, w - m, h - m], fill="#e0e7ff")
    fg = "#3b82f6"
    bw, bh = w * 0.48, h * 0.40
    bcx, bcy = w * 0.5, h * 0.46
    d.rounded_rectangle([bcx - bw/2, bcy - bh/2, bcx + bw/2, bcy + bh/2], radius=3*s, fill=fg)
    tail = [(bcx - bw/4, bcy + bh/2), (bcx - bw/2, bcy + bh/2 + 3.5*s), (bcx - bw/6, bcy + bh/2)]
    d.polygon(tail, fill=fg)
    # 3 dots inside bubble
    dr = 1.2 * s
    for off in (-3.5 * s, 0, 3.5 * s):
        d.ellipse([bcx + off - dr, bcy - dr, bcx + off + dr, bcy + dr], fill="#ffffff")


def _draw_bot_circle(d: ImageDraw.ImageDraw, w: int, h: int, s: int, bg: str, fg: str):
    """Cute rounded bot avatar."""
    m = 2 * s
    d.ellipse([m, m, w - m, h - m], fill=bg)
    cx, cy = w * 0.5, h * 0.52
    rw, rh = w * 0.46, h * 0.36
    # Antenna
    d.line([(cx, cy - rh/2), (cx, cy - rh/2 - 3*s)], fill=fg, width=max(2, int(1.8*s)))
    d.ellipse([cx - 2*s, cy - rh/2 - 5*s, cx + 2*s, cy - rh/2 - 1*s], fill=fg)
    # Head & Ears
    d.rounded_rectangle([cx - rw/2, cy - rh/2, cx + rw/2, cy + rh/2], radius=4*s, fill=fg)
    d.rounded_rectangle([cx - rw/2 - 2*s, cy - 2*s, cx - rw/2, cy + 2*s], radius=1*s, fill=fg)
    d.rounded_rectangle([cx + rw/2, cy - 2*s, cx + rw/2 + 2*s, cy + 2*s], radius=1*s, fill=fg)
    # Eyes & Smile
    er = 1.8 * s
    d.ellipse([cx - 4*s - er, cy - er, cx - 4*s + er, cy + er], fill="#ffffff")
    d.ellipse([cx + 4*s - er, cy - er, cx + 4*s + er, cy + er], fill="#ffffff")
    d.arc([cx - 3.5*s, cy - 1*s, cx + 3.5*s, cy + 3.5*s], start=0, end=180, fill="#ffffff", width=max(1, int(1.5*s)))


def _draw_lightbulb(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Glowing blue lightbulb for tip banner."""
    cx, cy = w * 0.5, h * 0.48
    fg = "#0284c7"
    # Bulb circle
    br = 5 * s
    d.ellipse([cx - br, cy - br, cx + br, cy + br], fill=fg)
    # Base
    d.rectangle([cx - 2.5*s, cy + 3.5*s, cx + 2.5*s, cy + 7*s], fill=fg)
    # Rays
    rw = max(1, int(1.5 * s))
    d.line([(cx, cy - 7*s), (cx, cy - 9*s)], fill=fg, width=rw)
    d.line([(cx - 7*s, cy), (cx - 9*s, cy)], fill=fg, width=rw)
    d.line([(cx + 7*s, cy), (cx + 9*s, cy)], fill=fg, width=rw)
    d.line([(cx - 5*s, cy - 5*s), (cx - 7*s, cy - 7*s)], fill=fg, width=rw)
    d.line([(cx + 5*s, cy - 5*s), (cx + 7*s, cy - 7*s)], fill=fg, width=rw)


def _draw_users_icon(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str = "#0e9f6e"):
    """Two users icon."""
    # Person 1 (front)
    c1x, c1y = w * 0.40, h * 0.40
    d.ellipse([c1x - 3.2*s, c1y - 3.2*s, c1x + 3.2*s, c1y + 3.2*s], fill=col)
    d.chord([c1x - 5.5*s, c1y + 2.5*s, c1x + 5.5*s, c1y + 11*s], start=180, end=0, fill=col)
    # Person 2 (back right)
    c2x, c2y = w * 0.65, h * 0.35
    d.ellipse([c2x - 2.8*s, c2y - 2.8*s, c2x + 2.8*s, c2y + 2.8*s], fill=col)
    d.chord([c2x - 4.5*s, c2y + 2.5*s, c2x + 4.5*s, c2y + 9*s], start=180, end=0, fill=col)


def _draw_radio(d: ImageDraw.ImageDraw, w: int, h: int, s: int, checked: bool):
    """Modern circular radio button."""
    cx, cy = w * 0.5, h * 0.5
    r = w * 0.42
    if checked:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#059669", width=max(2, int(2.2*s)))
        inner_r = r * 0.52
        d.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill="#059669")
    else:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#94a3b8", width=max(1, int(1.8*s)))


def _draw_status_dot(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str):
    """Crisp status dot."""
    cx, cy = w * 0.5, h * 0.5
    r = w * 0.36
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)


def _draw_service_icon(d: ImageDraw.ImageDraw, w: int, h: int, s: int, service_type: str):
    """Green line/filled icons for the 4 diagnostic services."""
    cx, cy = w * 0.5, h * 0.5
    col = "#059669"
    sw = max(2, int(2 * s))

    if service_type == "tg":
        # Paper airplane
        pts = [(cx + 6*s, cy - 5*s), (cx - 7*s, cy - 1*s), (cx - 2*s, cy + 2*s), (cx - 2*s, cy + 6*s), (cx + 2*s, cy + 2.5*s)]
        d.polygon(pts, fill=col)
    elif service_type == "web":
        # Globe with grid
        r = 6.5 * s
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=sw)
        d.line([(cx - r, cy), (cx + r, cy)], fill=col, width=sw)
        d.ellipse([cx - r*0.45, cy - r, cx + r*0.45, cy + r], outline=col, width=sw)
    elif service_type == "ext":
        # Puzzle piece
        d.rounded_rectangle([cx - 5*s, cy - 5*s, cx + 5*s, cy + 5*s], radius=2*s, fill=col)
        d.ellipse([cx + 3*s, cy - 2*s, cx + 7*s, cy + 2*s], fill=col)
        d.ellipse([cx - 2*s, cy - 7*s, cx + 2*s, cy - 3*s], fill=col)
    elif service_type == "agent":
        # 3D isometric cube
        d.regular_polygon((cx, cy, 6.5*s), 6, rotation=30, fill=None, outline=col)
        d.line([(cx, cy), (cx, cy + 6*s)], fill=col, width=sw)
        d.line([(cx, cy), (cx - 5.5*s, cy - 3.2*s)], fill=col, width=sw)
        d.line([(cx, cy), (cx + 5.5*s, cy - 3.2*s)], fill=col, width=sw)


def _draw_gear(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str = "#475569"):
    """Gear icon."""
    cx, cy = w * 0.5, h * 0.5
    r = 4.5 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=max(2, int(2*s)))
    d.ellipse([cx - 1.8*s, cy - 1.8*s, cx + 1.8*s, cy + 1.8*s], fill=col)
    sw = max(2, int(2*s))
    for angle in (0, 45, 90, 135):
        rad = math.radians(angle)
        dx = (r + 1.5*s) * math.cos(rad)
        dy = (r + 1.5*s) * math.sin(rad)
        d.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], fill=col, width=sw)


def _draw_trash(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str = "#ef4444"):
    """Trashcan icon."""
    cx, cy = w * 0.5, h * 0.5
    sw = max(1, int(1.5*s))
    # Lid
    d.line([(cx - 5*s, cy - 4*s), (cx + 5*s, cy - 4*s)], fill=col, width=sw)
    d.line([(cx - 2*s, cy - 5.5*s), (cx + 2*s, cy - 5.5*s)], fill=col, width=sw)
    # Bin body
    d.polygon([(cx - 4*s, cy - 3*s), (cx + 4*s, cy - 3*s), (cx + 3.2*s, cy + 6*s), (cx - 3.2*s, cy + 6*s)], outline=col)
    # 2 vertical stripes inside
    d.line([(cx - 1.5*s, cy - 1*s), (cx - 1.2*s, cy + 4*s)], fill=col, width=sw)
    d.line([(cx + 1.5*s, cy - 1*s), (cx + 1.2*s, cy + 4*s)], fill=col, width=sw)


def _draw_link(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str = "#475569"):
    """Chain link icon."""
    cx, cy = w * 0.5, h * 0.5
    sw = max(2, int(1.8*s))
    # Two tilted ovals
    d.line([(cx - 4*s, cy - 2*s), (cx - 1*s, cy - 5*s)], fill=col, width=sw)
    d.line([(cx - 4*s, cy - 2*s), (cx - 2*s, cy + 1*s)], fill=col, width=sw)
    d.line([(cx + 1*s, cy - 1*s), (cx + 4*s, cy + 2*s)], fill=col, width=sw)
    d.line([(cx + 2*s, cy + 5*s), (cx + 4*s, cy + 2*s)], fill=col, width=sw)
    d.line([(cx - 2*s, cy - 2*s), (cx + 2*s, cy + 2*s)], fill=col, width=sw)


def _draw_reload(d: ImageDraw.ImageDraw, w: int, h: int, s: int, col: str = "#475569"):
    """Circular reload arrow icon."""
    cx, cy = w * 0.5, h * 0.5
    r = 4.8 * s
    d.arc([cx - r, cy - r, cx + r, cy + r], start=45, end=320, fill=col, width=max(2, int(2*s)))
    rad = math.radians(45)
    ax, ay = cx + r * math.cos(rad), cy + r * math.sin(rad)
    d.polygon([(ax - 1*s, ay - 3*s), (ax + 3*s, ay), (ax - 2*s, ay + 2*s)], fill=col)


def _draw_white_plane(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Crisp pure white paper airplane for CTA button."""
    cx, cy = w * 0.5, h * 0.5
    pts = [
        (cx + 8.0 * s, cy - 6.5 * s),
        (cx - 8.5 * s, cy - 1.0 * s),
        (cx - 2.0 * s, cy + 2.0 * s),
        (cx - 2.0 * s, cy + 6.5 * s),
        (cx + 1.8 * s, cy + 2.8 * s),
        (cx + 8.0 * s, cy - 6.5 * s),
    ]
    d.polygon(pts, fill="#ffffff")
    fold = [
        (cx - 2.0 * s, cy + 2.0 * s),
        (cx - 2.0 * s, cy + 6.5 * s),
        (cx + 1.0 * s, cy + 3.8 * s),
    ]
    d.polygon(fold, fill="#a7f3d0")


def _draw_white_plus(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Pure white plus symbol."""
    cx, cy = w * 0.5, h * 0.5
    sw = max(2, int(2.2 * s))
    arm = 5 * s
    d.line([(cx - arm, cy), (cx + arm, cy)], fill="#ffffff", width=sw)
    d.line([(cx, cy - arm), (cx, cy + arm)], fill="#ffffff", width=sw)


def _draw_mini_silhouette(d: ImageDraw.ImageDraw, w: int, h: int, s: int, is_human: bool, col: str):
    """Mini icon used inside template cards (Human or Bot)."""
    cx, cy = w * 0.5, h * 0.5
    if is_human:
        hr = 3.2 * s
        hcy = cy - 3.2 * s
        d.ellipse([cx - hr, hcy - hr, cx + hr, hcy + hr], fill=col)
        d.chord([cx - 5.5 * s, cy + 0.5 * s, cx + 5.5 * s, cy + 8 * s], start=180, end=0, fill=col)
    else:
        # Small bot head
        rw, rh = 7 * s, 5.5 * s
        d.rounded_rectangle([cx - rw/2, cy - rh/2 + 0.5*s, cx + rw/2, cy + rh/2 + 0.5*s], radius=2*s, fill=col)
        # Antenna
        d.line([(cx, cy - rh/2 + 0.5*s), (cx, cy - rh/2 - 1.8*s)], fill=col, width=max(1, int(1.5*s)))
        # Eyes
        er = 1.0 * s
        d.ellipse([cx - 2*s - er, cy - er, cx - 2*s + er, cy + er], fill="#ffffff")
        d.ellipse([cx + 2*s - er, cy - er, cx + 2*s + er, cy + er], fill="#ffffff")


def _draw_icon_openai(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Classic emerald spiral loop for OpenAI."""
    cx, cy = w * 0.5, h * 0.5
    col = "#10a37f"
    sw = max(2, int(2.4 * s))
    # Draw 6 rotated rounded loops
    for i in range(6):
        angle = math.radians(i * 60)
        lx = cx + 3.5 * s * math.cos(angle)
        ly = cy + 3.5 * s * math.sin(angle)
        ex = cx + 9.0 * s * math.cos(angle + math.radians(45))
        ey = cy + 9.0 * s * math.sin(angle + math.radians(45))
        d.line([(lx, ly), (ex, ey)], fill=col, width=sw)
    d.ellipse([cx - 2.5*s, cy - 2.5*s, cx + 2.5*s, cy + 2.5*s], fill=col)


def _draw_icon_antigravity(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Graceful emerald leaf icon for Antigravity."""
    cx, cy = w * 0.5, h * 0.5
    col = "#10b981"
    # Curved leaf petal
    pts = [
        (cx - 7 * s, cy + 7 * s),
        (cx - 4 * s, cy - 4 * s),
        (cx + 7 * s, cy - 7 * s),
        (cx + 6 * s, cy + 3 * s),
        (cx - 2 * s, cy + 6 * s),
    ]
    d.polygon(pts, fill=col)
    # Leaf stem
    d.line([(cx - 7 * s, cy + 7 * s), (cx + 4 * s, cy - 4 * s)], fill="#ffffff", width=max(1, int(1.5 * s)))


def _draw_icon_claude(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Anthropic Claude orange sunburst / spark."""
    cx, cy = w * 0.5, h * 0.5
    col = "#d97706"
    sw = max(2, int(2.2 * s))
    # 8-ray asterisk with rounded ends
    for i in range(8):
        rad = math.radians(i * 45)
        d.line([(cx - 8*s*math.cos(rad), cy - 8*s*math.sin(rad)), (cx + 8*s*math.cos(rad), cy + 8*s*math.sin(rad))], fill=col, width=sw)
    d.ellipse([cx - 2.5*s, cy - 2.5*s, cx + 2.5*s, cy + 2.5*s], fill=col)


def _draw_icon_aider(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Aider chainlink in slate gray."""
    cx, cy = w * 0.5, h * 0.5
    col = "#475569"
    sw = max(2, int(2.5 * s))
    d.line([(cx - 6*s, cy - 2*s), (cx - 1*s, cy - 7*s)], fill=col, width=sw)
    d.line([(cx - 6*s, cy - 2*s), (cx - 3*s, cy + 2*s)], fill=col, width=sw)
    d.line([(cx + 1*s, cy - 3*s), (cx + 6*s, cy + 2*s)], fill=col, width=sw)
    d.line([(cx + 3*s, cy + 7*s), (cx + 6*s, cy + 2*s)], fill=col, width=sw)
    d.line([(cx - 3*s, cy - 3*s), (cx + 3*s, cy + 3*s)], fill=col, width=sw)


def _draw_icon_github(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """GitHub Octocat silhouette."""
    cx, cy = w * 0.5, h * 0.5
    col = "#181717"
    r = 7.5 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    # Cat ears
    d.polygon([(cx - 6*s, cy - 5*s), (cx - 3*s, cy - 8*s), (cx - 1*s, cy - 5*s)], fill=col)
    d.polygon([(cx + 6*s, cy - 5*s), (cx + 3*s, cy - 8*s), (cx + 1*s, cy - 5*s)], fill=col)
    # Inner face cut
    d.ellipse([cx - 4.5*s, cy - 2*s, cx + 4.5*s, cy + 5*s], fill="#ffffff")
    d.ellipse([cx - 2.8*s, cy - 0.5*s, cx - 1.2*s, cy + 1.5*s], fill=col)
    d.ellipse([cx + 1.2*s, cy - 0.5*s, cx + 2.8*s, cy + 1.5*s], fill=col)


def _draw_icon_copy(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Copy clipboard icon."""
    cx, cy = w * 0.5, h * 0.5
    col = "#64748b"
    sw = max(1, int(1.4 * s))
    # Back rectangle
    d.rectangle([cx - 2*s, cy - 5*s, cx + 5*s, cy + 2*s], outline=col, width=sw)
    # Front rectangle
    d.rectangle([cx - 5*s, cy - 2*s, cx + 2*s, cy + 5*s], outline=col, width=sw, fill="#ffffff")


def _draw_icon_more(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Vertical 3 dots."""
    cx, cy = w * 0.5, h * 0.5
    col = "#64748b"
    r = 1.4 * s
    d.ellipse([cx - r, cy - 5*s - r, cx + r, cy - 5*s + r], fill=col)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    d.ellipse([cx - r, cy + 5*s - r, cx + r, cy + 5*s + r], fill=col)


def _draw_icon_doc(d: ImageDraw.ImageDraw, w: int, h: int, s: int):
    """Document icon for view logs."""
    cx, cy = w * 0.5, h * 0.5
    col = "#475569"
    sw = max(1, int(1.5 * s))
    d.rectangle([cx - 4*s, cy - 6*s, cx + 4*s, cy + 6*s], outline=col, width=sw)
    d.line([(cx - 2*s, cy - 3*s), (cx + 2*s, cy - 3*s)], fill=col, width=sw)
    d.line([(cx - 2*s, cy), (cx + 2*s, cy)], fill=col, width=sw)
    d.line([(cx - 2*s, cy + 3*s), (cx + 1*s, cy + 3*s)], fill=col, width=sw)


# ==============================================================================
# Public Asset Accessor
# ==============================================================================

def get_icon(name: str, size: Tuple[int, int] = (24, 24)) -> ImageTk.PhotoImage:
    """Retrieves or creates a cached Tkinter PhotoImage icon."""
    key = f"{name}_{size[0]}x{size[1]}"
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]

    pil_img = get_pil_icon(name, size)
    photo = ImageTk.PhotoImage(pil_img)
    _IMAGE_CACHE[key] = photo
    return photo


def get_pil_icon(name: str, size: Tuple[int, int] = (24, 24)) -> Image.Image:
    """Retrieves or creates a cached PIL Image icon."""
    key = f"{name}_{size[0]}x{size[1]}"
    if key in _PIL_CACHE:
        return _PIL_CACHE[key]

    draw_map = {
        "logo_rocket": _draw_logo_rocket,
        "tg_circle": _draw_telegram_circle,
        "human_circle": _draw_human_circle,
        "code_circle": _draw_code_circle,
        "chat_circle": _draw_chat_circle,
        "bot_green": lambda d, w, h, s: _draw_bot_circle(d, w, h, s, "#e2f6eb", "#0e9f6e"),
        "bot_orange": lambda d, w, h, s: _draw_bot_circle(d, w, h, s, "#fef3c7", "#d97706"),
        "bot_purple": lambda d, w, h, s: _draw_bot_circle(d, w, h, s, "#ede9fe", "#7c3aed"),
        "lightbulb": _draw_lightbulb,
        "users": _draw_users_icon,
        "radio_checked": lambda d, w, h, s: _draw_radio(d, w, h, s, True),
        "radio_unchecked": lambda d, w, h, s: _draw_radio(d, w, h, s, False),
        "dot_green": lambda d, w, h, s: _draw_status_dot(d, w, h, s, "#10b981"),
        "dot_gray": lambda d, w, h, s: _draw_status_dot(d, w, h, s, "#94a3b8"),
        "dot_red": lambda d, w, h, s: _draw_status_dot(d, w, h, s, "#ef4444"),
        "service_tg": lambda d, w, h, s: _draw_service_icon(d, w, h, s, "tg"),
        "service_web": lambda d, w, h, s: _draw_service_icon(d, w, h, s, "web"),
        "service_ext": lambda d, w, h, s: _draw_service_icon(d, w, h, s, "ext"),
        "service_agent": lambda d, w, h, s: _draw_service_icon(d, w, h, s, "agent"),
        "gear": _draw_gear,
        "trash": _draw_trash,
        "link": _draw_link,
        "reload": _draw_reload,
        "white_plane": _draw_white_plane,
        "white_plus": _draw_white_plus,
        "human_green_small": lambda d, w, h, s: _draw_mini_silhouette(d, w, h, s, True, "#059669"),
        "bot_green_small": lambda d, w, h, s: _draw_mini_silhouette(d, w, h, s, False, "#059669"),
        "human_gray_small": lambda d, w, h, s: _draw_mini_silhouette(d, w, h, s, True, "#94a3b8"),
        "bot_gray_small": lambda d, w, h, s: _draw_mini_silhouette(d, w, h, s, False, "#94a3b8"),
        "icon_openai": _draw_icon_openai,
        "icon_antigravity": _draw_icon_antigravity,
        "icon_claude": _draw_icon_claude,
        "icon_aider": _draw_icon_aider,
        "icon_github": _draw_icon_github,
        "icon_copy": _draw_icon_copy,
        "icon_more": _draw_icon_more,
        "icon_doc": _draw_icon_doc,
    }

    fn = draw_map.get(name)
    if not fn:
        img = Image.new("RGBA", size, (0, 0, 0, 0))
    else:
        img = render_supersampled(fn, size=size)

    _PIL_CACHE[key] = img
    return img

