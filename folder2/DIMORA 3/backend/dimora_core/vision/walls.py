"""Wall-first floor-plan reading (v2) — works on CAD drawings, coloured/watercolour plans and scans.

 1. Normalise: scale to ~1400 px, treat strongly coloured ink (red icons, blue fills) as paper.
 2. Wall thickness T: median-filter the grey image (removes text, furniture and thin lines,
    fills speckled/textured walls), measure stroke widths with a distance transform.
 3. Wall mask: median filter sized to T + threshold; drop small blobs (leftover text).
 4. Openings: directional morphological closing (horizontal and vertical line kernels ≈ 12·T)
    joins collinear wall pieces across doors and windows. Every bridged gap is kept as an
    opening and classified as window (thin parallel lines across it) or door (empty).
 5. Footprint: a stronger closing + hole filling gives the building outline, so open sides
    (balcony railings) don't leak rooms into the outside.
 6. Spaces: connected components of the free area inside the footprint.
 7. Rectangles: each space is cut into axis-aligned rectangles with the classic
    "largest rectangle in a histogram" (monotonic stack) algorithm, repeatedly.
    Pieces of one space share a `group`, i.e. an open-plan area with no wall between them.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.cluster.hierarchy import DisjointSet

from .rooms import CVResult

WORK = 1400  # working resolution (long side)


def _odd(v: float) -> int:
    v = int(round(v))
    return v if v % 2 else v + 1


def normalise(img: np.ndarray):
    h, w = img.shape[:2]
    s = WORK / max(h, w)
    small = cv2.resize(img, (max(8, round(w * s)), max(8, round(h * s))), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = np.where(hsv[:, :, 1] > 90, 255, gray).astype(np.uint8)  # coloured ink ≠ wall
    return small, gray, s


def wall_threshold(gray: np.ndarray) -> int:
    otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(np.clip(otsu, 100, 165))


def estimate_thickness(gray: np.ndarray, thr: int):
    """Stroke width of walls: distance transform on a lightly median-filtered mask; walls are
    the thick strokes, so take upper percentiles of ridge widths among strokes ≥ 4 px."""
    m = (cv2.medianBlur(gray, 5) < thr).astype(np.uint8)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    ridge = (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8))) & (dt >= 2)
    wv = 2 * dt[ridge]
    wv = wv[wv >= 4]
    if len(wv) < 20:
        return 6.0, 9.0
    return float(np.percentile(wv, 50)), float(np.percentile(wv, 90))


def wall_mask(gray: np.ndarray, thr: int, T: float) -> np.ndarray:
    k = _odd(np.clip(T * 0.75, 3, 21))
    m = (cv2.medianBlur(gray, k) < thr).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        keep[i] = max(w, h) >= 4 * T and a >= 3 * T * T  # wall pieces are long; text blobs are not
    return keep[lab].astype(np.uint8)


def largest_rectangle(mask: np.ndarray):
    """Maximal all-ones axis-aligned rectangle in a binary matrix.

    Row by row, keep a histogram of consecutive ones above each column, and solve
    'largest rectangle in a histogram' with a monotonic stack. O(rows · cols).
    Returns (area, top, left, bottom, right) in cell units (bottom/right exclusive).
    """
    rows, cols = mask.shape
    heights = np.zeros(cols, int)
    best = (0, 0, 0, 0, 0)
    for r in range(rows):
        heights = np.where(mask[r], heights + 1, 0)
        stack: list[int] = []
        for c in range(cols + 1):
            h = heights[c] if c < cols else 0
            start = c
            while stack and heights[stack[-1]] >= h:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = heights[top] * (c - left)
                if area > best[0]:
                    best = (int(area), r - heights[top] + 1, left, r + 1, c)
                start = left
            stack.append(c)
    return best


def decompose(region: np.ndarray, cell: int, min_side_cells: int, min_frac: float = 0.04):
    """Cut a free-space region into rectangles (greedy, largest first)."""
    H, W = region.shape
    gh, gw = H // cell, W // cell
    if gh == 0 or gw == 0:
        return []
    grid = region[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).mean(axis=(1, 3)) > 0.5
    total = grid.sum()
    rects = []
    while grid.sum() > total * min_frac and len(rects) < 12:
        area, t, l, b, r = largest_rectangle(grid)
        # the first piece must be room-sized; later pieces (hall corners, alcoves) may be narrower
        need = min_side_cells if not rects else max(2, int(min_side_cells * 0.7))
        if area == 0 or min(b - t, r - l) < need or (rects and area < need * need * 1.5):
            break
        rects.append((l * cell, t * cell, r * cell, b * cell))
        grid[t:b, l:r] = False
    return rects


def _gap_kind(ink: np.ndarray, x0, y0, x1, y1, horizontal: bool, T: float) -> str:
    """Window = thin lines run along the gap (continuous ink across its length); door = empty."""
    pad = int(T * 0.6)
    if horizontal:
        band = ink[max(0, y0 - pad): y1 + pad, x0:x1]
        prof = (band.sum(axis=0) >= 2) if band.size else np.zeros(1)
    else:
        band = ink[y0:y1, max(0, x0 - pad): x1 + pad]
        prof = (band.sum(axis=1) >= 2) if band.size else np.zeros(1)
    return "window" if prof.mean() > 0.7 else "door"


def _door_evidence(ink, walls, x0, y0, x1, y1, horizontal: bool, T: float) -> bool:
    """A real doorway has its swing arc / leaf drawn beside it: thin ink in the square next to
    the gap on at least one side. An aligned-walls gap across a corridor has empty floor."""
    H, W = ink.shape
    Lg = (x1 - x0) if horizontal else (y1 - y0)
    thin = ink.astype(bool) & ~(walls > 0)
    if horizontal:
        sides = [thin[max(0, y0 - Lg): y0, x0: x1], thin[y1: min(H, y1 + Lg), x0: x1]]
    else:
        sides = [thin[y0: y1, max(0, x0 - Lg): x0], thin[y0: y1, x1: min(W, x1 + Lg)]]
    return any(sq.size and sq.mean() > 0.012 for sq in sides)


def detect_plan(img: np.ndarray) -> CVResult:
    small, gray, s = normalise(img)
    H, W = gray.shape
    thr = wall_threshold(gray)
    t_int, t_ext = estimate_thickness(gray, thr)
    T = max(4.0, t_int)
    walls = wall_mask(gray, thr, T)
    ink = (gray < thr).astype(np.uint8)  # all thin ink, for window detection

    # 4. openings: close gaps only *along* walls. Split the mask into horizontal and vertical
    #    wall pieces (opening with long line kernels), close each along its own axis, and keep
    #    only bridged gaps that are about one wall thick — so corridors are never filled.
    L = int(np.clip(T * 16, 25, W * 0.2))
    run = max(5, int(T * 3))
    hw = cv2.morphologyEx(walls, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (run, 1)))
    vw = cv2.morphologyEx(walls, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, run)))
    # close along each axis from the walls running that way, but let any wall cap a gap
    # (a door right beside a corner ends at a perpendicular wall)
    # (a) between two walls running the same way: up to L (wide windows, double doors)
    ch = cv2.morphologyEx(hw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1)))
    cvv = cv2.morphologyEx(vw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, L)))
    # (b) a wall ending at a perpendicular wall (door next to a corner): only door-sized gaps
    Lc = int(np.clip(T * 8, 15, L))
    ch |= cv2.morphologyEx(hw | cv2.dilate(vw, np.ones((1, 3), np.uint8)), cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (Lc, 1))) & \
        cv2.dilate(hw, cv2.getStructuringElement(cv2.MORPH_RECT, (Lc, 1)))
    cvv |= cv2.morphologyEx(vw | cv2.dilate(hw, np.ones((3, 1), np.uint8)), cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, Lc))) & \
        cv2.dilate(vw, cv2.getStructuringElement(cv2.MORPH_RECT, (1, Lc)))
    wb = walls > 0
    closed = walls.copy()
    openings, gaps = [], []
    for gmask, horiz in (((ch > 0) & ~wb, True), ((cvv > 0) & ~wb, False)):
        n, lab_g, st, _ = cv2.connectedComponentsWithStats(gmask.astype(np.uint8), connectivity=4)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            length, thick = (w, h) if horiz else (h, w)
            if thick > 1.8 * T or length < 1.5 * T:
                continue
            # seal across the full wall thickness (walls drawn by hand rarely line up exactly)
            k = max(3, int(2 * T)) | 1
            m = cv2.dilate((lab_g == i).astype(np.uint8), np.ones((k, 1) if horiz else (1, k), np.uint8)) > 0
            closed[m] = 1  # seal everything first, so the building outline is closed
            if thick < 0.5 * T or length < 3 * T:
                continue  # hairline / tiny notch: stays sealed, not an opening
            g = {"x0": int(x), "y0": int(y), "x1": int(x + w), "y1": int(y + h), "o": "h" if horiz else "v",
                 "kind": _gap_kind(ink, x, y, x + w, y + h, horiz, T), "evidence": _door_evidence(ink, walls, x, y, x + w, y + h, horiz, T)}
            gaps.append((g, m))

    # 5. footprint (building outline) = walls + long thin straight lines (railings, window bands)
    #    that lie within the walls' extent; then fill holes by flooding the outside from the border.
    n0, lab0, st0, _ = cv2.connectedComponentsWithStats(walls, connectivity=8)
    if n0 > 1:
        big = st0[1:, 4].max()
        main = [i for i in range(1, n0) if st0[i][4] >= big * 0.05]
        bx0 = min(st0[i][0] for i in main); by0 = min(st0[i][1] for i in main)
        bx1 = max(st0[i][0] + st0[i][2] for i in main); by1 = max(st0[i][1] + st0[i][3] for i in main)
    else:
        bx0, by0, bx1, by1 = 0, 0, W, H
    run_l = max(9, int(T * 6))
    bridge = max(9, int(T * 6))  # sketchy lines are broken: re-join along their own axis
    wob = max(3, int(T / 3)) | 1  # tolerate hand-drawn wobble: thicken across the line first
    lh = cv2.morphologyEx(cv2.dilate(ink, np.ones((wob, 1), np.uint8)), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (run_l, 1)))
    lv = cv2.morphologyEx(cv2.dilate(ink, np.ones((1, wob), np.uint8)), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, run_l)))
    lines = cv2.morphologyEx(lh, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (bridge, 1))) | \
        cv2.morphologyEx(lv, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, bridge)))
    box = np.zeros_like(walls)
    pad = int(T)
    box[max(0, by0 - pad):by1 + pad, max(0, bx0 - pad):bx1 + pad] = 1
    src = closed | (lines & box)
    K = _odd(T * 4)
    src = cv2.morphologyEx(src, cv2.MORPH_CLOSE, np.ones((K, K), np.uint8))
    flood = (src == 0).astype(np.uint8)
    nf, labf = cv2.connectedComponents(flood, connectivity=4)
    border = set(np.unique(np.concatenate([labf[0], labf[-1], labf[:, 0], labf[:, -1]]))) - {0}
    foot = (~np.isin(labf, list(border))).astype(np.uint8) if border else np.ones_like(walls)
    foot &= box

    # inside gaps with no door/window drawn (aligned walls across a corridor) are passages: reopen them
    for g, m in gaps:
        d = int(T * 1.6)
        cx, cy = (g["x0"] + g["x1"]) // 2, (g["y0"] + g["y1"]) // 2
        p1, p2 = ((cx, g["y0"] - d), (cx, g["y1"] + d)) if g["o"] == "h" else ((g["x0"] - d, cy), (g["x1"] + d, cy))
        inside = [0 <= px < W and 0 <= py < H and foot[py, px] > 0 for px, py in (p1, p2)]
        g["exterior"] = inside[0] != inside[1]
        if not g["exterior"] and g["kind"] == "door" and not g["evidence"]:
            closed[m & ~wb] = 0
            continue
        openings.append({k: v for k, v in g.items() if k != "evidence"})

    # 6. spaces = free area inside the footprint, after closing openings
    seal = _odd(max(3, T * 0.6))  # hand-drawn walls that don't quite meet: seal hairline slits
    closed = cv2.morphologyEx(closed, cv2.MORPH_CLOSE, np.ones((seal, seal), np.uint8))
    free = ((closed == 0) & (foot == 1)).astype(np.uint8)
    free = cv2.morphologyEx(free, cv2.MORPH_OPEN, np.ones((seal, seal), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    min_area = (6 * T) ** 2
    cell = max(2, int(T / 2))
    rooms = []
    grow = int(round(T / 2))
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if a < min_area or min(w, h) < 2.5 * T or x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        region = (lab[y:y + h, x:x + w] == i)
        rects = decompose(region, cell, max(2, int(3.5 * T / cell)))
        if not rects:
            continue
        group = f"g{i}" if len(rects) > 1 else None
        for (l, t, r, b) in rects:
            rooms.append({"x0": max(0, x + l - grow), "y0": max(0, y + t - grow), "x1": min(W, x + r + grow), "y1": min(H, y + b + grow),
                          "group": group, "fill": 1.0})
    # balconies / terraces: an outside side with no solid wall (railing or open edge)
    band = max(2, int(T))
    for q in rooms:
        for side in ("n", "s", "w", "e"):
            if side in ("n", "s"):
                y = q["y0"] if side == "n" else q["y1"]
                strip = closed[max(0, y - band): y + band, q["x0"]: q["x1"]].max(axis=0) if q["x1"] > q["x0"] else np.zeros(1)
                oy = y - 3 * band if side == "n" else y + 3 * band
                outside = not (0 <= oy < H) or foot[oy, (q["x0"] + q["x1"]) // 2] == 0
            else:
                x = q["x0"] if side == "w" else q["x1"]
                strip = closed[q["y0"]: q["y1"], max(0, x - band): x + band].max(axis=1) if q["y1"] > q["y0"] else np.zeros(1)
                ox = x - 3 * band if side == "w" else x + 3 * band
                outside = not (0 <= ox < W) or foot[(q["y0"] + q["y1"]) // 2, ox] == 0
            if outside and strip.size and strip.mean() < 0.35:
                q["open_side"] = True
    res = CVResult(W=W, H=H, rooms=rooms, int_px=t_int, ext_px=t_ext, dark=float(walls.mean()), scale=s,
                   openings=openings, method="walls-v2")
    res.stairs = detect_stairs(ink, rooms, T)
    return res


def detect_stairs(ink: np.ndarray, rooms, T: float):
    """Staircases without AI: many evenly spaced parallel lines (treads) across a room area.

    Project the ink in the room onto one axis; a tread is a row/column with ink across ≥ 60% of
    the width. ≥ 7 treads at a regular spacing (1.2–4.5 × wall thickness, coefficient of variation
    < 0.35) spanning ≥ 1.5 m-ish → a stair box."""
    out = []
    pad = int(T)
    for r in rooms:
        x0, y0, x1, y1 = r["x0"] + pad, r["y0"] + pad, r["x1"] - pad, r["y1"] - pad
        if x1 - x0 < 3 * T or y1 - y0 < 3 * T:
            continue
        sub = ink[y0:y1, x0:x1]
        for axis in (0, 1):  # axis 0: treads are horizontal lines (stack along y)
            prof = sub.mean(axis=1) if axis == 0 else sub.mean(axis=0)
            on = prof > 0.6
            runs, i = [], 0
            while i < len(on):
                if on[i]:
                    j = i
                    while j < len(on) and on[j]:
                        j += 1
                    runs.append((i + j) / 2)
                    i = j
                else:
                    i += 1
            if len(runs) < 7:
                continue
            gaps = np.diff(runs)
            m = float(np.median(gaps))
            regular = gaps[(gaps > 0.6 * m) & (gaps < 1.5 * m)]
            if not (1.2 * T <= m <= 4.5 * T) or len(regular) < 6 or float(np.std(regular) / max(1e-6, np.mean(regular))) > 0.35:
                continue
            a, b = runs[0], runs[-1]
            if b - a < 8 * T:
                continue
            box = (x0, y0 + int(a), x1, y0 + int(b)) if axis == 0 else (x0 + int(a), y0, x0 + int(b), y1)
            out.append({"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]})
            break
    return out


def plan_boxes(img: np.ndarray):
    """Separate floor-plan drawings on a sheet, using walls only (frames, grids and titles are thin)."""
    small, gray, s = normalise(img)
    H, W = gray.shape
    thr = wall_threshold(gray)
    T = max(4.0, estimate_thickness(gray, thr)[0])
    walls = wall_mask(gray, thr, T)
    k = _odd(T * 10)  # wider than a doorway, narrower than the white space between two drawings
    m = cv2.dilate(walls, np.ones((k, k), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    comps = [(int(st[i][0]), int(st[i][1]), int(st[i][0] + st[i][2]), int(st[i][1] + st[i][3]), int(st[i][4])) for i in range(1, n)]
    if not comps:
        return [{"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}]
    big = max(c[4] for c in comps)
    plans = [c for c in comps if c[4] >= big * 0.25 and min(c[2] - c[0], c[3] - c[1]) >= min(W, H) * 0.15]
    # boxes that overlap belong to one drawing: merge them with a disjoint set
    ds = DisjointSet(range(len(plans)))
    for i in range(len(plans)):
        for j in range(i + 1, len(plans)):
            a, b = plans[i], plans[j]
            if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                ds.merge(i, j)
    plans = [(min(plans[i][0] for i in g), min(plans[i][1] for i in g), max(plans[i][2] for i in g), max(plans[i][3] for i in g), sum(plans[i][4] for i in g)) for g in ds.subsets()]
    plans.sort(key=lambda c: (round(c[1] / (H * 0.2)), c[0]))
    pad = int(T)
    return [{"x0": max(0, c[0] - pad) / W, "y0": max(0, c[1] - pad) / H, "x1": min(W, c[2] + pad) / W, "y1": min(H, c[3] + pad) / H} for c in plans]
