"""
Department of Technology  -  GROUND FLOOR ONLY  -  procedural Blender reconstruction
=====================================================================================
Paste into Blender's Scripting workspace and press "Run Script".
Tested design target: Blender 3.6 - 4.x (uses only Blender's built-in Python API).

WHAT THIS SCRIPT BUILDS (and nothing else)
  * a roofless, single-storey cut-away: walls, floor slab, door/window openings
  * NO roof / ceiling / parapet / upper storey / staircase treads

SOURCE OF THE GEOMETRY
  Every coordinate below is in the PIXELS of the reference floor-map (1263 x 791 px).
  Room outlines, arc centres and arc radii were measured from the image
  (colour-segmented bounding boxes + circle fits on the blue theatre ring), then
  snapped onto a shared grid so neighbouring rooms share exactly the same wall line.
  PIXEL_TO_METER converts pixels to metres.

HOW THE WALLS ARE MADE
  1. Every room outline (and the corridor outline where it touches the outside)
     is cut into short pieces.  Each piece is probed on both sides: if one side
     is outside the building it is an EXTERIOR wall piece (thick), otherwise an
     INTERIOR partition (thin).
  2. Consecutive pieces of the same kind are merged into one polyline and turned
     into a mitred wall prism (so curved walls follow the plan, they are NOT
     bevelled boxes).
  3. All prisms are joined into one mesh; ONE exact Boolean pass unions the
     overlaps (clean T-junctions / corners) and subtracts every door/window
     opening at the same time.
  4. Wall faces are painted per room type (colours taken from the map legend).

EDITING
  Change the parameters in the "USER PARAMETERS" block.  Set SAVE_PATH to also
  write a .blend file, RENDER_ON_RUN = True to render a still image.
"""

import math
import os

import bpy
import bmesh
from mathutils import Vector, Matrix

# =============================================================================
# USER PARAMETERS
# =============================================================================
PIXEL_TO_METER = 0.05            # 1 px of the floor-map = 5 cm  (building ~ 60 m wide)

WALL_HEIGHT = 3.2                # open-top wall height (m)
EXTERIOR_WALL_THICKNESS = 0.30   # walls on the building perimeter (m)
INTERIOR_WALL_THICKNESS = 0.15   # partitions (m)
FLOOR_THICKNESS = 0.20           # structural slab thickness (m)
ROOM_FINISH_THICKNESS = 0.01     # raised room finish above the corridor slab (m)
BEVEL_AMOUNT = 0.008             # architectural edge softening (m)

DOOR_WIDTH = 1.0                 # default single door (m)
DOOR_HEIGHT = 2.1
DOOR_SWING_DEG = 72.0            # how far the door leaves are drawn open (into the room)

ADD_WINDOWS = True               # NOTE: the map does not draw windows -> inferred on
WINDOW_WIDTH = 1.5               # exterior faces of lecture/lab/office rooms only.
WINDOW_HEIGHT = 1.3              # Set False to keep strictly what the map shows.
WINDOW_SILL = 0.9
WINDOW_SPACING = 2.8             # target centre-to-centre spacing (m)
WINDOW_MIN_RUN = 2.4             # wall must be at least this long to get a window (m)

ADD_STAIR_DRUMS = True           # the two round stair cores of the plan (walls only, no treads)
ADD_GROUND = True                # large ground plane for context (not part of the building)

ARC_STEP_DEG = 5.0               # curve tessellation of the plan arcs

RENDER_ENGINE = "CYCLES"         # "CYCLES" or "EEVEE"
RENDER_SAMPLES = 128
RESOLUTION = (1920, 1080)
CAMERA_ELEVATION_DEG = 52.0      # elevated cut-away view
CAMERA_AZIMUTH_DEG = 0.0         # 0 = looking north (plan "up") from the open courtyard side
CAMERA_DISTANCE = 72.0
CAMERA_LENS = 35.0

SAVE_PATH = ""                   # e.g. "C:/temp/ground_floor.blend"  ("" = do not save)
RENDER_ON_RUN = False            # True -> also render RENDER_PATH
RENDER_PATH = ""                 # e.g. "C:/temp/ground_floor.png"

UNION_WALLS = True               # exact Boolean union/openings (slower but clean)

# =============================================================================
# FLOOR-MAP DATA  (pixel coordinates of the reference image, y pointing DOWN)
# =============================================================================
IMG_CENTER = (638.0, 385.0)      # building centre -> becomes the world origin

# ---- shared grid lines (measured) ------------------------------------------
Y_TOP = 71.5        # top edge of the northern rooms
Y_BAND = 194.0      # bottom of D-8 / D-23 / D-24 ... = upper edge of the corridor band
Y_IN = 345.0        # courtyard face of the inner room row
X_LC = 150.5        # left corridor edge
X_RC = 1134.0       # right corridor edge

# ---- the two curved lecture-theatre rings ------------------------------------
CL, RL_IN, RL_OUT = (274.0, 317.5), 123.5, 229.0      # left ring  (Lt-9, Lt-10, Lt-11)
CR, RR_IN, RR_OUT = (1016.0, 312.0), 118.0, 224.0     # right ring (Lt-12, Lt-13, Lt-14)


def arc_points(c, r, a0, a1, step=ARC_STEP_DEG):
    """Points of a circular arc in pixel space (angles in degrees, counter-clockwise on
    screen, i.e. 90 deg = up in the image)."""
    n = max(1, int(math.ceil(abs(a1 - a0) / step)))
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((c[0] + r * math.cos(a), c[1] - r * math.sin(a)))
    return pts


def sector(c, r_in, r_out, a_out, a_in):
    """Annular sector. a_out / a_in = (start,end) angles of the outer / inner arc."""
    outer = arc_points(c, r_out, a_out[0], a_out[1])
    inner = arc_points(c, r_in, a_in[1], a_in[0])
    pts = outer + inner
    arc = [True] * (len(outer) - 1) + [False] + [True] * (len(inner) - 1) + [False]
    return pts, arc


def asin_deg(v):
    return math.degrees(math.asin(v))


ROOMS = []      # every room / zone
PATCHES = []    # open circulation (corridor) floor areas


def add_room(name, kind, pts, arc=None):
    ROOMS.append(dict(name=name, kind=kind, pts=[tuple(p) for p in pts],
                      arc=arc if arc else [False] * len(pts)))


def add_rect(name, kind, x0, y0, x1, y1):
    add_room(name, kind, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def add_patch(name, pts, arc=None):
    PATCHES.append(dict(name=name, kind="corridor", pts=[tuple(p) for p in pts],
                        arc=arc if arc else [False] * len(pts)))


# ---- curved lecture theatres (blue) -------------------------------------------
_yb = 323.5                                   # D-4 top = lower end of Lt-9
_ao = 180.0 + asin_deg((_yb - CL[1]) / RL_OUT)
_ai = 180.0 + asin_deg((_yb - CL[1]) / RL_IN)
for nm, ao, ai in (("Lt-9", (150.0, _ao), (150.0, _ai)),
                   ("Lt-10", (120.0, 150.0), (120.0, 150.0)),
                   ("Lt-11", (90.0, 120.0), (90.0, 120.0))):
    p, a = sector(CL, RL_IN, RL_OUT, ao, ai)
    add_room(nm, "lecture", p, a)

_xv = 1008.0                                  # D-24 right edge = left end of Lt-12
_ao = 90.0 + asin_deg((CR[0] - _xv) / RR_OUT)
_ai = 90.0 + asin_deg((CR[0] - _xv) / RR_IN)
_y14 = 314.5                                  # D-28 top = lower end of Lt-14
_bo = -asin_deg((_y14 - CR[1]) / RR_OUT)
_bi = -asin_deg((_y14 - CR[1]) / RR_IN)
for nm, ao, ai in (("Lt-12", (60.0, _ao), (60.0, _ai)),
                   ("Lt-13", (30.0, 60.0), (30.0, 60.0)),
                   ("Lt-14", (_bo, 30.0), (_bi, 30.0))):
    p, a = sector(CR, RR_IN, RR_OUT, ao, ai)
    add_room(nm, "lecture", p, a)

# ---- north-west block -----------------------------------------------------------
add_rect("D-8", "lecture", 274.0, Y_TOP, 350.5, Y_BAND)
add_rect("X-1", "shaft", 350.5, Y_TOP, 386.5, 124.5)
add_rect("Cabin-A", "cabin", 350.5, 124.5, 386.5, 159.0)
add_rect("Cabin-B", "cabin", 350.5, 159.0, 386.5, Y_BAND)
add_rect("T-4", "tutorial", 386.5, 124.5, 434.0, Y_BAND)

# ---- northern row ------------------------------------------------------------------
add_rect("Cabin-C", "cabin", 470.0, Y_TOP, 503.0, 141.5)
add_rect("T-5", "tutorial", 503.0, Y_TOP, 548.5, 141.5)

HOD_C, HOD_R = (635.5, 318.5), 268.0          # shallow arched roof-line of the HOD block


def _hod_top(x):
    return HOD_C[1] - math.sqrt(HOD_R ** 2 - (x - HOD_C[0]) ** 2)


_hx = [548.5, 577.5, 606.5, 664.5, 693.5, 723.5]
for i, nm in enumerate(("Cabin-D1", "Cabin-D2", "HOD", "Cabin-D4", "Cabin-D5")):
    xa, xb = _hx[i], _hx[i + 1]
    k = max(2, int(round((xb - xa) / 20.0)))
    top = [(xb - (xb - xa) * j / k, _hod_top(xb - (xb - xa) * j / k)) for j in range(k + 1)]
    pts = [(xa, 117.5), (xb, 117.5)] + top
    arc = [False, False] + [True] * (len(top) - 1) + [False]
    add_room(nm, "cabin", pts, arc)

add_rect("T-6", "tutorial", 723.5, Y_TOP, 768.5, 141.5)
add_rect("Cabin-E", "cabin", 768.5, Y_TOP, 802.0, 141.5)
add_rect("Cabin-F", "cabin", 802.0, Y_TOP, 838.5, 141.5)
add_rect("X-2", "shaft", 838.5, Y_TOP, 924.5, 121.5)
add_rect("Locker", "locker", 838.5, 121.5, 889.0, Y_BAND)
add_rect("D-23", "cabin", 889.0, 121.5, 924.5, Y_BAND)
add_rect("D-24", "lab", 924.5, Y_TOP, 1008.0, Y_BAND)

# domed hall in the middle of the corridor (white, unlabelled in the map)
_dc, _dr = (640.0, 221.7), 80.2
_dome = [(553.5, 161.5), (587.0, 161.5)]
for j in range(1, 14):                       # dome arc, peak at y = 141.5
    x = 587.0 + (693.0 - 587.0) * j / 13.0
    _dome.append((x, _dc[1] - math.sqrt(_dr ** 2 - (x - _dc[0]) ** 2)))
_dome += [(721.5, 161.5), (721.5, 198.5), (553.5, 198.5)]
add_room("Hall", "hall", _dome)

# ---- west wing -------------------------------------------------------------------------
add_rect("D-4", "lecture", 34.5, 323.5, X_LC, 390.5)
add_rect("Cabin-L1", "cabin", 77.5, 390.5, X_LC, 425.5)
add_rect("D-2", "white", 77.5, 425.5, X_LC, 481.5)
add_rect("Lab-6", "lab", 77.5, 481.5, X_LC, 542.5)
add_rect("Library", "library", 34.5, 542.5, 226.0, 689.5)
add_room("Girls-L", "girls", [(181.5, 314.0), (197.0, 303.5), (262.0, 303.5),
                              (262.0, 352.0), (181.5, 352.0)])
add_rect("Cabin-L2", "cabin", 181.5, 352.0, 250.5, 389.5)
add_rect("Lab-5", "lab", 181.5, 389.5, 250.5, 461.5)
add_room("Boys-L", "boys", [(262.0, 238.0), (277.0, 223.0), (309.5, 223.0),
                            (309.5, 303.5), (262.0, 303.5)])
add_rect("X-3", "shaft", 262.0, 303.5, 309.5, Y_IN)

# ---- inner row (courtyard face) -----------------------------------------------------------
add_rect("Lab-1", "lab", 309.5, 262.0, 371.5, Y_IN)
add_rect("Lab-2", "lab", 371.5, 262.0, 434.0, Y_IN)
add_rect("Lab-8", "lab", 434.0, 223.0, 506.0, Y_IN)
add_room("Vest-L", "white", [(506.0, 223.0), (551.0, 223.0), (551.0, 317.5),
                             (536.5, 317.5), (536.5, 238.0), (506.0, 238.0)])
add_rect("Cell-L1", "cabin", 506.0, 238.0, 536.5, 263.5)
add_rect("Cell-L2", "cabin", 506.0, 263.5, 536.5, 289.5)
add_rect("Cell-L3", "cabin", 506.0, 289.5, 536.5, 317.5)
add_rect("Cabin-L3", "cabin", 506.0, 317.5, 551.0, Y_IN)
add_rect("Lab-7", "lab", 551.0, 223.0, 622.5, Y_IN)
add_rect("Lab-3", "lab", 653.5, 223.0, 725.5, 285.5)
add_rect("Lab-4", "lab", 653.5, 285.5, 725.5, Y_IN)
add_room("Vest-R", "white", [(725.5, 223.0), (770.5, 223.0), (770.5, 238.0),
                             (740.5, 238.0), (740.5, 316.0), (725.5, 316.0)])
add_rect("Cell-R1", "cabin", 740.5, 238.0, 770.5, 263.5)
add_rect("Cell-R2", "cabin", 740.5, 263.5, 770.5, 289.5)
add_rect("Cell-R3", "cabin", 740.5, 289.5, 770.5, 316.0)
add_rect("Cabin-R0", "cabin", 725.5, 316.0, 770.5, Y_IN)
add_rect("Lab-12", "lab", 770.5, 223.0, 842.5, Y_IN)
add_rect("Tech-Hall", "hall", 842.5, 259.0, 970.5, Y_IN)

# ---- east wing --------------------------------------------------------------------------------
add_room("Girls-R", "girls", [(970.5, 216.0), (1007.0, 216.0), (1032.5, 241.0), (1032.5, 294.0),
                              (999.0, 294.0), (999.0, Y_IN), (970.5, Y_IN)])
add_rect("X-4", "shaft", 999.0, 294.0, 1032.5, Y_IN)
add_room("Boys-R", "boys", [(1032.5, 293.5), (1088.5, 293.5), (1107.5, 311.5),
                            (1107.5, Y_IN), (1032.5, Y_IN)])
add_rect("EGMD", "lab", 1032.5, Y_IN, 1107.5, 447.5)
add_rect("Lab-35", "lecture", 1032.5, 447.5, 1107.5, 549.5)
add_rect("Lab-36", "lecture", 1032.5, 549.5, 1107.5, 639.5)
add_rect("W-1", "white", 1032.5, 639.5, 1107.5, 720.5)
add_rect("D-28", "lab", X_RC, 314.5, 1240.0, 380.5)
add_rect("Cabin-R1", "cabin", X_RC, 380.5, 1206.0, 419.5)
add_rect("X-5", "shaft", 1206.0, 380.5, 1240.0, 467.5)
add_rect("T-8", "tutorial", X_RC, 419.5, 1206.0, 464.5)
add_rect("Lab-9", "lab", X_RC, 464.5, 1177.5, 548.5)
add_rect("Cabin-R2", "cabin", 1177.5, 464.5, 1206.0, 527.5)
add_rect("Lab-10", "lab", 1206.0, 467.5, 1240.0, 548.5)
add_rect("Cabin-R3", "cabin", 1206.0, 548.5, 1240.0, 596.5)
add_rect("Cabin-R4", "cabin", X_RC, 567.5, 1186.0, 596.5)
add_rect("X-6", "shaft", 1143.5, 596.5, 1172.5, 635.5)
add_rect("Lab-11", "lab", 1172.5, 596.5, 1240.0, 635.5)
add_rect("W-2", "white", X_RC, 635.5, 1201.5, 681.5)

# ---- circulation (grey) areas -----------------------------------------------------------------
_d = arc_points(CL, RL_IN, 90.0, 180.0)
add_patch("Corr-L-disc", [CL] + _d, [False] + [True] * (len(_d) - 1) + [False])
_porch = arc_points((306.5, 689.5), 22.0, 90.0, 0.0)           # rounded end of the entrance porch
add_patch("Corr-L-strip",
          [(X_LC, 300.0), (250.5, 300.0), (250.5, 667.5)] + _porch + [(226.0, 689.5), (226.0, 542.5), (X_LC, 542.5)],
          [False, False, False] + [True] * (len(_porch) - 1) + [False, False, False, False])
add_patch("Corr-L-top", [(274.0, Y_BAND), (434.0, Y_BAND), (434.0, 262.0), (274.0, 262.0)])
add_patch("Corr-N-passage", [(386.5, Y_TOP), (470.0, Y_TOP), (470.0, 223.0),
                             (434.0, 223.0), (434.0, 124.5), (386.5, 124.5)])
add_patch("Corr-centre", [(470.0, 117.5), (838.5, 117.5), (838.5, 223.0), (470.0, 223.0)])
add_patch("Corr-gap", [(622.5, 223.0), (653.5, 223.0), (653.5, Y_IN), (622.5, Y_IN)])
add_patch("Corr-R-top", [(770.5, Y_BAND), (1016.0, Y_BAND), (1016.0, 259.0), (770.5, 259.0)])
_d = arc_points(CR, RR_IN, 0.0, 90.0)
add_patch("Corr-R-disc", [CR] + _d, [False] + [True] * (len(_d) - 1) + [False])
add_patch("Corr-R-strip", [(1107.5, 300.0), (X_RC, 300.0), (X_RC, 681.5), (1107.5, 681.5)])
add_patch("Corr-S1", [(X_RC, 548.5), (1206.0, 548.5), (1206.0, 567.5), (X_RC, 567.5)])
add_patch("Corr-S2", [(1177.5, 527.5), (1206.0, 527.5), (1206.0, 548.5), (1177.5, 548.5)])
add_patch("Corr-S3", [(1186.0, 567.5), (1206.0, 567.5), (1206.0, 596.5), (1186.0, 596.5)])
add_patch("Corr-S4", [(X_RC, 596.5), (1143.5, 596.5), (1143.5, 635.5), (X_RC, 635.5)])

# circular stair cores (drawn as round enclosures in the plan; walls only)
STAIR_DRUMS = [dict(c=(230.0, 271.0), r=28.5, open_deg=135.0),
               dict(c=(1056.0, 263.5), r=28.5, open_deg=45.0)]

# rooms that receive (inferred) windows on their exterior faces
WINDOW_KINDS = {"lecture", "lab", "cabin", "tutorial", "library", "hall"}

# ---- doors (door-free rooms: lift/duct shafts "X" boxes stay closed) -----------------------------
DOORS = []


def door(x, y, orient, into, w=DOOR_WIDTH, ext=False):
    """orient: 'H' wall runs along x, 'V' along y, or a (tx,ty) tangent. into = swing side (px dir)."""
    if orient == "H":
        t = (1.0, 0.0)
    elif orient == "V":
        t = (0.0, 1.0)
    else:
        t = orient
    DOORS.append(dict(p=(x, y), t=t, into=into, w=w, ext=ext))


N, S_, E, W_ = (0, -1), (0, 1), (1, 0), (-1, 0)

# curved lecture theatres - doors on the corridor (inner) arc
for a in (165.0, 135.0, 105.0):
    r = math.radians(a)
    door(CL[0] + RL_IN * math.cos(r), CL[1] - RL_IN * math.sin(r),
         (math.sin(r), math.cos(r)), (math.cos(r), -math.sin(r)), 1.2)
for a in (75.0, 45.0, 15.0):
    r = math.radians(a)
    door(CR[0] + RR_IN * math.cos(r), CR[1] - RR_IN * math.sin(r),
         (math.sin(r), math.cos(r)), (math.cos(r), -math.sin(r)), 1.2)

# north-west block
door(311.0, Y_BAND, "H", N)                    # D-8
door(368.5, 159.0, "H", N, 0.9)                # Cabin-A <-> Cabin-B (A has no corridor face)
door(368.5, Y_BAND, "H", N, 0.9)               # Cabin-B
door(434.0, 160.0, "V", W_)                    # T-4
# northern row
door(486.5, 141.5, "H", N, 0.9)                # Cabin-C
door(525.75, 141.5, "H", N)                    # T-5
for x in (563.0, 592.0, 635.5, 679.0, 708.5):  # HOD block cells
    door(x, 117.5, "H", N, 0.9)
door(746.0, 141.5, "H", N)                     # T-6
door(785.0, 141.5, "H", N, 0.9)                # Cabin-E
door(820.0, 141.5, "H", N, 0.9)                # Cabin-F
door(637.0, 198.5, "H", N, 1.2)                # domed hall
door(863.5, Y_BAND, "H", N)                    # locker room
door(907.0, Y_BAND, "H", N, 0.9)               # D-23
door(966.0, Y_BAND, "H", N, 1.2)               # D-24
# west wing
door(X_LC, 357.0, "V", W_)                     # D-4
door(X_LC, 408.0, "V", W_)                     # cabin
door(X_LC, 453.5, "V", W_)                     # D-2
door(X_LC, 512.0, "V", W_)                     # Lab-6
door(190.0, 542.5, "H", S_, 1.6)               # library (double leaf)
door(181.5, 333.0, "V", E)                     # girls washroom
door(181.5, 370.5, "V", E)                     # cabin
door(181.5, 425.5, "V", E)                     # Lab-5
door(293.0, 223.0, "H", S_)                    # boys washroom
# inner row
door(340.5, 262.0, "H", S_)                    # Lab-1
door(403.0, 262.0, "H", S_)                    # Lab-2
door(470.0, 223.0, "H", S_, 1.2)               # Lab-8
door(528.5, 223.0, "H", S_)                    # vestibule L
for y in (250.75, 276.5, 303.5):               # cells L
    door(536.5, y, "V", W_, 0.7)
door(543.75, 317.5, "H", S_, 0.6)
door(587.0, 223.0, "H", S_, 1.2)               # Lab-7
door(689.5, 223.0, "H", S_, 1.2)               # Lab-3
door(653.5, 315.0, "V", E)                     # Lab-4 (from the bay between Lab-7 and Lab-3/4)
door(748.0, 223.0, "H", S_)                    # vestibule R
for y in (250.75, 276.5, 303.5):               # cells R
    door(740.5, y, "V", E, 0.7)
door(733.0, 316.0, "H", S_, 0.6)
door(806.0, 223.0, "H", S_, 1.2)               # Lab-12
door(906.0, 259.0, "H", S_, 1.8)               # technology conference hall (double leaf)
# east wing
door(989.0, 216.0, "H", S_)                    # girls washroom
door(1107.5, 329.0, "V", W_)                   # boys washroom
door(1107.5, 396.0, "V", W_)                   # EGMD lab
door(1107.5, 498.5, "V", W_, 1.2)              # Lab-35
door(1107.5, 594.5, "V", W_, 1.2)              # Lab-36
door(1107.5, 660.0, "V", W_)                   # white room
door(X_RC, 347.0, "V", E)                      # D-28
door(X_RC, 400.0, "V", E)                      # cabin
door(X_RC, 442.0, "V", E)                      # T-8
door(X_RC, 506.0, "V", E)                      # Lab-9
door(1191.75, 527.5, "H", N, 0.9)              # Cabin-R2
door(1206.0, 538.0, "V", E, 0.8)               # Lab-10
door(1206.0, 558.0, "V", E, 0.7)               # cabin
door(1160.0, 567.5, "H", S_, 0.9)              # cabin
door(1196.0, 596.5, "H", S_, 0.8)              # Lab-11
door(X_RC, 658.0, "V", E)                      # white room
# main entrance ("Enter" arrow, rounded porch at the bottom-left)
door(262.0, 689.5, "H", N, 2.4, ext=True)

# =============================================================================
# 2D GEOMETRY HELPERS (pure python, no Blender needed)
# =============================================================================


def to_world(p):
    return ((p[0] - IMG_CENTER[0]) * PIXEL_TO_METER, -(p[1] - IMG_CENTER[1]) * PIXEL_TO_METER)


def to_px(x, y):
    return (x / PIXEL_TO_METER + IMG_CENTER[0], -y / PIXEL_TO_METER + IMG_CENTER[1])


def vec_to_world(v):
    return (v[0], -v[1])


def unit(v):
    l = math.hypot(v[0], v[1])
    return (v[0] / l, v[1] / l) if l > 1e-12 else (1.0, 0.0)


def point_in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class PolyIndex:
    """Fast 'is this pixel inside any polygon' lookup."""

    def __init__(self, polys):
        self.items = []
        for p in polys:
            xs = [q[0] for q in p]
            ys = [q[1] for q in p]
            self.items.append((min(xs), min(ys), max(xs), max(ys), p))

    def hit(self, x, y):
        for x0, y0, x1, y1, p in self.items:
            if x0 <= x <= x1 and y0 <= y <= y1 and point_in_poly(x, y, p):
                return True
        return False


def signed_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        a += x0 * y1 - x1 * y0
    return a * 0.5


def clean_pts(pts, eps=1e-6):
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > eps:
            out.append(p)
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= eps:
        out.pop()
    return out


def strip_polygon(pts, t):
    """Mitred outline of a polyline of width t (metres). Ends are extended by t/2 so that
    two walls meeting at a corner overlap and later union into a clean corner."""
    pts = clean_pts(pts)
    if len(pts) < 2:
        return []
    h = t * 0.5
    d0 = unit((pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
    d1 = unit((pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]))
    pts = [(pts[0][0] - d0[0] * h, pts[0][1] - d0[1] * h)] + pts[1:-1] + \
          [(pts[-1][0] + d1[0] * h, pts[-1][1] + d1[1] * h)]
    pts = clean_pts(pts)
    n = len(pts)
    left, right = [], []
    for i in range(n):
        if i == 0 or i == n - 1:
            a, b = (pts[0], pts[1]) if i == 0 else (pts[-2], pts[-1])
            d = unit((b[0] - a[0], b[1] - a[1]))
            nx, ny = -d[1] * h, d[0] * h
        else:
            da = unit((pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
            db = unit((pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
            n1 = (-da[1], da[0])
            m = (n1[0] - db[1], n1[1] + db[0])
            ml = math.hypot(m[0], m[1])
            if ml < 1e-6:
                m = n1
            else:
                m = (m[0] / ml, m[1] / ml)
            k = h / max(m[0] * n1[0] + m[1] * n1[1], 0.34)
            nx, ny = m[0] * k, m[1] * k
        left.append((pts[i][0] + nx, pts[i][1] + ny))
        right.append((pts[i][0] - nx, pts[i][1] - ny))
    return left + right[::-1]


# =============================================================================
# WALL / OPENING DATA FROM THE PLAN
# =============================================================================
PIECE_LEN = 10.0      # px - length of the probe pieces
PROBE = 2.5           # px - distance of the in/out probes from the wall line


def build_wall_data():
    """Returns (runs, window_candidates).
    runs: list of dict(pts=[px...], ext=bool)  - wall polylines
    window_candidates: list of dict(pts=[px...], outward=[px dir...], owner, kind)"""
    all_polys = [r["pts"] for r in ROOMS] + [p["pts"] for p in PATCHES]
    index = PolyIndex(all_polys)
    seen = set()
    runs = []
    wins = []

    def key(a, b):
        ka = (round(a[0], 1), round(a[1], 1))
        kb = (round(b[0], 1), round(b[1], 1))
        return (ka, kb) if ka <= kb else (kb, ka)

    def in_drum(mx, my):
        if not ADD_STAIR_DRUMS:
            return False
        for d in STAIR_DRUMS:
            if math.hypot(mx - d["c"][0], my - d["c"][1]) < d["r"] - 3.0:
                return True
        return False

    entries = [(r, True) for r in ROOMS] + [(p, False) for p in PATCHES]
    for ent, is_room in entries:
        pts, arcf = ent["pts"], ent["arc"]
        n = len(pts)
        pieces = []
        arc_group = None
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            if arcf[i]:
                if arc_group is None:
                    arc_group = 1000 + i
                gid = arc_group
            else:
                arc_group = None
                gid = i
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1e-6:
                continue
            k = max(1, int(math.ceil(L / PIECE_LEN)))
            nrm = unit((-(b[1] - a[1]), b[0] - a[0]))
            for s in range(k):
                pa = (a[0] + (b[0] - a[0]) * s / k, a[1] + (b[1] - a[1]) * s / k)
                pb = (a[0] + (b[0] - a[0]) * (s + 1) / k, a[1] + (b[1] - a[1]) * (s + 1) / k)
                mx, my = (pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5
                ina = index.hit(mx + nrm[0] * PROBE, my + nrm[1] * PROBE)
                inb = index.hit(mx - nrm[0] * PROBE, my - nrm[1] * PROBE)
                ext = not (ina and inb)
                outward = None
                if ext:
                    outward = (-nrm[0], -nrm[1]) if ina else nrm
                emit = (is_room or ext) and not in_drum(mx, my)
                pieces.append(dict(a=pa, b=pb, ext=ext, emit=emit, gid=gid, out=outward, k=key(pa, pb)))
        if not pieces:
            continue

        # --- window candidates (exterior stretches of window-capable rooms) ---------
        if ADD_WINDOWS and is_room and ent["kind"] in WINDOW_KINDS:
            cur = None
            for pc in pieces + [None]:
                if pc is not None and pc["ext"] and not in_drum((pc["a"][0] + pc["b"][0]) / 2, (pc["a"][1] + pc["b"][1]) / 2):
                    if cur is None or cur["gid"] != pc["gid"]:
                        if cur is not None:
                            wins.append(cur)
                        cur = dict(gid=pc["gid"], pts=[pc["a"]], outward=[], owner=ent["name"], kind=ent["kind"])
                    cur["pts"].append(pc["b"])
                    cur["outward"].append(pc["out"])
                else:
                    if cur is not None:
                        wins.append(cur)
                        cur = None

        # --- drop duplicates (shared by two rooms) -------------------------------------
        for pc in pieces:
            if pc["emit"]:
                if pc["k"] in seen:
                    pc["emit"] = False
                else:
                    seen.add(pc["k"])

        # --- merge consecutive pieces of the same class into wall runs ---------------------
        cls = [(pc["emit"], pc["ext"]) for pc in pieces]
        m = len(pieces)
        start = 0
        for i in range(m):
            if cls[i] != cls[i - 1]:
                start = i
                break
        order = pieces[start:] + pieces[:start]
        cur = None
        for pc in order + [None]:
            if pc is not None and pc["emit"]:
                if cur is None or cur["ext"] != pc["ext"]:
                    if cur is not None:
                        runs.append(cur)
                    cur = dict(pts=[pc["a"]], ext=pc["ext"], owner=ent["name"])
                cur["pts"].append(pc["b"])
            else:
                if cur is not None:
                    runs.append(cur)
                    cur = None
    return runs, wins, index


def window_positions(wins, door_points_world):
    """Distribute windows along the exterior stretches (all in world metres)."""
    out = []
    for w in wins:
        wp = [to_world(p) for p in w["pts"]]
        segs = []
        total = 0.0
        for i in range(len(wp) - 1):
            L = math.hypot(wp[i + 1][0] - wp[i][0], wp[i + 1][1] - wp[i][1])
            segs.append(L)
            total += L
        if total < WINDOW_MIN_RUN:
            continue
        n = max(1, int(total // WINDOW_SPACING))
        for k in range(n):
            s = (k + 0.5) * total / n
            acc = 0.0
            for i, L in enumerate(segs):
                if acc + L >= s:
                    f = (s - acc) / L if L > 0 else 0
                    x = wp[i][0] + (wp[i + 1][0] - wp[i][0]) * f
                    y = wp[i][1] + (wp[i + 1][1] - wp[i][1]) * f
                    t = unit((wp[i + 1][0] - wp[i][0], wp[i + 1][1] - wp[i][1]))
                    o = vec_to_world(w["outward"][i])
                    out.append(dict(p=(x, y), t=t, out=unit(o), w=min(WINDOW_WIDTH, total / n - 0.9)))
                    break
                acc += L
    # keep windows clear of doors
    res = []
    for wd in out:
        ok = True
        for dp, dw in door_points_world:
            if math.hypot(wd["p"][0] - dp[0], wd["p"][1] - dp[1]) < (dw + wd["w"]) * 0.5 + 0.4:
                ok = False
                break
        if ok and wd["w"] > 0.6:
            res.append(wd)
    return res


# =============================================================================
# MESH BUILDERS (Blender)
# =============================================================================
def add_prism(bm, pts, z0, z1, tri_faces):
    """Closed extruded polygon (outward normals). pts in world metres."""
    pts = clean_pts(pts)
    if len(pts) < 3:
        return
    if signed_area(pts) < 0:
        pts = pts[::-1]
    bot = [bm.verts.new((x, y, z0)) for x, y in pts]
    top = [bm.verts.new((x, y, z1)) for x, y in pts]
    n = len(pts)
    try:
        ft = bm.faces.new(top)
        fb = bm.faces.new(bot[::-1])
        for i in range(n):
            j = (i + 1) % n
            bm.faces.new((bot[i], bot[j], top[j], top[i]))
        if n > 3:
            tri_faces.extend([ft, fb])
    except ValueError:
        pass


def add_ring(bm, c, r_in, r_out, z0, z1, n=48):
    ang = [2 * math.pi * i / n for i in range(n)]
    oi = [(c[0] + r_in * math.cos(a), c[1] + r_in * math.sin(a)) for a in ang]
    oo = [(c[0] + r_out * math.cos(a), c[1] + r_out * math.sin(a)) for a in ang]
    bi = [bm.verts.new((x, y, z0)) for x, y in oi]
    ti = [bm.verts.new((x, y, z1)) for x, y in oi]
    bo = [bm.verts.new((x, y, z0)) for x, y in oo]
    to = [bm.verts.new((x, y, z1)) for x, y in oo]
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((bo[i], bo[j], to[j], to[i]))      # outer wall
        bm.faces.new((bi[j], bi[i], ti[i], ti[j]))      # inner wall
        bm.faces.new((ti[i], ti[j], to[j], to[i]))      # top
        bm.faces.new((bi[j], bi[i], bo[i], bo[j]))      # bottom


def add_box(bm, center, size, angle_z):
    m = Matrix.Translation(Vector(center)) @ Matrix.Rotation(angle_z, 4, "Z") @ \
        Matrix.Diagonal((size[0], size[1], size[2], 1.0))
    bmesh.ops.create_cube(bm, size=1.0, matrix=m)


def make_object(name, bm, collection, materials=()):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for mt in materials:
        me.materials.append(mt)
    ob = bpy.data.objects.new(name, me)
    collection.objects.link(ob)
    return ob


def evaluate_and_replace(ob):
    """Apply all modifiers of ob without relying on bpy.ops context."""
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    new_me = bpy.data.meshes.new_from_object(ev)
    old = ob.data
    mats = list(old.materials)
    ob.modifiers.clear()
    ob.data = new_me
    for mt in mats:
        if mt not in list(new_me.materials):
            new_me.materials.append(mt)
    bpy.data.meshes.remove(old)


# =============================================================================
# MATERIALS
# =============================================================================
def pset(node, names, value):
    for nm in names:
        if nm in node.inputs:
            try:
                node.inputs[nm].default_value = value
                return True
            except Exception:
                pass
    return False


def rgba(c):
    return (c[0], c[1], c[2], 1.0)


def new_mat(name, base, rough=0.5, metallic=0.0, spec=0.5):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    pset(bsdf, ["Base Color"], rgba(base))
    pset(bsdf, ["Roughness"], rough)
    pset(bsdf, ["Metallic"], metallic)
    pset(bsdf, ["Specular IOR Level", "Specular"], spec)
    m.diffuse_color = rgba(base)
    return m, nt, bsdf


def _tex_coord_mapping(nt, scale=(1, 1, 1)):
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mp = nt.nodes.new("ShaderNodeMapping")
    mp.inputs["Scale"].default_value = scale
    nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
    return mp


def add_bump(nt, bsdf, height_socket, strength, distance=0.01):
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    bump.inputs["Distance"].default_value = distance
    nt.links.new(height_socket, bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def shade(c, f):
    return (max(0.0, min(1.0, c[0] * f)), max(0.0, min(1.0, c[1] * f)), max(0.0, min(1.0, c[2] * f)))


def painted_plaster(name, base, rough=0.85):
    """Painted plaster: low-contrast colour mottling + fine micro-bump."""
    m, nt, bsdf = new_mat(name, base, rough, 0.0, 0.3)
    mp = _tex_coord_mapping(nt)
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 3.0
    nz.inputs["Detail"].default_value = 6.0
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].color = rgba(shade(base, 0.9))
    cr.color_ramp.elements[1].color = rgba(shade(base, 1.07))
    nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"])
    nt.links.new(cr.outputs["Color"], bsdf.inputs["Base Color"])
    fine = nt.nodes.new("ShaderNodeTexNoise")
    fine.inputs["Scale"].default_value = 400.0
    fine.inputs["Detail"].default_value = 2.0
    nt.links.new(mp.outputs["Vector"], fine.inputs["Vector"])
    add_bump(nt, bsdf, fine.outputs["Fac"], 0.12, 0.002)
    return m


def tiled_floor(name, c1, c2, mortar, tile=0.6, rough=0.28, mortar_size=0.012, offset=0.0):
    m, nt, bsdf = new_mat(name, c1, rough, 0.0, 0.5)
    mp = _tex_coord_mapping(nt)
    br = nt.nodes.new("ShaderNodeTexBrick")
    br.offset = offset
    br.squash = 1.0
    br.inputs["Color1"].default_value = rgba(c1)
    br.inputs["Color2"].default_value = rgba(c2)
    br.inputs["Mortar"].default_value = rgba(mortar)
    br.inputs["Scale"].default_value = 1.0 / tile
    br.inputs["Mortar Size"].default_value = mortar_size / tile * 1.0
    br.inputs["Brick Width"].default_value = 1.0
    br.inputs["Row Height"].default_value = 1.0
    nt.links.new(mp.outputs["Vector"], br.inputs["Vector"])
    nt.links.new(br.outputs["Color"], bsdf.inputs["Base Color"])
    add_bump(nt, bsdf, br.outputs["Fac"], 0.25, 0.004)
    return m


def wood_floor(name, dark, light, rough=0.38):
    m, nt, bsdf = new_mat(name, dark, rough, 0.0, 0.5)
    mp = _tex_coord_mapping(nt, (1.0, 28.0, 1.0))
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 3.0
    nz.inputs["Detail"].default_value = 8.0
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].color = rgba(dark)
    cr.color_ramp.elements[1].color = rgba(light)
    nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"])
    nt.links.new(cr.outputs["Color"], bsdf.inputs["Base Color"])
    add_bump(nt, bsdf, nz.outputs["Fac"], 0.05, 0.002)
    # plank seams
    mp2 = _tex_coord_mapping(nt)
    br = nt.nodes.new("ShaderNodeTexBrick")
    br.offset = 0.5
    br.inputs["Scale"].default_value = 1.0
    br.inputs["Brick Width"].default_value = 1.8
    br.inputs["Row Height"].default_value = 0.14
    br.inputs["Mortar Size"].default_value = 0.004
    nt.links.new(mp2.outputs["Vector"], br.inputs["Vector"])
    bump2 = nt.nodes.new("ShaderNodeBump")
    bump2.inputs["Strength"].default_value = 0.2
    bump2.inputs["Distance"].default_value = 0.002
    nt.links.new(br.outputs["Fac"], bump2.inputs["Height"])
    nt.links.new(bump2.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def terrazzo_floor(name, base, rough=0.3):
    m, nt, bsdf = new_mat(name, base, rough, 0.0, 0.5)
    mp = _tex_coord_mapping(nt)
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 90.0
    nz.inputs["Detail"].default_value = 4.0
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].color = rgba(shade(base, 0.82))
    cr.color_ramp.elements[1].color = rgba(shade(base, 1.18))
    nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"])
    nt.links.new(cr.outputs["Color"], bsdf.inputs["Base Color"])
    # large slab joints (1.2 m)
    br = nt.nodes.new("ShaderNodeTexBrick")
    br.inputs["Scale"].default_value = 1.0 / 1.2
    br.inputs["Brick Width"].default_value = 1.0
    br.inputs["Row Height"].default_value = 1.0
    br.inputs["Mortar Size"].default_value = 0.004
    nt.links.new(mp.outputs["Vector"], br.inputs["Vector"])
    add_bump(nt, bsdf, br.outputs["Fac"], 0.15, 0.003)
    return m


def concrete(name, base, rough=0.9, scale=6.0):
    m, nt, bsdf = new_mat(name, base, rough, 0.0, 0.3)
    mp = _tex_coord_mapping(nt)
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = scale
    nz.inputs["Detail"].default_value = 10.0
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].color = rgba(shade(base, 0.8))
    cr.color_ramp.elements[1].color = rgba(shade(base, 1.12))
    nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"])
    nt.links.new(cr.outputs["Color"], bsdf.inputs["Base Color"])
    add_bump(nt, bsdf, nz.outputs["Fac"], 0.3, 0.004)
    return m


def glass_material():
    m, nt, bsdf = new_mat("Glass", (0.88, 0.96, 0.96), 0.02, 0.0, 0.5)
    pset(bsdf, ["Transmission Weight", "Transmission"], 1.0)
    pset(bsdf, ["IOR"], 1.45)
    try:
        m.blend_method = "BLEND"
        m.show_transparent_back = False
    except Exception:
        pass
    return m


def build_materials():
    M = {}
    # ---- wall paints, colour-coded like the legend of the map but muted/realistic -----
    M["wall_lecture"] = painted_plaster("Wall_Lecture_BlueGrey", (0.36, 0.55, 0.66))
    M["wall_lab"] = painted_plaster("Wall_Lab_Green", (0.45, 0.68, 0.56))
    M["wall_cabin"] = painted_plaster("Wall_Cabin_Ochre", (0.86, 0.69, 0.38))
    M["wall_tutorial"] = painted_plaster("Wall_Tutorial_Terracotta", (0.72, 0.34, 0.20))
    M["wall_boys"] = painted_plaster("Wall_Boys_Cyan", (0.30, 0.66, 0.82))
    M["wall_girls"] = painted_plaster("Wall_Girls_Magenta", (0.74, 0.20, 0.45))
    M["wall_hall"] = painted_plaster("Wall_Hall_OffWhite", (0.88, 0.87, 0.84))
    M["wall_white"] = painted_plaster("Wall_White", (0.90, 0.89, 0.86))
    M["wall_library"] = painted_plaster("Wall_Library_Cream", (0.88, 0.84, 0.74))
    M["wall_locker"] = painted_plaster("Wall_Locker", (0.74, 0.76, 0.76))
    M["wall_shaft"] = concrete("Wall_Shaft_Concrete", (0.46, 0.46, 0.45), 0.9, 8.0)
    M["wall_corridor"] = painted_plaster("Wall_Corridor_Grey", (0.66, 0.70, 0.72))
    M["wall_exterior"] = painted_plaster("Wall_Exterior_Render", (0.83, 0.80, 0.73), 0.9)
    M["wall_cap"] = concrete("Wall_Top_Concrete", (0.58, 0.57, 0.55), 0.95, 14.0)
    # ---- floors -----------------------------------------------------------------------------
    M["floor_corridor"] = terrazzo_floor("Floor_Corridor_Terrazzo", (0.47, 0.51, 0.53))
    M["floor_lecture"] = concrete("Floor_Lecture_PolishedConcrete", (0.20, 0.30, 0.36), 0.35, 3.0)
    M["floor_lab"] = tiled_floor("Floor_Lab_Tile", (0.55, 0.70, 0.60), (0.50, 0.66, 0.56), (0.28, 0.30, 0.30), 0.6)
    M["floor_cabin"] = wood_floor("Floor_Cabin_Wood", (0.32, 0.18, 0.09), (0.50, 0.30, 0.15))
    M["floor_tutorial"] = tiled_floor("Floor_Tutorial_Tile", (0.58, 0.31, 0.20), (0.52, 0.27, 0.17), (0.25, 0.22, 0.20), 0.45)
    M["floor_boys"] = tiled_floor("Floor_Boys_Tile", (0.55, 0.78, 0.86), (0.50, 0.73, 0.82), (0.75, 0.75, 0.75), 0.3, 0.12, 0.01, 0.5)
    M["floor_girls"] = tiled_floor("Floor_Girls_Tile", (0.78, 0.45, 0.60), (0.72, 0.40, 0.55), (0.75, 0.75, 0.75), 0.3, 0.12, 0.01, 0.5)
    M["floor_hall"] = tiled_floor("Floor_Hall_PolishedTile", (0.80, 0.79, 0.76), (0.77, 0.76, 0.73), (0.45, 0.45, 0.45), 0.8, 0.12)
    M["floor_white"] = tiled_floor("Floor_White_Tile", (0.80, 0.79, 0.76), (0.77, 0.76, 0.73), (0.45, 0.45, 0.45), 0.6, 0.2)
    M["floor_library"] = wood_floor("Floor_Library_Wood", (0.45, 0.30, 0.17), (0.62, 0.43, 0.25), 0.3)
    M["floor_locker"] = concrete("Floor_Locker_Concrete", (0.40, 0.40, 0.40), 0.55, 5.0)
    M["floor_shaft"] = concrete("Floor_Shaft_Concrete", (0.38, 0.38, 0.37), 0.9, 8.0)
    # ---- joinery / glazing ----------------------------------------------------------------------
    M["door_leaf"] = wood_floor("Door_Wood", (0.30, 0.16, 0.08), (0.42, 0.24, 0.12), 0.45)
    M["door_frame"], _, _ = new_mat("Door_Frame_Hardwood", (0.20, 0.11, 0.06), 0.5)
    M["window_frame"], _, _ = new_mat("Window_Frame_Aluminium", (0.10, 0.10, 0.11), 0.35, 0.9, 0.5)
    M["sill"] = concrete("Window_Sill_Stone", (0.62, 0.61, 0.58), 0.7, 20.0)
    M["glass"] = glass_material()
    M["ground"] = concrete("Ground_Paving", (0.50, 0.50, 0.47), 0.85, 2.0)
    return M


# =============================================================================
# SCENE SET-UP
# =============================================================================
def clear_scene():
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights,
                 bpy.data.curves, bpy.data.images):
        for blk in list(coll):
            if blk.users == 0:
                coll.remove(blk)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def room_kind_lookup():
    items = []
    for r in ROOMS:
        xs = [p[0] for p in r["pts"]]
        ys = [p[1] for p in r["pts"]]
        items.append((min(xs), min(ys), max(xs), max(ys), r["pts"], r["kind"]))
    pitems = []
    for p in PATCHES:
        xs = [q[0] for q in p["pts"]]
        ys = [q[1] for q in p["pts"]]
        pitems.append((min(xs), min(ys), max(xs), max(ys), p["pts"]))

    def lookup(px, py):
        for x0, y0, x1, y1, poly, kind in items:
            if x0 <= px <= x1 and y0 <= py <= y1 and point_in_poly(px, py, poly):
                return kind
        for x0, y0, x1, y1, poly in pitems:
            if x0 <= px <= x1 and y0 <= py <= y1 and point_in_poly(px, py, poly):
                return "corridor"
        for d in STAIR_DRUMS:
            if math.hypot(px - d["c"][0], py - d["c"][1]) < d["r"]:
                return "corridor"
        return "exterior"
    return lookup


def build_walls(collection, M, runs, openings_cutter_bm_builder):
    """Create the wall object (raw prisms) and cut/union it with one Boolean pass."""
    bm = bmesh.new()
    tri = []
    z0, z1 = -FLOOR_THICKNESS, WALL_HEIGHT
    for r in runs:
        t = EXTERIOR_WALL_THICKNESS if r["ext"] else INTERIOR_WALL_THICKNESS
        poly = strip_polygon([to_world(p) for p in r["pts"]], t)
        add_prism(bm, poly, z0, z1, tri)
    if ADD_STAIR_DRUMS:
        for d in STAIR_DRUMS:
            c = to_world(d["c"])
            rm = d["r"] * PIXEL_TO_METER
            add_ring(bm, c, rm - INTERIOR_WALL_THICKNESS * 0.5, rm + INTERIOR_WALL_THICKNESS * 0.5, z0, z1, 64)
    if tri:
        bmesh.ops.triangulate(bm, faces=[f for f in tri if f.is_valid], quad_method="BEAUTY", ngon_method="EAR_CLIP")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    walls = make_object("GF_Walls", bm, collection)

    cutter = openings_cutter_bm_builder()
    if UNION_WALLS:
        print("[GF] Boolean union + openings (exact solver) - this can take a minute ...")
        mod = walls.modifiers.new("Union_and_Openings", "BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.object = cutter
        mod.solver = "EXACT"
        try:
            mod.use_self = True            # resolves the overlaps of the joined wall prisms
        except Exception:
            pass
        try:
            before = len(walls.data.polygons)
            evaluate_and_replace(walls)
            print("[GF] wall faces: %d -> %d" % (before, len(walls.data.polygons)))
        except Exception as exc:
            print("[GF] Boolean failed (%s) - keeping raw wall prisms" % exc)
            walls.modifiers.clear()
    bpy.data.objects.remove(cutter, do_unlink=True)
    return walls


def finish_wall_mesh(walls, M):
    """Dissolve coplanar leftovers, paint faces per room type, smooth, bevel."""
    kinds = ["lecture", "lab", "cabin", "tutorial", "boys", "girls", "hall", "white", "library",
             "locker", "shaft", "corridor", "exterior"]
    slot_keys = ["wall_" + k for k in kinds] + ["wall_cap"]
    me = walls.data
    me.materials.clear()
    for k in slot_keys:
        me.materials.append(M[k])
    slot = {k: i for i, k in enumerate(slot_keys)}
    lookup = room_kind_lookup()

    bm = bmesh.new()
    bm.from_mesh(me)
    try:
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.5), use_dissolve_boundaries=False,
                                 verts=bm.verts, edges=bm.edges)
    except Exception:
        pass
    bm.faces.ensure_lookup_table()
    for f in bm.faces:
        n = f.normal
        if abs(n.z) > 0.5:
            f.material_index = slot["wall_cap"]
        else:
            c = f.calc_center_median()
            px, py = to_px(c.x + n.x * 0.12, c.y + n.y * 0.12)
            f.material_index = slot["wall_" + lookup(px, py)]
        f.smooth = True
    # keep real corners crisp (>35 deg) while arcs (5 deg facets) stay smooth
    for e in bm.edges:
        if len(e.link_faces) == 2:
            if e.calc_face_angle(0.0) > math.radians(35.0):
                e.smooth = False
    bm.to_mesh(me)
    bm.free()
    me.update()
    if hasattr(me, "use_auto_smooth"):         # Blender < 4.1
        try:
            me.use_auto_smooth = True
            me.auto_smooth_angle = math.radians(35.0)
        except Exception:
            pass
    bv = walls.modifiers.new("Bevel", "BEVEL")
    bv.width = BEVEL_AMOUNT
    bv.segments = 2
    bv.limit_method = "ANGLE"
    bv.angle_limit = math.radians(35.0)
    try:
        bv.harden_normals = False
    except Exception:
        pass
    try:
        walls.modifiers.new("WeightedNormal", "WEIGHTED_NORMAL")
        walls.modifiers["WeightedNormal"].keep_sharp = True
    except Exception:
        pass


def build_floors(collection, M):
    """Corridor slab + raised room finishes grouped per material (few objects)."""
    z_slab0, z_slab1 = -FLOOR_THICKNESS, 0.0
    tri = []
    bm = bmesh.new()
    for p in PATCHES:
        add_prism(bm, [to_world(q) for q in p["pts"]], z_slab0, z_slab1, tri)
    if tri:
        bmesh.ops.triangulate(bm, faces=[f for f in tri if f.is_valid], quad_method="BEAUTY", ngon_method="EAR_CLIP")
    bm.normal_update()
    make_object("GF_Floor_Corridor", bm, collection, [M["floor_corridor"]])

    per_kind = {}
    for r in ROOMS:
        per_kind.setdefault(r["kind"], []).append(r)
    for kind, rooms in per_kind.items():
        bm = bmesh.new()
        tri = []
        for r in rooms:
            add_prism(bm, [to_world(q) for q in r["pts"]], z_slab0, ROOM_FINISH_THICKNESS, tri)
        if tri:
            bmesh.ops.triangulate(bm, faces=[f for f in tri if f.is_valid], quad_method="BEAUTY", ngon_method="EAR_CLIP")
        bm.normal_update()
        make_object("GF_Floor_" + kind.capitalize(), bm, collection, [M.get("floor_" + kind, M["floor_white"])])
    if ADD_STAIR_DRUMS:
        bm = bmesh.new()
        for d in STAIR_DRUMS:
            c = to_world(d["c"])
            rr = d["r"] * PIXEL_TO_METER
            circle = [(c[0] + rr * math.cos(2 * math.pi * i / 48), c[1] + rr * math.sin(2 * math.pi * i / 48)) for i in range(48)]
            add_prism(bm, circle, z_slab0, ROOM_FINISH_THICKNESS, [])
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 4], quad_method="BEAUTY", ngon_method="EAR_CLIP")
        make_object("GF_Floor_StairCores", bm, collection, [M["floor_shaft"]])


def prepare_openings():
    """Returns (door_list_world, window_list_world) after clearance checks."""
    door_world = []
    for d in DOORS:
        p = to_world(d["p"])
        t = unit(vec_to_world(d["t"]))
        into = unit(vec_to_world(d["into"]))
        # make 'n' the unit normal pointing into the room
        n = (-t[1], t[0])
        if n[0] * into[0] + n[1] * into[1] < 0:
            n = (-n[0], -n[1])
        door_world.append(dict(p=p, t=t, n=n, w=d["w"], ext=d["ext"]))
    if ADD_STAIR_DRUMS:
        for dr in STAIR_DRUMS:                    # one opening per stair core
            a = math.radians(dr["open_deg"])
            rm = dr["r"] * PIXEL_TO_METER
            c = to_world(dr["c"])
            n = (math.cos(a), math.sin(a))        # world: x right, y up
            p = (c[0] + n[0] * rm, c[1] + n[1] * rm)
            door_world.append(dict(p=p, t=(-n[1], n[0]), n=(-n[0], -n[1]), w=1.1, ext=False, drum=True))
    return door_world


def build_openings(collection, M, runs_wins, index):
    doors = prepare_openings()
    wins = []
    if ADD_WINDOWS:
        wins = window_positions(runs_wins, [(d["p"], d["w"]) for d in doors])

    def cutter_builder():
        bm = bmesh.new()
        for d in doors:
            ang = math.atan2(d["t"][1], d["t"][0])
            depth = 0.9 if d["ext"] else 0.55
            add_box(bm, (d["p"][0], d["p"][1], DOOR_HEIGHT * 0.5), (d["w"], depth, DOOR_HEIGHT), ang)
        for w in wins:
            ang = math.atan2(w["t"][1], w["t"][0])
            add_box(bm, (w["p"][0], w["p"][1], WINDOW_SILL + WINDOW_HEIGHT * 0.5), (w["w"], 0.9, WINDOW_HEIGHT), ang)
        return make_object("GF_Cutter", bm, collection)

    # ------------- door frames + leaves ------------------------------------------------------
    bf, bl = bmesh.new(), bmesh.new()
    for d in doors:
        ang = math.atan2(d["t"][1], d["t"][0])
        t, n, p, w = d["t"], d["n"], d["p"], d["w"]
        th = EXTERIOR_WALL_THICKNESS if d["ext"] else INTERIOR_WALL_THICKNESS
        fd = th + 0.04
        jw = 0.06
        for sgn in (-1, 1):                                   # jambs
            cx = p[0] + t[0] * sgn * (w * 0.5 - jw * 0.5)
            cy = p[1] + t[1] * sgn * (w * 0.5 - jw * 0.5)
            add_box(bf, (cx, cy, DOOR_HEIGHT * 0.5), (jw, fd, DOOR_HEIGHT), ang)
        add_box(bf, (p[0], p[1], DOOR_HEIGHT - jw * 0.5), (w, fd, jw), ang)   # head
        if d.get("drum"):
            continue
        leaves = 2 if w >= 1.4 else 1
        lw = (w - 2 * jw) / leaves
        for k in range(leaves):
            sgn = -1 if (k == 0) else 1                        # hinge side
            hinge = (p[0] + t[0] * sgn * (w * 0.5 - jw), p[1] + t[1] * sgn * (w * 0.5 - jw))
            phi = math.radians(DOOR_SWING_DEG)
            # direction from hinge towards the free end, rotated into the room
            base = (-t[0] * sgn, -t[1] * sgn)
            direction = (base[0] * math.cos(phi) + n[0] * math.sin(phi),
                         base[1] * math.cos(phi) + n[1] * math.sin(phi))
            cx = hinge[0] + direction[0] * lw * 0.5
            cy = hinge[1] + direction[1] * lw * 0.5
            add_box(bl, (cx, cy, (DOOR_HEIGHT - jw) * 0.5), (lw, 0.045, DOOR_HEIGHT - jw - 0.01),
                    math.atan2(direction[1], direction[0]))
    make_object("GF_Door_Frames", bf, collection, [M["door_frame"]])
    make_object("GF_Door_Leaves", bl, collection, [M["door_leaf"]])

    # ------------- window frames, glass, sills ---------------------------------------------------
    if wins:
        bfr, bgl, bsl = bmesh.new(), bmesh.new(), bmesh.new()
        fb = 0.05
        for w in wins:
            ang = math.atan2(w["t"][1], w["t"][0])
            t, o, p, ww = w["t"], w["out"], w["p"], w["w"]
            zc = WINDOW_SILL + WINDOW_HEIGHT * 0.5
            dpt = 0.09
            for sgn in (-1, 1):
                add_box(bfr, (p[0] + t[0] * sgn * (ww * 0.5 - fb * 0.5), p[1] + t[1] * sgn * (ww * 0.5 - fb * 0.5), zc),
                        (fb, dpt, WINDOW_HEIGHT), ang)
            for zz in (WINDOW_SILL + fb * 0.5, WINDOW_SILL + WINDOW_HEIGHT - fb * 0.5):
                add_box(bfr, (p[0], p[1], zz), (ww, dpt, fb), ang)
            add_box(bfr, (p[0], p[1], zc), (fb * 0.6, dpt, WINDOW_HEIGHT - 2 * fb), ang)    # mullion
            add_box(bgl, (p[0], p[1], zc), (ww - 2 * fb, 0.012, WINDOW_HEIGHT - 2 * fb), ang)
            off = EXTERIOR_WALL_THICKNESS * 0.5 + 0.02
            add_box(bsl, (p[0] + o[0] * off, p[1] + o[1] * off, WINDOW_SILL - 0.02),
                    (ww + 0.16, 0.2, 0.04), ang)
        make_object("GF_Window_Frames", bfr, collection, [M["window_frame"]])
        make_object("GF_Window_Glass", bgl, collection, [M["glass"]])
        make_object("GF_Window_Sills", bsl, collection, [M["sill"]])
    return cutter_builder, len(doors), len(wins)


def build_ground(collection, M):
    me = bpy.data.meshes.new("GF_Ground")
    s = 250.0
    z = -FLOOR_THICKNESS + 0.02
    me.from_pydata([(-s, -s, z), (s, -s, z), (s, s, z), (-s, s, z)], [], [(0, 1, 2, 3)])
    me.materials.append(M["ground"])
    ob = bpy.data.objects.new("GF_Ground", me)
    collection.objects.link(ob)


def setup_world_and_lights(scene, collection):
    world = bpy.data.worlds.new("GF_World") if not scene.world else scene.world
    scene.world = world
    try:
        world.use_nodes = True
    except Exception:
        pass
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    try:
        sky = nt.nodes.new("ShaderNodeTexSky")
        sky.sky_type = "NISHITA"
        sky.sun_elevation = math.radians(48)
        sky.sun_rotation = math.radians(40)
        try:
            sky.sun_disc = False                      # sun light is a separate soft-shadow lamp
        except Exception:
            pass
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
    except Exception:
        bg.inputs["Color"].default_value = (0.55, 0.68, 0.85, 1.0)
    bg.inputs["Strength"].default_value = 0.9
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])

    sun_data = bpy.data.lights.new("GF_Sun", "SUN")
    sun_data.energy = 3.5
    sun_data.angle = math.radians(1.2)                # soft shadows
    sun_data.color = (1.0, 0.96, 0.9)
    sun = bpy.data.objects.new("GF_Sun", sun_data)
    sun.rotation_euler = (math.radians(42), math.radians(0), math.radians(-55))
    collection.objects.link(sun)


def setup_camera(scene, collection):
    cam_data = bpy.data.cameras.new("GF_Camera")
    cam_data.lens = CAMERA_LENS
    cam_data.clip_start = 0.5
    cam_data.clip_end = 2000.0
    cam = bpy.data.objects.new("GF_Camera", cam_data)
    el = math.radians(CAMERA_ELEVATION_DEG)
    az = math.radians(CAMERA_AZIMUTH_DEG)
    target = Vector((0.0, 0.0, 0.8))
    loc = target + Vector((CAMERA_DISTANCE * math.cos(el) * math.sin(az),
                           -CAMERA_DISTANCE * math.cos(el) * math.cos(az),
                           CAMERA_DISTANCE * math.sin(el)))
    cam.location = loc
    cam.rotation_euler = (target - loc).to_track_quat("-Z", "Y").to_euler()
    collection.objects.link(cam)
    scene.camera = cam


def setup_render(scene):
    r = scene.render
    r.resolution_x, r.resolution_y = RESOLUTION
    r.resolution_percentage = 100
    if RENDER_ENGINE.upper() == "CYCLES":
        r.engine = "CYCLES"
        c = scene.cycles
        c.samples = RENDER_SAMPLES
        c.preview_samples = 32
        c.use_denoising = True
        try:
            c.denoiser = "OPENIMAGEDENOISE"
        except Exception:
            pass
        c.max_bounces = 8
        c.diffuse_bounces = 4
        c.glossy_bounces = 4
        c.transmission_bounces = 8
        try:
            c.use_adaptive_sampling = True
        except Exception:
            pass
    else:
        for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                r.engine = eng
                break
            except TypeError:
                continue
        ee = scene.eevee
        for attr, val in (("taa_render_samples", RENDER_SAMPLES), ("use_gtao", True), ("gtao_distance", 1.5),
                          ("use_soft_shadows", True), ("use_ssr", True), ("use_raytracing", True)):
            try:
                setattr(ee, attr, val)
            except Exception:
                pass
    for vt in ("AgX", "Filmic"):
        try:
            scene.view_settings.view_transform = vt
            break
        except TypeError:
            continue
    try:
        scene.view_settings.look = "None"
    except Exception:
        pass
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0


def show_camera_in_viewport():
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                sp = area.spaces[0]
                sp.shading.type = "MATERIAL"
                sp.region_3d.view_perspective = "CAMERA"
                sp.clip_end = 2000.0
    except Exception:
        pass


# =============================================================================
# MAIN
# =============================================================================
def main():
    scene = bpy.context.scene
    clear_scene()
    coll = bpy.data.collections.new("GroundFloor")
    scene.collection.children.link(coll)

    M = build_materials()

    runs, wins, index = build_wall_data()
    n_ext = sum(1 for r in runs if r["ext"])
    print("[GF] wall runs: %d (%d exterior, %d interior)" % (len(runs), n_ext, len(runs) - n_ext))

    build_floors(coll, M)
    cutter_builder, n_doors, n_wins = build_openings(coll, M, wins, index)
    print("[GF] doors/openings: %d   windows: %d" % (n_doors, n_wins))
    walls = build_walls(coll, M, runs, cutter_builder)
    finish_wall_mesh(walls, M)
    if ADD_GROUND:
        build_ground(coll, M)

    setup_world_and_lights(scene, coll)
    setup_camera(scene, coll)
    setup_render(scene)
    bpy.context.view_layer.update()
    show_camera_in_viewport()

    if SAVE_PATH:
        try:
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(SAVE_PATH))
            print("[GF] saved", SAVE_PATH)
        except Exception as exc:
            print("[GF] could not save .blend:", exc)
    if RENDER_ON_RUN and RENDER_PATH:
        scene.render.filepath = os.path.abspath(RENDER_PATH)
        bpy.ops.render.render(write_still=True)
    print("[GF] done - ground floor built (no roof, no upper storey).")


if __name__ == "__main__":
    main()
