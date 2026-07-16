#!/usr/bin/env python3
"""
thermal_strategy.py  -- per-pilot thermalling parameters + the day's result, in a
table you can sort and play with.

For every IGC under FOLDER (one subfolder per day) it measures per-pilot flying
"levers", compares each pilot to the field of the same day, and joins the
official day result you provide. A per-day wind field is reconstructed from the
pilots' own circling (paraglider-as-anemometer); see METHODS.md.

RESULTS (official distance): put a file 'results.csv' in FOLDER (or pass --results)
with a key column (file / name / pilot) and a km column (km / xc_km / fai_km /
distance) and optionally a speed column (speed / speed_kmh). Keys match either the
IGC filename (without extension) or the pilot name. If absent, a rough route proxy
is used and clearly flagged.

USAGE
    python thermal_strategy.py FOLDER [--group subfolder|date] [--results results.csv]
                                      [--min-thermals 5] [--min-per-day 3]
                                      [--target result_km] [--target-sign 1]

OUTPUT (next to the script)
    schnell-fliegen.html    the shared panel: per-pilot cards, teaching radar,
                            fold-out methods + wind (one self-contained file)
    schnell-fliegen.csv     master table, one row per pilot (open in Excel)
    schnell-fliegen.pdf     static result-sorted table, per-pilot signatures,
                            parameter-vs-result correlations, and a glossary
    METHODS.md              the full methods, identical to the page fold-out

Requires numpy, pandas, scipy, matplotlib (Python 3.9+).
"""
import sys, os, glob, re, argparse, datetime, textwrap, html as _html
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.signal import savgol_filter
from scipy import stats
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

# ----- detection / metric parameters
MIN_TURNS, MIN_GAIN_M = 3.0, 10.0
SHORT_MIN_TURNS, SHORT_MIN_GAIN_M = 1.0, 0.0
TURN_GATE_DEG_S, PRESENCE_WIN_S, MERGE_GAP_S = 6.0, 11, 15
BEAR_WIN_S, POST_WIN_S, PROBE_TIME_S, PROBE_DIST_M = 30, 60, 120, 1500
# ----- triangle scoring: scored distance = perimeter - closing gap (XContest-style)
TRI_CLOSE_FRAC = 0.20        # a triangle counts only if the closing gap <= this fraction of the perimeter
# ----- hard plausibility limits: flights breaching these are altitude-data glitches and are dropped
MAX_PLAUS_CLIMB    = 12.0    # m/s; no paraglider sustains a climb/peak/core gain above this
MAX_PLAUS_MEAN_CLIMB = 6.0   # m/s; mean climb across a thermal above this is not real
MIN_PLAUS_ABANDON  = -1.0    # m/s; abandon climb more negative than this is a glitch, not a choice
CLIMB_PCT_THR      = 0.1     # m/s; vario above this counts as actively climbing (gaining height)
STRONG_CLIMB_MS    = 2.0     # m/s; a thermal whose mean climb is >= this counts as "strong" (else weak)
CORE_MIN_TURNS, CORE_MIN_GAIN_M = 5.0, 20.0  # a thermal must be at least this big to test core-finding
CORE_MIN_SAMPLES = 2  # need at least this many weak (or strong) big thermals before reporting a core value
RICHTUNG_WIN       = 180     # s; window for net velocity-made-good toward the reconstructed goal

# ============================== IGC LOADING =====================================
def _coord(v, h, is_lat):
    deg = int(v[:2]) if is_lat else int(v[:3])
    minutes = float(v[2:]) / 1000.0 if is_lat else float(v[3:]) / 1000.0
    d = deg + minutes / 60.0
    return -d if h in ("S", "W") else d

def _clean_site(val):
    # take a short name: first chunk before / , : ( and trim
    s = re.split(r"[/,:(]", val.strip())[0].strip()
    return s if s else None

def read_header(path):
    meta = {"date": "", "name": "", "site": None}
    raw = open(path, "rb").read(65536)
    try: text = raw.decode("utf-8")
    except UnicodeDecodeError: text = raw.decode("latin-1", errors="ignore")
    for line in text.splitlines():
        if line.startswith("B"): break
        u = line.strip()
        if u.startswith("HFDTE") and not meta["date"]:
            m = re.search(r"(\d{6})", u)
            if m:
                dd, mm, yy = int(m.group(1)[:2]), int(m.group(1)[2:4]), int(m.group(1)[4:6])
                try: meta["date"] = datetime.date(2000 + yy, mm, dd).isoformat()
                except Exception: meta["date"] = m.group(1)
        elif "PILOT" in u and ":" in u and not meta["name"]:
            meta["name"] = u.split(":", 1)[1].strip()
        elif u[:1] == "H" and u[2:5] == "SIT" and ":" in u and not meta["site"]:
            meta["site"] = _clean_site(u.split(":", 1)[1])
    return meta

def parse_igc(path):
    rows = []
    with open(path, "r", encoding="latin-1", errors="ignore") as f:
        for line in f:
            if not line.startswith("B") or len(line.strip()) < 35: continue
            try:
                hh, mm, ss = int(line[1:3]), int(line[3:5]), int(line[5:7])
                rows.append((hh*3600+mm*60+ss, _coord(line[7:14], line[14], True),
                             _coord(line[15:23], line[23], False), int(line[25:30]), int(line[30:35])))
            except Exception: continue
    if not rows: raise ValueError("no B-records")
    df = pd.DataFrame(rows, columns=["ts", "lat", "lon", "alt_baro", "alt_gps"])
    t = df["ts"].to_numpy().astype(float); off = 0
    for i in range(1, len(t)):
        if t[i] + off < t[i-1]: off += 86400
        t[i] += off
    df["t"] = t - t[0]
    # Altitude source: GPS is preferred for ALL flights. It is the primary, always-
    # present signal, and on modern loggers its vario quality matches barometric while
    # keeping every pilot on the SAME source (fairer for a within-day relative compare).
    # Barometric pressure is only a fallback for the rare flight whose GPS altitude is
    # itself broken (zeros, flat, or physically impossible jumps).
    baro = df["alt_baro"].to_numpy().astype(float)
    gps = df["alt_gps"].to_numpy().astype(float)
    def _bad(a):
        return (len(a) == 0 or np.nanstd(a) < 1 or np.mean(a <= 0) > 0.01
                or np.mean(np.abs(np.diff(a)) > 40) > 0.02)   # >40 m/s steps = dropouts
    gps_bad, baro_bad = _bad(gps), _bad(baro)
    if not gps_bad:
        df["alt"] = gps
    elif not baro_bad:
        df["alt"] = baro
    else:
        df["alt"] = gps    # both look questionable; GPS is still the primary signal
    return df[["t", "lat", "lon", "alt"]]

def haversine_m(a1, o1, a2, o2):
    R = 6371000.0; p1, p2 = np.radians(a1), np.radians(a2)
    a = np.sin(np.radians(a2-a1)/2)**2 + np.cos(p1)*np.cos(p2)*np.sin(np.radians(o2-o1)/2)**2
    return 2*R*np.arcsin(np.sqrt(a))

# ---- XContest-style distances (approximate): free distance, flat & FAI triangle
def _rdp_idx(x, y, eps):
    keep = np.zeros(len(x), bool); keep[0] = keep[-1] = True
    stack = [(0, len(x)-1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0+1: continue
        x0, y0, x1, y1 = x[i0], y[i0], x[i1], y[i1]
        dx, dy = x1-x0, y1-y0; L = np.hypot(dx, dy)
        seg = slice(i0+1, i1)
        if L == 0: d = np.hypot(x[seg]-x0, y[seg]-y0)
        else: d = np.abs(dy*(x[seg]-x0) - dx*(y[seg]-y0))/L
        if len(d) == 0: continue
        m = int(np.argmax(d))
        if d[m] > eps:
            mi = i0+1+m; keep[mi] = True; stack += [(i0, mi), (mi, i1)]
    return np.where(keep)[0]

def simplify_indices(x, y, max_pts=240):
    eps = 150.0
    idx = _rdp_idx(x, y, eps)
    while len(idx) > max_pts:
        eps *= 1.6; idx = _rdp_idx(x, y, eps)
    return idx

def _pairwise_km(lat, lon):
    la = np.radians(lat); lo = np.radians(lon)
    dlat = la[:, None]-la[None, :]; dlon = lo[:, None]-lo[None, :]
    a = np.sin(dlat/2)**2 + np.cos(la[:, None])*np.cos(la[None, :])*np.sin(dlon/2)**2
    return 2*6371.0*np.arcsin(np.sqrt(np.clip(a, 0, 1)))

def free_distance_km(D):
    """Open distance with up to 3 turnpoints (5 points, 4 legs) via DP."""
    N = D.shape[0]
    if N < 2: return 0.0
    dp = np.zeros((5, N))
    for l in range(1, 5):
        prev = dp[l-1]
        for j in range(1, N):
            dp[l][j] = np.max(prev[:j] + D[:j, j])
    return float(dp[1:].max())  # best of 1..4 legs

def triangles_km(D):
    """Scored flat and FAI triangle distance, XContest-style:
    scored = triangle perimeter - closing distance, where the closing distance is the
    gap between a start fix (at/before the 1st turnpoint) and a finish fix (at/after the
    3rd). A triangle counts only if closing <= TRI_CLOSE_FRAC of the perimeter; a
    'badly closed' triangle keeps the perimeter minus its (larger) closing gap.
    FAI also needs every leg >= 28% of the perimeter."""
    N = D.shape[0]
    if N < 3: return 0.0, 0.0
    # M[a][c] = min over start s<=a, finish f>=c of D[s,f]  (cheapest way to close a..c)
    R = np.empty((N, N)); R[:, N-1] = D[:, N-1]
    for c in range(N-2, -1, -1): R[:, c] = np.minimum(D[:, c], R[:, c+1])
    M = np.empty((N, N)); M[0, :] = R[0, :]
    for a in range(1, N): M[a, :] = np.minimum(M[a-1, :], R[a, :])
    best_flat = best_fai = 0.0
    frac = max(TRI_CLOSE_FRAC, 1e-9)
    for a in range(N-2):
        ab_row = D[a]
        for b in range(a+1, N-1):
            ab = ab_row[b]
            cs = np.arange(b+1, N)
            bc = D[b, cs]; ca = D[a, cs]
            P = ab + bc + ca
            clo = M[a, cs]
            closed = clo <= frac*P
            if not closed.any(): continue
            scored = P - clo                       # badly-closed triangles lose the gap
            sf = np.where(closed, scored, -1.0)
            mflat = float(sf.max())
            if mflat > best_flat: best_flat = mflat
            minleg = np.minimum(np.minimum(ab, bc), ca)
            fmask = closed & (minleg >= 0.28*P)    # FAI shape
            if fmask.any():
                mfai = float(np.where(fmask, scored, -1.0).max())
                if mfai > best_fai: best_fai = mfai
    return max(best_flat, 0.0), max(best_fai, 0.0)

def _trim_landing(out, flight_kmh=10.0, ground_kmh=4.0, min_ground_s=60):
    """Drop trailing on-ground fixes. Many live-tracking devices keep logging after the
    pilot has landed, which would otherwise stretch the flight time and depress the speed.
    Real flying (even tight thermalling) keeps the fix-to-fix speed well above flight_kmh,
    while a parked glider drifts only a few km/h via GPS jitter. So we cut at the last
    genuine-flight fix, but only when the tail after it is truly on the ground: it lasts at
    least min_ground_s seconds and its median speed stays below ground_kmh."""
    n = len(out)
    if n < 2 * min_ground_s:
        return out
    gs = out["step_m"].to_numpy() * 3.6  # km/h, since fixes are 1 s apart (metres per second)
    gsm = pd.Series(gs).rolling(15, center=True, min_periods=1).median().to_numpy()
    flying = np.where(gsm >= flight_kmh)[0]
    if len(flying) == 0:
        return out
    last_flight = int(flying.max())
    tail = gs[last_flight + 1:]
    if len(tail) >= min_ground_s and float(np.median(tail)) < ground_kmh:
        out = out.iloc[:last_flight + 1].reset_index(drop=True)
        out["t"] = np.arange(len(out), dtype=float)
    return out

def features_frame(df):
    df = df.sort_values("t").drop_duplicates("t").reset_index(drop=True)
    df = df.set_index(pd.to_timedelta(df["t"], unit="s"))
    out = df[["lat","lon","alt"]].resample("1s").mean().interpolate(limit=10).dropna().reset_index(drop=True)
    out["t"] = np.arange(len(out), dtype=float)
    step = np.zeros(len(out))
    step[1:] = haversine_m(out["lat"].iloc[:-1].to_numpy(), out["lon"].iloc[:-1].to_numpy(),
                           out["lat"].iloc[1:].to_numpy(), out["lon"].iloc[1:].to_numpy())
    out["step_m"] = step; out["speed_ms"] = step
    out = _trim_landing(out)  # cut trailing on-ground fixes (tracker still logging after landing)
    n = len(out); win = min(15, n if n % 2 == 1 else n-1)
    out["alt_smooth"] = savgol_filter(out["alt"], max(win, 5), 2) if n > 5 else out["alt"]
    out["vario"] = pd.Series(np.gradient(out["alt_smooth"].to_numpy())).rolling(5, center=True, min_periods=1).mean().to_numpy()
    x = (out["lon"].to_numpy()-out["lon"].mean())*np.cos(np.radians(out["lat"].mean()))*111320.0
    y = (out["lat"].to_numpy()-out["lat"].mean())*110540.0
    out["x_m"], out["y_m"] = x, y
    vx, vy = np.gradient(x), np.gradient(y)
    hdg = np.degrees(np.arctan2(vx, vy)); dh = np.diff(hdg); dh = (dh+180) % 360 - 180
    out["turn_deg_s"] = pd.Series(np.concatenate([[0.0], dh])).rolling(5, center=True, min_periods=1).mean().to_numpy()
    return out

# ============================== DETECTION =======================================
def detect_bouts(df):
    turn = df["turn_deg_s"].to_numpy()
    pres = np.convolve((np.abs(turn) > TURN_GATE_DEG_S).astype(float),
                       np.ones(PRESENCE_WIN_S)/PRESENCE_WIN_S, mode="same") > 0.5
    bouts, inb, s = [], False, 0
    for i in range(len(pres)):
        if pres[i] and not inb: s, inb = i, True
        elif not pres[i] and inb: bouts.append((s, i-1)); inb = False
    if inb: bouts.append((s, len(pres)-1))
    merged = []
    for a, b in bouts:
        if merged and a - merged[-1][1] - 1 <= MERGE_GAP_S: merged[-1] = (merged[-1][0], b)
        else: merged.append((a, b))
    alt = df["alt_smooth"].to_numpy(); big, short = [], []
    for a, b in merged:
        turns = abs(float(np.sum(turn[a:b+1])))/360.0; gain = float(alt[b]-alt[a])
        if turns >= MIN_TURNS and gain >= MIN_GAIN_M: big.append({"a": a, "b": b, "turns": turns})
        elif SHORT_MIN_TURNS <= turns < MIN_TURNS and gain >= SHORT_MIN_GAIN_M: short.append({"a": a, "b": b, "turns": turns})
    return big, short

def circle_radius_m(df, a, b):
    seg = df.iloc[a:b+1]; omega = np.abs(np.radians(seg["turn_deg_s"].to_numpy())); v = seg["speed_ms"].to_numpy()
    m = np.degrees(omega) > TURN_GATE_DEG_S
    if m.sum() < 3: return np.nan
    r = v[m]/omega[m]; r = r[(r > 2) & (r < 200)]
    return float(np.median(r)) if len(r) else np.nan

def circles(df, a, b):
    turn = np.abs(df["turn_deg_s"].to_numpy()[a:b+1]); cum = np.cumsum(turn); out = []
    for k in range(max(int(cum[-1]//360), 1)):
        idx = np.where((cum >= k*360) & (cum < (k+1)*360))[0]
        if len(idx) < 3: continue
        out.append(circle_radius_m(df, a+int(idx[0]), a+int(idx[-1])))
    return [r for r in out if np.isfinite(r)]

def ang_diff(a, b):
    d = abs(a-b) % 360.0; return min(d, 360.0-d)

def bearing_vec(df, i0, i1):
    i0 = max(i0, 0); i1 = min(i1, len(df)-1)
    if i1-i0 < 5: return None
    dx = float(df["x_m"].iloc[i1]-df["x_m"].iloc[i0]); dy = float(df["y_m"].iloc[i1]-df["y_m"].iloc[i0])
    return None if (dx == 0 and dy == 0) else np.degrees(np.arctan2(dx, dy))

# ============================== PER-FLIGHT ======================================
def _coring_gain(v):
    """How much the climb improves from entry to the working core, before the natural
    top-fade. Split the thermal into sixths (by time, ~1Hz samples): entry = 1st sixth,
    core = strongest of the 2nd-4th sixths (the working band). coring_gain = core - entry.
    Immune to top-fade (ignores the last two sixths) and to which thermal was chosen."""
    v = np.asarray(v, float); n = len(v)
    if n < 18: return np.nan
    e = (np.linspace(0, 1, 7)*n).astype(int)
    b = [np.nanmean(v[e[k]:e[k+1]]) for k in range(6)]
    if any(not np.isfinite(x) for x in b[:4]): return np.nan
    return float(max(b[1], b[2], b[3]) - b[0])

def _lift_following(seg):
    """Does the pilot move the circle toward the stronger-lift side? For each circle
    (one 360deg), find the vario-weighted 'strong side' relative to that circle's centre,
    then check whether the NEXT circle's centre shifted that way. Wind drift removed first.
    Returns mean cosine alignment over circle pairs: >0 = tracks toward stronger lift."""
    x = seg["x_m"].to_numpy(); y = seg["y_m"].to_numpy()
    v = seg["vario"].to_numpy(); tr = seg["turn_deg_s"].to_numpy(); n = len(x)
    if n < 40: return np.nan
    a = np.abs(tr); med = np.nanmedian(a[a > 3]) if np.any(a > 3) else 18.0
    per = int(np.clip(360.0/max(med, 4.0), 14, 45))
    cx = pd.Series(x).rolling(per, center=True, min_periods=per//2).mean().to_numpy()
    cy = pd.Series(y).rolling(per, center=True, min_periods=per//2).mean().to_numpy()
    t = np.arange(n, dtype=float); ok = np.isfinite(cx)
    if ok.sum() < per: return np.nan
    dx = np.polyval(np.polyfit(t[ok], cx[ok], 1), t); dy = np.polyval(np.polyfit(t[ok], cy[ok], 1), t)
    ax = x-dx; ay = y-dy                                    # wind-drift removed
    cum = np.nancumsum(a); edges = [0]; nxt = 360.0
    for i in range(n):
        if cum[i] >= nxt: edges.append(i); nxt += 360.0
    edges.append(n-1)
    centers, strong = [], []
    for k in range(len(edges)-1):
        i0, i1 = edges[k], edges[k+1]
        if i1-i0 < 6: continue
        cxk = ax[i0:i1].mean(); cyk = ay[i0:i1].mean()
        w = np.clip(v[i0:i1]-np.nanmean(v[i0:i1]), 0, None)
        if w.sum() <= 0: continue
        sx = np.average(ax[i0:i1]-cxk, weights=w); sy = np.average(ay[i0:i1]-cyk, weights=w)
        nrm = np.hypot(sx, sy)
        if nrm < 1e-6: continue
        centers.append((cxk, cyk)); strong.append((sx/nrm, sy/nrm))
    if len(centers) < 2: return np.nan
    al = []
    for k in range(len(centers)-1):
        sh = np.array(centers[k+1])-np.array(centers[k]); sn = np.hypot(*sh)
        if sn < 3: continue
        al.append(float(np.dot(sh/sn, strong[k])))
    return float(np.nanmean(al)) if al else np.nan

def parse_cutoff_sod(name):
    """A day-folder name may pin a race-start time, e.g. ``2026_06_16_UTC1000`` or
    ``UTC:10:00`` / ``utc 10:00``. Read as a UTC wall-clock time (the same clock as the IGC
    B-records) and returned as seconds-of-day. Returns None when no token is present."""
    m = re.search(r"utc[\s:_\-]*([0-2]?\d)[:_\.]?([0-5]\d)\b", str(name), re.IGNORECASE)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return hh*3600 + mm*60 if hh <= 23 else None

def _null_sink_speed(df):
    """Null-Sink-Speed (km/h): fit height-gain (m/min) vs net ground speed (km/h) over 90 s windows;
    the x-intercept (where the line crosses zero gain) is the break-even ground speed. A statistical
    summary of how well the pilot makes height, not a really flyable speed."""
    x = df["x_m"].to_numpy(); y = df["y_m"].to_numpy(); alt = df["alt_smooth"].to_numpy(); N = len(x)
    W = 90
    if N < 2*W:
        return np.nan
    gg = []; hh = []
    for i in range(0, N-W, W):
        net = float(np.hypot(x[i+W]-x[i], y[i+W]-y[i]))/W*3.6
        hg = (alt[i+W]-alt[i])/W*60.0
        if net < 90 and abs(hg) < 400:
            gg.append(net); hh.append(hg)
    g = np.array(gg); h = np.array(hh)
    if len(g) < 20:
        return np.nan
    b, a = np.polyfit(g, h, 1)
    return float(-a/b) if b < -1e-3 else np.nan

def flight_record(path, meta, cut_sod=None, strong_climb=STRONG_CLIMB_MS,
                  core_min_turns=CORE_MIN_TURNS, core_min_gain=CORE_MIN_GAIN_M):
    df = features_frame(parse_igc(Path(path)))
    # ---- analysis-window cut: drop everything before cut_sod (UTC seconds-of-day) ----
    # cut_sod is either a race-start token from the day-folder name, or (non-race days) the
    # group's latest launch plus a fixed offset. Measuring distance, speed and every lever only
    # inside this common window stops early starters -- who spend the first part slowly making
    # ground -- from being penalised. Each pilot keeps their own end, so window lengths differ.
    race_start_alt = np.nan
    if cut_sod is not None:
        sod = float(_start_sod(Path(path))) + df["t"].to_numpy()
        keep = sod >= cut_sod
        if int(keep.sum()) < 60:
            raise ValueError(f"almost nothing after window cut ({int(keep.sum())} s of track)")
        race_start_alt = float(df["alt_smooth"].to_numpy()[int(np.argmax(keep))])
        df = df.loc[keep].reset_index(drop=True)
    fh = float(df["t"].iloc[-1] - df["t"].iloc[0])/3600.0   # air-time of the analysed window
    big_all, short_all = detect_bouts(df)
    big = big_all; short = short_all          # thermal levers use every detected thermal
    eff_h = max(fh, 0.1)

    rows = []
    for i, th in enumerate(big):
        a, b = th["a"], th["b"]; seg = df.iloc[a:b+1]
        v = seg["vario"].to_numpy(); tt = seg["t"].to_numpy()-float(seg["t"].iloc[0]); dur = float(tt[-1])
        gain = float(seg["alt_smooth"].iloc[-1]-seg["alt_smooth"].iloc[0]); mc = gain/dur if dur > 0 else np.nan
        peak = float(np.nanmax(v)); half = len(v)//2; radii = circles(df, a, b)
        directness = np.nan
        if i > 0:
            pa, pb = big[i-1]["a"], big[i-1]["b"]
            cov = float(df["step_m"].iloc[pb+1:a+1].sum())/1000.0
            strt = float(haversine_m(df["lat"].iloc[pb], df["lon"].iloc[pb], df["lat"].iloc[a], df["lon"].iloc[a]))/1000.0
            directness = strt/cov if cov > 0 else np.nan
        ea_t = float(df["t"].iloc[a]); elat = float(df["lat"].iloc[a]); elon = float(df["lon"].iloc[a]); probes = 0
        for s in short:
            if 0 <= ea_t-float(df["t"].iloc[s["b"]]) <= PROBE_TIME_S:
                slat = float(df["lat"].iloc[s["a"]:s["b"]+1].mean()); slon = float(df["lon"].iloc[s["a"]:s["b"]+1].mean())
                if haversine_m(elat, elon, slat, slon) <= PROBE_DIST_M: probes += 1
        rows.append(dict(mean_climb=mc, peak_climb=peak,
            frac_strong=float(np.mean(v > 0.75*peak)) if peak > 0 else np.nan,
            climb_cv=float(np.nanstd(v)/np.nanmean(v)) if np.nanmean(v) > 0 else np.nan,
            radius_c1=radii[0] if radii else np.nan, radius_min=min(radii) if radii else np.nan,
            radius_mean=float(np.mean(radii)) if radii else np.nan,
            turns_to_min=(int(np.argmin(radii))+1) if radii else np.nan,
            time_to_peak_s=float(tt[int(np.nanargmax(v))]) if len(v) else np.nan,
            coring_gain=_coring_gain(v), n_turns=float(th["turns"]), gain_m=gain,
            lift_follow=_lift_following(seg),
            directness_in=directness,
            bearing_change_deg=(lambda bi, bo: ang_diff(bi, bo) if (bi is not None and bo is not None) else np.nan)(
                bearing_vec(df, a-BEAR_WIN_S-5, a-5), bearing_vec(df, b+5, b+BEAR_WIN_S+5)),
            post_exit_turn_std=float(np.nanstd(df["turn_deg_s"].to_numpy()[b+1:min(b+1+POST_WIN_S, len(df))])),
            probes_before=probes, dur_min=dur/60.0, lat=float(seg["lat"].mean()), lon=float(seg["lon"].mean())))
    P = pd.DataFrame(rows)
    sab, scl, sdur = [], [], []
    for s in short:
        a, b = s["a"], s["b"]; d2 = float(df["t"].iloc[b]-df["t"].iloc[a])
        g2 = float(df["alt_smooth"].iloc[b]-df["alt_smooth"].iloc[a]); last = df["vario"].to_numpy()[max(a, b-9):b+1]
        sab.append(float(np.nanmean(last)) if len(last) else np.nan); scl.append(g2/d2 if d2 > 0 else np.nan); sdur.append(d2/60.0)

    # route proxy (whole flight -- it's the outcome)
    pts = [(float(df["lat"].iloc[0]), float(df["lon"].iloc[0]))]
    pts += [(float(df["lat"].iloc[th["a"]]), float(df["lon"].iloc[th["a"]])) for th in big_all]
    pts.append((float(df["lat"].iloc[-1]), float(df["lon"].iloc[-1])))
    route_km = float(sum(haversine_m(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)))/1000.0

    # XContest-style distances (approximate) on a simplified track
    sidx = simplify_indices(df["x_m"].to_numpy(), df["y_m"].to_numpy(), 240)
    Dk = _pairwise_km(df["lat"].to_numpy()[sidx], df["lon"].to_numpy()[sidx])
    free_km = free_distance_km(Dk)
    flat_km, fai_km = triangles_km(Dk)

    # control parameters (shown in the statistics only, never in the profile/radar)
    _turn = df["turn_deg_s"].to_numpy(); _vario = df["vario"].to_numpy()
    _circ = np.abs(_turn) > TURN_GATE_DEG_S
    _total_turns = float(np.abs(_turn[_circ]).sum())/360.0
    _gs = np.hypot(np.gradient(df["x_m"].to_numpy()), np.gradient(df["y_m"].to_numpy()))   # m/s
    _glide = (~_circ) & (_vario < -0.3) & (_gs > 3)
    _drop = -_vario[_glide]
    _glide_ratio = float(_gs[_glide].sum()/_drop.sum()) if (_glide.sum() > 5 and _drop.sum() > 0) else np.nan
    _null = _null_sink_speed(df)

    rec = dict(file=Path(path).stem, day=meta["date"] or "?", name=meta.get("name", ""),
               site_hdr=meta.get("site"), launch_lat=float(df["lat"].iloc[0]), launch_lon=float(df["lon"].iloc[0]),
               flight_h=round(fh, 2), n_big=len(big), n_short=len(short),
               big_per_h=len(big)/eff_h if eff_h > 0 else np.nan,
               short_per_h=len(short)/eff_h if eff_h > 0 else np.nan,
               short_over_big=len(short)/len(big) if big else np.nan,
               circling_pct=float((np.abs(df["turn_deg_s"]) > TURN_GATE_DEG_S).mean())*100,
               total_turns=round(_total_turns, 1),
               turns_per_h=round(_total_turns/fh, 1) if fh > 0 else np.nan,
               glide_ratio=round(_glide_ratio, 1) if np.isfinite(_glide_ratio) else np.nan,
               null_sink_speed=round(_null, 1) if np.isfinite(_null) else np.nan,
               climb_pct=float((df["vario"] > CLIMB_PCT_THR).mean())*100,
               climb_straight_pct=float(((df["vario"] > CLIMB_PCT_THR) & (np.abs(df["turn_deg_s"]) <= TURN_GATE_DEG_S)).sum()
                                         / max(int((np.abs(df["turn_deg_s"]) <= TURN_GATE_DEG_S).sum()), 1))*100,
               short_mean_climb=np.nanmean(scl) if scl else np.nan,
               abandon_climb=np.nanmean(sab) if sab else np.nan, short_sample_min=np.nanmean(sdur) if sdur else np.nan,
               route_km=route_km,
               free_km=round(free_km, 1), flat_tri_km=round(flat_km, 1), fai_tri_km=round(fai_km, 1),
               race_start_alt=race_start_alt)
    for k in ["mean_climb","peak_climb","frac_strong","climb_cv","radius_c1","radius_min","radius_mean","turns_to_min",
              "time_to_peak_s","coring_gain","lift_follow","directness_in","bearing_change_deg","post_exit_turn_std",
              "probes_before","dur_min"]:
        rec[k] = float(P[k].mean()) if (len(P) and k in P) else np.nan
    # "Finds the core" is only measured on genuinely BIG thermals (>= core_min_turns turns AND
    # >= core_min_gain m gain) - in a short climb there is no real core-finding to read. Within those,
    # split by thermal strength (hard cut on mean climb). Either side may be NaN on a given day.
    if len(P) and "mean_climb" in P and "coring_gain" in P:
        bc = P[(P["n_turns"] >= core_min_turns) & (P["gain_m"] >= core_min_gain)]
        sg = bc.loc[bc["mean_climb"] >= strong_climb, "coring_gain"]
        wk = bc.loc[bc["mean_climb"] <  strong_climb, "coring_gain"]
        rec["core_strong"] = float(sg.mean()) if sg.notna().sum() >= CORE_MIN_SAMPLES else np.nan
        rec["core_weak"]   = float(wk.mean()) if wk.notna().sum() >= CORE_MIN_SAMPLES else np.nan
        rec["n_core_thermals"] = int(len(bc))
    else:
        rec["core_strong"] = rec["core_weak"] = np.nan; rec["n_core_thermals"] = 0
    rec["time_in_min"] = rec.pop("dur_min")
    return rec, df

# ============================== RESULTS FILE ====================================
def read_results(path):
    try:
        r = pd.read_csv(path)
    except Exception as e:
        print(f"  (could not read results file {path}: {e})"); return {}
    cols = {c.lower().strip(): c for c in r.columns}
    keyc = next((cols[c] for c in ["file", "name", "pilot", "igc"] if c in cols), None)
    kmc = next((cols[c] for c in ["km", "xc_km", "fai_km", "distance", "result_km", "score"] if c in cols), None)
    spc = next((cols[c] for c in ["speed", "speed_kmh", "avg_speed", "kmh"] if c in cols), None)
    tyc = next((cols[c] for c in ["type", "discipline", "kind"] if c in cols), None)
    ptc = next((cols[c] for c in ["points", "score_points", "pts", "xc_points"] if c in cols), None)
    if keyc is None or kmc is None:
        print("  (results file needs a key column [file/name/pilot] and a km column)"); return {}
    out = {}
    for _, row in r.iterrows():
        key = str(row[keyc]).strip().lower()
        try: km = float(row[kmc])
        except Exception: continue
        sp = None
        if spc is not None:
            try: sp = float(row[spc])
            except Exception: sp = None
        ty = str(row[tyc]).strip() if (tyc is not None and pd.notna(row[tyc])) else None
        pt = None
        if ptc is not None:
            try: pt = float(row[ptc])
            except Exception: pt = None
        out[key] = (km, sp, ty, pt)
    return out

def match_result(results, file_stem, name):
    if not results: return None
    fs = file_stem.strip().lower(); nm = str(name).strip().lower()
    if fs in results: return results[fs]
    if nm and nm in results: return results[nm]
    for k, v in results.items():       # loose substring match on name
        if nm and (k in nm or nm in k): return v
    return None

# ============================== SIGNATURE TEXT ==================================
SIG = [  # (column, low-percentile phrase, high-percentile phrase) -- all RELATIVE to the same-day field
    ("big_per_h", "commits to fewer thermals than most", "stops to climb more often than most"),
    ("abandon_climb", "holds onto weak climbs longer than most", "leaves weak climbs sooner than most (selective)"),
    ("mean_climb", "softer average climbs than most", "stronger average climbs than most"),
    ("peak_climb", "lower peak climbs than most", "higher peak climbs than most"),
    ("coring_gain", "gains less from entry to core than most", "works up to a stronger core than most"),
    ("climb_straight_pct", "less straight-line climbing than most", "more straight-line climbing than most (lines/convergence)"),
]

def signature(row):
    cand = []
    for col, lop, hip in SIG:
        p = row.get(col+"_p", np.nan)
        if not np.isfinite(p): continue
        if p <= 0.20 and lop: cand.append((0.5-p, lop))
        elif p >= 0.80 and hip: cand.append((p-0.5, hip))
    cand.sort(reverse=True)
    return "; ".join(ph for _, ph in cand[:4]) if cand else "middle of the field on most measures"

# ============================== CORRELATION =====================================
FEATURES = ["big_per_h","circling_pct","climb_pct","climb_straight_pct","mean_climb","peak_climb","frac_strong","climb_cv","radius_c1",
            "radius_min","radius_mean","turns_to_min","time_to_peak_s","coring_gain","lift_follow","directness_in","bearing_change_deg",
            "post_exit_turn_std","probes_before","time_in_min","short_per_h","short_over_big","short_mean_climb",
            "abandon_climb","short_sample_min","avg_speed_kmh"]
NICE = {"big_per_h":"big thermals /h","circling_pct":"% time circling","climb_pct":"% time climbing","climb_straight_pct":"% climbing without circling","mean_climb":"mean climb m/s",
        "peak_climb":"peak climb m/s","frac_strong":"frac time in strong lift","climb_cv":"climb variation (CV)",
        "radius_c1":"1st-circle radius m","radius_min":"min circle radius m","radius_mean":"avg circle radius m","turns_to_min":"circles to tightest",
        "time_to_peak_s":"time to peak climb s","coring_gain":"coring gain m/s (core-entry)","lift_follow":"lift-tracking (toward stronger side)","directness_in":"glide directness",
        "bearing_change_deg":"entry->exit bearing chg","post_exit_turn_std":"post-exit turn scatter",
        "probes_before":"probes before big","time_in_min":"time in thermal min","short_per_h":"short thermals /h",
        "short_over_big":"short/big ratio","short_mean_climb":"short mean climb","abandon_climb":"abandon climb m/s",
        "short_sample_min":"short sample time min","avg_speed_kmh":"avg speed km/h"}

def within_day(df, col):
    g = df.groupby("day")[col]
    sd = g.transform("std").replace(0, np.nan)   # avoid divide-by-zero on degenerate days
    return (df[col]-g.transform("mean"))/sd

def correlate(df, target, sign):
    df = df.copy(); df["_tgt"] = sign*df[target]; tz = within_day(df, "_tgt")
    res = []
    for k in FEATURES:
        if k == target or k not in df or df[k].notna().sum() < 4: continue
        fz = within_day(df, k); m = tz.notna() & fz.notna()
        if m.sum() < 4 or np.nanstd(fz[m]) == 0: continue
        r, p = stats.pearsonr(fz[m], tz[m])
        res.append(dict(parameter=NICE.get(k, k), r=r, p=p, n=int(m.sum()), days=int(df.loc[m, "day"].nunique())))
    out = pd.DataFrame(res, columns=["parameter", "r", "p", "n", "days"])
    return out.reindex(out["r"].abs().sort_values(ascending=False).index).reset_index(drop=True) if not out.empty else out

# ============================== HTML TABLE ======================================
HTML_COLS = [("pilot","pilot",0),("day","day",0),("profile","profile",0),("launch","launch",0),("result_km","result km",1),("type_disp","type",0),("xc_points","XC points",1),
             ("avg_speed_kmh","speed km/h",1),("glide_airspeed","Glides fast",1),
             ("abandon_climb","Leaves weak thermals",1),("coring_gain","Finds the core",1),("climb_straight_pct","Climbs without circling",1),("flow_pct","Avoids getting stuck",1)]
INT_COLS = {"result_km", "fai_tri_km", "flat_tri_km", "free_km", "xc_points", "radius_min", "radius_mean", "circling_pct", "climb_pct", "climb_straight_pct", "glide_cov", "flow_pct"}
ONE_DP_COLS = {"big_per_h", "short_per_h", "abandon_climb", "mean_climb", "peak_climb", "coring_gain", "glide_airspeed"}

HTML_GLOSSARY = [
    ("launch", "take-off site: read from the IGC header when present, otherwise inferred from the nearest labelled take-off within 3 km (blank if unknown)"),
    ("result km", "the day's distance (whole-number km): your official figure, or our approximate best discipline if no official result matched"),
    ("type", "the discipline: FAI (triangle), flat (triangle) or free (open distance). A triangle is scored as perimeter minus its closing gap, so a badly-closed triangle loses the gap; it counts only if that gap is within ~20% of the perimeter. A trailing ~ means our approximation, not an official figure."),
    ("XC points", "XContest-style points = km x factor (FAI 1.4, flat 1.2, free 1.0); from your file if it has a points column, else computed"),
    ("speed km/h", "result km / air-time - the thing this whole tool is trying to explain"),
    ("Glides fast", "wind-corrected cruising airspeed on glide legs (km/h), after removing the local wind. The cell shows the spread of the pilot's glide airspeeds that day as a small histogram, with the median marked AS - higher = pushes more bar / cruises faster (a mix of speed-bar use, wing performance and the air flown through). How well the wind was sampled (the trust behind these numbers) is reported per day in the wind section at the foot of the page."),
    ("Leaves weak thermals", "climb (m/s) still showing when the pilot leaves a short thermal; high = picky (drops weak climbs and moves on), low = hangs on to weak lift"),
    ("Finds the core", "how much stronger the climb gets (m/s) from entry to the working core of the thermal, ignoring the top fade; high = works into the core, ~0 = sits in the lift first entered"),
    ("Climbs without circling", "share of the whole flight gaining height while NOT circling - climbing on lines / convergence / ridge without turning"),
    ("Avoids getting stuck", "share of 30-second blocks in which the pilot was getting something done - either advancing through the air (>=30 km/h wind-corrected straight-line progress) OR climbing decently (at least half the day's average climb). The opposite is a 'Baustelle': stuck, neither moving nor going up (scratching low, drifting, flying into sink). High = rarely stuck. This lever overlaps with speed by its nature - wasting less time almost is going faster - but it isolates a real, separate failure mode and helps explain pilots who stay fast without scoring high on pure technique. The climb bar is set per day (half the field's mean climb) so the measure works on weak and strong days alike."),
    ("profile", "spider/radar chart of the things a pilot can directly work on, each compared with the OTHER pilots flying the same day: Glides fast (carry more speed between thermals), Leaves weak thermals (drop a weak climb and move on sooner), Finds the core (work in to the strongest part of the climb), Climbs without circling (gain height while moving instead of always circling), and Avoids getting stuck (avoid blocks where you neither climb nor advance). The dashed circle is the day's average; a spoke outside it means stronger than the field that day, inside means room to improve. Always relative to that day, never absolute."),
]

def _fmt(v, num, key=None):
    if isinstance(v, float) and not np.isfinite(v): return "-"
    if num:
        try:
            if key in INT_COLS: return f"{float(v):.0f}"
            if key in ONE_DP_COLS: return f"{float(v):.1f}"
            return f"{float(v):.2f}".rstrip("0").rstrip(".")
        except Exception: return str(v)
    return _html.escape(str(v))

def explained_variance(df):
    """Adjusted R^2 of within-day points regressed on the measured behavioural parameters:
    the share of who-outscores-whom-on-a-day that this tool's measurements account for."""
    preds = [p for p in ["abandon_climb","core_strong","core_weak","climb_straight_pct","glide_airspeed","glide_ratio","turns_per_km"] if p in df.columns]
    tgt = "avg_speed_kmh"
    d = df.copy()
    def z(c):
        g = d.groupby("day")[c]; sd = g.transform("std").replace(0, np.nan)
        return (d[c]-g.transform("mean"))/sd
    Z = pd.DataFrame({c: z(c) for c in preds+[tgt]}).replace([np.inf, -np.inf], np.nan).dropna()
    n, k = len(Z), len(preds)
    if d["day"].nunique() < 2 or n < k+12: return n, None
    X = np.c_[np.ones(n), Z[preds].values]; y = Z[tgt].values.astype(float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    ssr = float(((y-X@beta)**2).sum()); sst = float(((y-y.mean())**2).sum())
    if sst <= 0: return n, None
    R2 = 1-ssr/sst
    return n, max(1-(1-R2)*(n-1)/(n-k-1), 0.0)

# The seven speed-indicative levers (label, raw column, invert-for-display). 'Flies far' is profile-only.
LEVERS_DE = [("Leaves weak thermals", "abandon_climb", False),
             ("Finds the core in strong thermals", "core_strong", False),
             ("Finds the core in weak thermals", "core_weak", False),
             ("Climbs without circling", "climb_straight_pct", False),
             ("Glides fast", "glide_airspeed", False),
             ("Mean glide ratio", "glide_ratio", False),
             ("Few turns per km", "turns_per_km", True)]
# control parameters: shown in the stats only, never in the profile/radar
CONTROL_DE = [("Richtungs-Effizienz", "richtung_pct", False),
              ("Null-Sink-Speed", "null_sink_speed", False)]
# profile axis shown on the radar but kept out of the speed regression
PROFILE_EXTRA_DE = [("Flies far", "flies_long", False)]

def _within_day_z(d, c):
    g = d.groupby("day")[c]; sd = g.transform("std").replace(0, np.nan)
    return (d[c]-g.transform("mean"))/sd

def _within_day_corr(df, col, tgt="avg_speed_kmh"):
    """Pearson r of one lever vs speed, both de-meaned within day (so it's a within-day relationship)."""
    if col not in df.columns or tgt not in df.columns:
        return None
    d = df.copy()
    x = _within_day_z(d, col); y = _within_day_z(d, tgt)
    m = x.notna() & y.notna()
    if m.sum() < 4 or float(np.nanstd(x[m])) == 0 or float(np.nanstd(y[m])) == 0:
        return None
    r, p = stats.pearsonr(x[m], y[m])
    return float(r), float(p), int(m.sum())

def _repeatability(flights, col):
    """Is this parameter a stable pilot trait or day-to-day noise? Within-day z-score the parameter,
    then one-way decomposition with pilot as the group (names merged). Returns (ICC, p, n_pilots):
    ICC = share of the spread that sits between pilots rather than within a pilot across days.
    High & significant = stable pilot trait (good spoke); near 0 = essentially random per pilot."""
    if col not in flights.columns or "day" not in flights.columns:
        return None
    d = flights[["day", col]].copy()
    d["_pid"] = [("".join(str(p).split()).casefold() if isinstance(p, str) else "?")
                 for p in (flights["pilot"] if "pilot" in flights.columns else flights.get("name", ""))]
    d["_z"] = _within_day_z(d, col)
    d = d.dropna(subset=["_z"])
    vc = d["_pid"].value_counts(); d = d[d["_pid"].isin(vc[vc >= 2].index)]
    k = d["_pid"].nunique(); N = len(d)
    if k < 3 or N < k + 3:
        return None
    grand = d["_z"].mean(); groups = [g["_z"].values for _, g in d.groupby("_pid")]
    ni = np.array([len(g) for g in groups])
    msb = sum(len(g)*(g.mean()-grand)**2 for g in groups)/(k-1)
    msw = sum(((g-g.mean())**2).sum() for g in groups)/(N-k)
    if msw <= 0:
        return None
    n0 = (N - (ni**2).sum()/N)/(k-1)
    icc = (msb-msw)/(msb+(n0-1)*msw) if (msb+(n0-1)*msw) > 0 else 0.0
    p = float(stats.f.sf(msb/msw, k-1, N-k))
    return float(max(icc, 0.0)), p, int(k)

def _speed_stats_block(flights):
    """Race vs XC: each lever's within-day correlation with speed, plus the joint explained variance.
    Below that, the one profile-only axis (Flies far) and a couple of control parameters."""
    def corr_rows(df, triples):
        out = ""
        for label, col, inv in triples:
            res = _within_day_corr(df, col)
            if res is None:
                out += f'<tr><td>{_html.escape(label)}</td><td class="num">\u2013</td><td class="sig"></td></tr>'; continue
            r, p, _ = res
            if inv:
                r = -r
            sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else ""))
            out += f'<tr><td>{_html.escape(label)}</td><td class="num">{r:+.2f}</td><td class="sig">{sig}</td></tr>'
        return out
    def col_html(df, title):
        nf = len(df); nd = df["day"].nunique()
        n, adj = explained_variance(df)
        ev = f"{round(adj*100)}%" if adj is not None else "\u2013 (not enough data)"
        return (f'<div class="statcol"><div class="stath">{title}</div>'
                f'<div class="statmeta">{nf} flights \u00b7 {nd} days</div>'
                f'<table class="stat"><tbody>{corr_rows(df, LEVERS_DE)}</tbody></table>'
                f'<div class="statev">explained variance of all seven together: <b>{ev}</b></div></div>')
    if "is_race" in flights.columns:
        race = flights[flights["is_race"] == True]; xc = flights[flights["is_race"] != True]
    else:
        race = flights.iloc[0:0]; xc = flights
    cols = (col_html(race, "Race") if len(race) else "") + col_html(xc, "XC")
    return (f'<p class="ovlead">How do the <b>seven speed levers</b> relate to average speed? '
            f'Everything is computed within each day \u2013 day by day, then pooled. <b>r</b> is the single correlation '
            f'of one lever with speed (+1 = moves fully together, 0 = no relationship); every lever is turned to its '
            f'\u201cgood\u201d direction, so plus always means \u201cmore of it \u2192 faster\u201d. The <b>explained '
            f'variance</b> is the joint share of all seven in the speed differences (adjusted R\u00b2). '
            f'Race and XC shown separately.</p>'
            f'<div class="statgrid">{cols}</div>'
            f'<div class="statnote">Significance: * p&lt;0.05 \u00b7 ** p&lt;0.01 \u00b7 *** p&lt;0.001. Correlation is no proof of causation.</div>')


# radar ("spider") profile: the four things a pilot can directly work on, each shown as the
# within-day percentile (dashed circle = day average; outward = stronger). Operational action
# labels; longest labels go top/bottom where there is horizontal room. (label, percentile-column, invert)
RADAR_AXES = [("Finds the core in strong thermals", "core_strong_p", False),
              ("Leaves weak thermals", "abandon_climb_p", False),
              ("Glides fast", "glide_airspeed_p", False),
              ("Mean glide ratio", "glide_ratio_p", False),
              ("Finds the core in weak thermals", "core_weak_p", False),
              ("Flies far", "flies_long_p", False),
              ("Few turns per km", "turns_per_km_p", True),
              ("Climbs without circling", "climb_straight_pct_p", False)]
# compact labels for the small pilot-page radars (same order as RADAR_AXES)
RADAR_SHORT = ["cores strong lift", "leaves weak lift", "glides fast", "good glide ratio",
               "cores weak lift", "flies far", "few turns/km", "climbs w/o circling"]

def _wrap2(s):
    words = s.split()
    if len(words) <= 1: return [s]
    best = None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        m = max(len(a), len(b))
        if best is None or m < best[0]: best = (m, a, b)
    return [best[1], best[2]]

def _radar_svg(fracs, labels=None, w=66, h=None):
    h = h or w
    cx, cy = w/2.0, h/2.0
    R = min(w, h)/2.0 - (34 if labels else 7)
    n = len(fracs); poly = []; axes = []; lab_svg = ""
    for i, f in enumerate(fracs):
        miss = (f is None) or (isinstance(f, float) and np.isnan(f))  # suppressed / no data -> skip this spoke
        a = np.radians(-90 + i*360.0/n)
        ca, sa = float(np.cos(a)), float(np.sin(a))
        if not miss:
            fv = max(0.08, min(1.0, float(f)))
            poly.append(f"{cx+R*fv*ca:.1f},{cy+R*fv*sa:.1f}")
        axes.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{cx+R*ca:.1f}" y2="{cy+R*sa:.1f}" '
                    f'stroke="{"#efefef" if miss else "#e0e0e0"}" stroke-width="0.6"/>')
        if labels:
            anchor = "start" if ca > 0.3 else ("end" if ca < -0.3 else "middle")
            lines = _wrap2(labels[i]); lx = cx+(R+9)*ca; ly = cy+(R+9)*sa
            fill = "#c8c8c8" if miss else "#555"
            for j, ln in enumerate(lines):
                dy = (j-(len(lines)-1)/2.0)*7.6
                lab_svg += (f'<text x="{lx:.1f}" y="{ly+dy:.1f}" font-size="7" fill="{fill}" '
                            f'text-anchor="{anchor}" dominant-baseline="middle">{_html.escape(ln)}</text>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<circle cx="{cx}" cy="{cy}" r="{R:.1f}" fill="none" stroke="#e6e6e6" stroke-width="0.8"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{R*0.5:.1f}" fill="none" stroke="#aab" stroke-width="0.7" stroke-dasharray="2,2"/>'
            f'{"".join(axes)}'
            f'<polygon points="{" ".join(poly)}" fill="rgba(52,96,127,0.33)" stroke="#34607f" stroke-width="1.3"/>'
            f'{lab_svg}</svg>')

def _dist_svg(counts, median, lo, hi, w=132, h=52):
    """Small histogram of a pilot's glide airspeeds that day, with the median marked 'AS' and a ticked km/h axis."""
    if not counts or sum(counts) == 0 or median is None or (isinstance(median, float) and np.isnan(median)):
        return '<span style="color:#bbb">-</span>'
    n = len(counts); mx = max(counts) or 1; bw = w/n; base = h-13
    X = lambda v: (0.5 if hi <= lo else min(1.0, max(0.0, (v-lo)/(hi-lo)))) * w
    bars = "".join(
        f'<rect x="{i*bw:.1f}" y="{base-(c/mx)*(base-10):.1f}" width="{max(bw-0.7, 0.6):.1f}" height="{(c/mx)*(base-10):.1f}" fill="#9bbcd3"/>'
        for i, c in enumerate(counts))
    xm = X(median)
    med = f'<line x1="{xm:.1f}" y1="2" x2="{xm:.1f}" y2="{base:.1f}" stroke="#b3001b" stroke-width="1.5"/>'
    anchor = "end" if xm > w-28 else "start"; tx = xm-2 if anchor == "end" else xm+2
    lab = f'<text x="{tx:.1f}" y="9" font-size="9" fill="#b3001b" text-anchor="{anchor}">AS {median:.0f}</text>'
    axis = f'<line x1="0" y1="{base:.1f}" x2="{w}" y2="{base:.1f}" stroke="#bbb" stroke-width="0.8"/>'
    ticks = ""
    t0 = int(np.ceil(lo/10.0)*10)
    for v in range(t0, int(hi)+1, 10):
        xv = X(v)
        anc = "start" if xv < 6 else ("end" if xv > w-6 else "middle")
        ticks += (f'<line x1="{xv:.1f}" y1="{base:.1f}" x2="{xv:.1f}" y2="{base+3:.1f}" stroke="#999" stroke-width="0.8"/>'
                  f'<text x="{xv:.1f}" y="{h-2}" font-size="7.5" fill="#888" text-anchor="{anc}">{v}</text>')
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">{bars}{axis}{ticks}{med}{lab}</svg>'


METHODS_MD = """
# Fly Faster — Methods

**This analysis is experimental.** It measures only what is in the GPS data — not courage, experience or luck — and compares each pilot exclusively with the others who are in the data on the **same day**. The goal of this description is reproducibility: anyone starting from the raw IGC files should be able to reconstruct essentially the same numbers.

Everything is computed **per day**: one subfolder of IGC files = one day. Every comparison a pilot sees (radar levers, explained variance) is a comparison within that day.

## 1. Reading & resampling

Every IGC file is resampled to **1 Hz**. Altitude is lightly smoothed (Savitzky-Golay, ~15 s); the **vario** is the derivative of the smoothed altitude (averaged over ~5 s). **GPS altitude** is the preferred altitude source — it is the always-present primary signal and keeps all pilots on the same source; barometric altitude serves only as a fallback when the GPS altitude itself looks defective (dropouts, impossible jumps).

## 2. The analysis window

So that early launchers who cover ground slowly at first are not penalised, the analysis starts for everyone at a **common point in time**:

- **Race day** — if the day folder carries a start time in its name (e.g. `…_UTC1000` for 10:00 UTC), everything before it is discarded. Each lever then measures only the race. The **altitude at race start** (GPS altitude at the start time) is also recorded.
- **Normal day** — the analysis starts at the **latest launch in the field + 10 minutes** (adjustable via `--start-cut-min`).

Distance, average speed and all levers are measured **only inside this window**; each pilot keeps their own end, so window lengths differ. Whoever lands before the common start has no window and drops out of the comparison.

## 3. Detecting thermals & circling

**Circling phases** are detected from the turn rate (degrees per second): above a gate of 6°/s counts as circling. Connected phases are grouped into **bouts** — big thermals (≥ 3 turns and ≥ 10 m gain) and short probing/climb attempts (1–3 turns). The thermal levers use every detected thermal.

## 4. The eight axes

Each axis is a measurable property of a flight. **Seven** of them are **speed levers** (they relate to average speed and feed the speed analysis); one — **Flies far** — describes style and is deliberately kept out.

**Speed levers:**

1. **Leaves weak thermals** — mean climb in the last ≤ 10 s of short climb bouts. Higher = walks away from weak lift sooner.
2. **Finds the core in strong thermals** — *coring gain* (core minus entry), measured **only on the bigger thermals** (at least **5 full turns** and **20 m height gain** — in very short climbs there is no real centring to measure), and of those only the ones with a mean climb **≥ 2 m/s**. The thermal is split into time sixths: entry = 1st sixth, core = strongest of sixths 2–4. Higher = centres faster. (At least two such thermals are required, otherwise the axis stays empty.)
3. **Finds the core in weak thermals** — the same for the bigger thermals with **< 2 m/s**, where centring is harder. (Thresholds adjustable: `--core-min-turns` / `--core-min-gain` for thermal size, `--strong-climb` for the strong/weak boundary. On days without enough bigger thermals, one or both axes stay empty.)
4. **Climbs without circling** — share of straight-flown time in which the pilot is nevertheless climbing (lines, convergence, ridge lift instead of full circles).
5. **Glides fast** — median of the **wind-corrected airspeed** on the glide legs (see Wind, section 5).
6. **Mean glide ratio** — mean glide ratio on the glide legs (distance covered per metre of height lost). Higher = flatter gliding.
7. **Few turns per km** — turns (full circles) per kilometre covered. Shown **inverted** on the radar: a long spoke means *few* turns per km, i.e. efficient progress. The actual number is printed under each flight radar.

**Profile axis without speed relevance:**

8. **Flies far** — distance covered compared with the field of the same day.

Glide ratio and turns per km partly overlap with the other levers (good gliding, little circling) but carry enough signal of their own to count as separate speed levers. Previously shown axes "Uses tailwind" and "High directional efficiency" were removed: a repeatability check across several days showed they carry almost no stable pilot signal (they fluctuate almost like chance from day to day). "Directional efficiency" and "zero-sink speed" are now shown only in the statistics section, not in the profile.

## 5. Wind from the gliders

A glider circling in a thermal drifts with the air — every climb is also a wind measurement. From the circles of **all** pilots, a time-, altitude- and location-dependent wind field is estimated (kernel smoothing). This wind is subtracted from every glide leg to obtain the wind-corrected airspeed (lever 5). As a check, each pilot is held out in turn and their wind predicted from the others (leave-one-pilot-out).

## 6. Distance, discipline & average speed

Without a declared task (the files contain none), the distance is **reconstructed**: free distance (up to 3 turnpoints) plus flat and FAI triangle. Triangles are scored XContest-style (perimeter minus closing gap). The **average speed** is the distance covered inside the window divided by the flight time inside the window.

## 7. Plausibility filter

Flights with physically impossible climb values (usually altitude-data dropouts) are discarded entirely, so that one broken file does not distort the day's field.

## 8. Terrain / height above ground

If a terrain model (DEM) for the region is available, the wind field uses height above ground; otherwise height above sea level. The **altitude at race start** is GPS altitude (above sea level), not above ground.

## 9. Day-relative normalisation & radar

Every lever is converted to a percentile rank (0–1) **within the day**. On the radar, the **dashed circle** is the day median, the **outer circle** the day's best value. A spoke touching the outer circle was the day's best in that lever. Everything is day-relative, never absolute.

How much the levers explain is shown by the **adjusted R²** of a within-day regression of average speed on the seven speed levers (Flies far excluded).

## 10. Honest limits

- Without a declared task the distance is reconstructed; at the edges (holding before the start, flying after the goal) the direction measurement stays fuzzy.
- The wind is only known where somebody circled; empty areas mean "not measured".
- The profile is **day-relative** — radar shapes from different days are not directly comparable.
- Everything is **correlative, not causal**: the levers relate to speed but are no proof of cause and effect.
"""

def _md_to_html(md):
    """Minimal Markdown -> HTML for the in-page methods (single-sourced from METHODS_MD)."""
    def inline(s):
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)
        return s
    lines = md.split("\n"); out = []; i = 0; n = len(lines)
    bullet = r"\s*[-*]\s+"; numbered = r"\s*\d+\.\s+"
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1; continue
        if ln.startswith("#"):
            m = re.match(r"(#+)\s*(.*)", ln); lvl = len(m.group(1))
            tag = {1: "h3", 2: "h4", 3: "h5"}.get(lvl, "h5")
            out.append("<%s>%s</%s>" % (tag, inline(m.group(2)), tag)); i += 1; continue
        if ln.strip() == "---":
            out.append("<hr/>"); i += 1; continue
        if ln.lstrip().startswith("|"):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            head = None; body = []
            for r in rows:
                if all(re.fullmatch(r":?-+:?", c or "-") for c in r):
                    continue
                if head is None: head = r
                else: body.append(r)
            h = "".join("<th>%s</th>" % inline(c) for c in (head or []))
            b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r) for r in body)
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (h, b)); continue
        if re.match(bullet, ln):
            items = []
            while i < n and (re.match(bullet, lines[i]) or (lines[i].startswith("  ") and lines[i].strip() and items)):
                if re.match(bullet, lines[i]): items.append(re.sub(bullet, "", lines[i], count=1))
                else: items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(x) for x in items)); continue
        if re.match(numbered, ln):
            items = []
            while i < n and (re.match(numbered, lines[i]) or (lines[i].startswith("   ") and lines[i].strip() and items)):
                if re.match(numbered, lines[i]): items.append(re.sub(numbered, "", lines[i], count=1))
                else: items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ol>%s</ol>" % "".join("<li>%s</li>" % inline(x) for x in items)); continue
        para = [ln]; i += 1
        while i < n and lines[i].strip() and not lines[i].startswith("#") and lines[i].strip() != "---" and not lines[i].lstrip().startswith("|") and not re.match(bullet, lines[i]) and not re.match(numbered, lines[i]):
            para.append(lines[i]); i += 1
        out.append("<p>%s</p>" % inline(" ".join(x.strip() for x in para)))
    return "\n".join(out)


def _parse_day(s):
    """Subfolder names express a date but with mixed separators / zero-padding
    (2026_5_1, 2026-05-29, 2026_05_25). Pull the first three integers as Y, M, D."""
    nums = re.findall(r"\d+", str(s))
    if len(nums) >= 3:
        try:
            return datetime.date(int(nums[0]), int(nums[1]), int(nums[2]))
        except Exception:
            return None
    return None

def _day_label(s):
    d = _parse_day(s)
    return d.strftime("%Y-%m-%d") if d else str(s)

def _day_launch_word(launches):
    """Short label for a day's launch site. Usually the single most common word across
    the day's launch strings; but if that word is very short (<= 3 letters, e.g. 'Val'),
    it is extended with the word that most often follows it, so 'Val Louron' is kept
    instead of being cut to 'Val'. (Launch strings vary: sometimes one word, sometimes more.)"""
    import collections
    seqs = []
    for L in launches:
        ws = [w for w in re.split(r"\s+", str(L).strip()) if w and not w.isdigit()]
        if ws:
            seqs.append(ws)
    if not seqs:
        return ""
    cnt = collections.Counter(); rep = {}
    for ws in seqs:
        for w in ws:
            if len(w) >= 2 and not w.isdigit():
                lw = w.lower(); cnt[lw] += 1; rep.setdefault(lw, w)
    if not cnt:
        return " ".join(seqs[0][:2])
    w0 = cnt.most_common(1)[0][0]
    label = rep[w0]
    if len(label) <= 3:
        nxt = collections.Counter(); nrep = {}
        for ws in seqs:
            for i in range(len(ws) - 1):
                if ws[i].lower() == w0:
                    nx = ws[i + 1]; nxt[nx.lower()] += 1; nrep.setdefault(nx.lower(), nx)
        if nxt:
            label = f"{label} {nrep[nxt.most_common(1)[0][0]]}"
        else:
            for ws in seqs:
                if w0 in (x.lower() for x in ws) and len(ws) >= 2:
                    label = " ".join(ws[:2]); break
    return label

def write_html(tbl, path, footnote, banner="", wind_html="", speeder_hist=None, speeder_range=(None, None), public=False, author=""):
    speeder_hist = speeder_hist or {}; slo, shi = speeder_range
    days = sorted(tbl["day"].astype(str).unique(),
                  key=lambda s: (_parse_day(s) or datetime.date.min), reverse=True)
    multi = len(days) > 1
    _launch_col = tbl["launch"] if "launch" in tbl.columns else None
    day_info = {}
    for d in days:
        sub = tbl[tbl["day"].astype(str) == d]
        launches = [x for x in (sub["launch"].tolist() if "launch" in sub.columns else []) if isinstance(x, str) and x.strip()]
        day_info[d] = (len(sub), _day_launch_word(launches),
                       bool(sub["is_race"].any()) if "is_race" in sub.columns else False)
    _n = lambda v: f"{float(v):.4f}" if (isinstance(v, (int, float)) and np.isfinite(v)) else "-1e9"
    def stat(v, lab, fmt):
        s = (fmt % float(v)) if (isinstance(v, (int, float)) and np.isfinite(v)) else "\u2013"
        return f'<div class="stat"><div class="sv">{s}</div><div class="sl">{lab}</div></div>'
    daycount = tbl["day"].astype(str).value_counts().to_dict()
    def _slug(s): return re.sub(r"[^A-Za-z0-9_-]", "-", str(s))
    def card_html(i, r):
        fr = [(1-r.get(col, np.nan)) if inv else r.get(col, np.nan) for _, col, inv in RADAR_AXES]
        radar = _radar_svg(fr, labels=[l for l, _, _ in RADAR_AXES], w=248, h=156)
        med = r.get("glide_airspeed", np.nan)
        dist = _dist_svg(speeder_hist.get(r.get("file"), []), med, slo, shi, w=248, h=54)
        name = _html.escape(str(r.get("name") or r.get("pilot") or "?"))
        day = str(r["day"]); ncmp = daycount.get(day, 1)
        sub = " \u00b7 ".join(x for x in [_html.escape(str(r.get("launch", "") or "")),
                                          _html.escape(str(r.get("type_disp", "") or ""))] if x)
        hero = stat(r.get("avg_speed_kmh"), "avg km/h", "%.1f") + stat(r.get("result_km"), "distance km", "%.0f") + stat(r.get("xc_points"), "XC points", "%.0f")
        cmp_badge = f'<div class="cmp"><div class="cn">{ncmp}</div><div class="cl">flights compared</div></div>'
        race_tag = ' <span class="racetag">Race</span>' if bool(r.get("is_race")) else ''
        rsa = r.get("race_start_alt", np.nan)
        rstart = (f'<div class="rstart">Altitude at race start: {float(rsa):.0f}\u202fm</div>'
                  if (bool(r.get("is_race")) and isinstance(rsa, (int, float)) and np.isfinite(rsa)) else '')
        gz = r.get("glide_ratio", np.nan); tpk = r.get("turns_per_km", np.nan)
        gz_s = f"{float(gz):.1f}" if isinstance(gz, (int, float)) and np.isfinite(gz) else "\u2013"
        tpk_s = f"{float(tpk):.2f}" if isinstance(tpk, (int, float)) and np.isfinite(tpk) else "\u2013"
        rnums = f'<div class="rnums">Glide ratio {gz_s} \u00b7 {tpk_s} turns/km</div>'
        return (f'<div class="card" style="animation-delay:{min(i,14)*0.04:.2f}s">'
                f'<div class="chd"><div class="nm">{name}{race_tag}</div>{cmp_badge}</div>'
                f'<div class="sub">{sub}</div><div class="hero">{hero}</div>{rstart}'
                f'<div class="radar">{radar}</div>{rnums}'
                f'<div class="distwrap"><div class="dl">Glide speed distribution (wind removed)</div>{dist}</div></div>')
    sections = ""
    for day in days:
        sub_tbl = tbl[tbl["day"].astype(str) == day].sort_values("avg_speed_kmh", ascending=False)
        cards = "".join(card_html(pos, r) for pos, (_, r) in enumerate(sub_tbl.iterrows()))
        n, launch, is_race = day_info[day]
        meta = (f"{_html.escape(launch)} \u00b7 " if launch else "") + f"{n} flights" + (" \u00b7 race" if is_race else "")
        head = f'<h2 class="dayhead">{_html.escape(_day_label(day))} <span class="dh-meta">\u00b7 {meta}</span></h2>' if multi else ''
        sections += f'<section class="day" data-day="{_html.escape(day)}">{head}<div class="grid">{cards}</div></section>'
    # Day selection only (no sort, no "all days"): one line per day; the newest day is
    # shown by default; picking another day swaps which section is visible, via CSS :has().
    if multi:
        rows = ""
        for k, d in enumerate(days):
            n, launch, is_race = day_info[d]
            meta = (f"{_html.escape(launch)} \u00b7 " if launch else "") + f"{n} flights" + (" \u00b7 race" if is_race else "")
            chk = " checked" if k == 0 else ""
            rows += (f'<label class="dayopt"><input type="radio" name="day" id="day-{_slug(d)}" value="{_html.escape(d)}"{chk}>'
                     f'<span class="do-main">{_html.escape(_day_label(d))}</span><span class="do-meta">{meta}</span></label>')
        controls = (f'<div class="controls"><div class="ctrlrow"><span class="lbl">Choose day</span>'
                    f'<div class="dayfilter" role="radiogroup" aria-label="Choose day">{rows}</div></div></div>')
    else:
        controls = ''
    # show only the selected day; the default-checked (newest) day hides the rest on load
    day_css = "".join('body:has(#day-%s:checked) .day:not([data-day="%s"]),body:has(#day-%s:checked) .windday:not([data-day="%s"]){display:none}'
                      % (_slug(d), _html.escape(d), _slug(d), _html.escape(d)) for d in days) if multi else ""
    dyn_css = day_css
    credit = (f'<footer class="credit">Analysis by {_html.escape(author)}</footer>') if author else ''
    big_radar = _radar_svg([0.55, 0.82, 0.70, 0.64, 0.62, 0.66, 0.58, 0.55], labels=[l for l, _, _ in RADAR_AXES], w=360, h=248)
    notes = [("Finds the core in strong thermals", "Increase in climb from entry to core \u2013 only in bigger thermals (\u2265 5 turns / 20 m) with a strong mean climb (\u2265 2 m/s)."),
             ("Leaves weak thermals", "Climb rate at the moment a weak thermal is left."),
             ("Glides fast", "Median wind-corrected airspeed on the glide legs."),
             ("Mean glide ratio", "Mean glide ratio on the glide legs (distance per height lost). The actual number is printed under the flight radar."),
             ("Finds the core in weak thermals", "The same in the bigger but weak thermals (< 2 m/s) \u2013 where centring is harder."),
             ("Flies far", "Distance covered compared with the field \u2013 profile only, does not feed the speed analysis."),
             ("Few turns per km", "Turns per kilometre covered \u2013 a long spoke means few turns per km (efficient progress). The actual number is printed under the flight radar."),
             ("Climbs without circling", "Share of straight-flown time in which the pilot is climbing.")]
    notes_html = "".join(f"<li><b>{_html.escape(t)}</b> \u2013 {_html.escape(d)}</li>" for t, d in notes)
    legend_html = (f'<div class="legend"><div class="legend-radar">{big_radar}</div>'
                   f'<div class="legend-text"><div class="lt-title">The profile \u2013 eight axes</div>'
                   f'<ul class="lt">{notes_html}</ul>'
                   f'<div class="lt-foot">Every axis is relative to the field of the same day: the <b>dashed line</b> is the '
                   f'day median, the <b>outer circle</b> the highest value reached that day. A spoke '
                   f'touching the outer circle was the best of the day in that lever. Exactly how this '
                   f'is computed is described in the methods below.</div></div></div>')
    intro_html = (
        '<div class="intro">'
        '<p><b>This analysis is experimental.</b> It measures what is in the GPS data \u2013 not courage, experience or '
        'luck \u2013 and compares each pilot only with the others who are in the data on the same day. Numbers are '
        'pointers, not a verdict.</p>'
        '<p>When we fly together on a good day in the same area and upload our igc files, interesting data piles up. '
        'Conditions were similar for everyone \u2013 so the strengths and '
        'weaknesses of the pilots should become visible in the data. That is exactly what this is about: a <b>pilot profile</b>. '
        'Speed is a big part of it, but not everything.</p>'
        '<p>The analysis relies on nothing but the igc files and makes a same-day comparison of all the flights '
        'behind it. Even the wind is estimated from the flight data, by time and altitude.</p>'
        '<p>The profile shows <b>eight axes</b>. <b>Seven</b> of them are <b>speed levers</b> \u2013 they relate to '
        'average speed and are things one can (in theory) work on to get faster. '
        'The eighth, <b>Flies far</b> (distance covered), describes style and deliberately does not feed the '
        'speed analysis. There are surely more levers.</p>'
        '<p>How well you stand and where potential remains is meant purely <b>relative to the flights analysed that '
        'day</b>, never absolutely. The details of how everything is computed follow below.</p>'
        '</div>')
    methods_block = f'<div class="methods">{_md_to_html(METHODS_MD)}</div>'
    info_html = (f'<details id="info" class="infofold"><summary class="isum">What is this? '
                 f'\u2013 introduction, profile legend, methods &amp; wind</summary>'
                 f'<div class="infobody">{intro_html}{legend_html}{methods_block}{wind_html}</div></details>')
    profile_html = (f'<details id="profile" class="infofold profilefold"><summary class="isum">Pilot profiles '
                    f'\u2013 mean profile per pilot, ordered by completeness</summary>'
                    f'<div class="infobody">{_overview_block(tbl)}</div></details>')
    stats_html = (f'<details id="stats" class="infofold statfold"><summary class="isum">Statistics '
                  f'\u2013 what counts for speed? (race &amp; XC)</summary>'
                  f'<div class="infobody">{_speed_stats_block(tbl)}</div></details>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Fly faster</title>
<style>*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#f5f5f7;color:#1d1d1f;margin:0;padding:34px 24px;-webkit-font-smoothing:antialiased}}
h1{{font-size:34px;font-weight:600;letter-spacing:-.022em;margin:0 0 4px}}
.tag{{color:#86868b;font-size:15px;margin:0 0 22px;max-width:780px;line-height:1.5}}
.intro{{max-width:780px;color:#3a3a3c;font-size:15px;line-height:1.55;margin:0 0 22px}}.intro p{{margin:10px 0}}
.link{{color:#0071e3;cursor:pointer;text-decoration:none}}.link:hover{{text-decoration:underline}}
.legend{{display:flex;gap:24px;align-items:center;flex-wrap:wrap;background:#fff;border-radius:20px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06);max-width:900px;margin:0 0 26px}}
.legend-radar{{flex:0 0 auto}}.legend-text{{flex:1 1 280px;min-width:250px}}
.lt-title{{font-size:15px;font-weight:600;margin-bottom:8px}}
ul.lt{{margin:0;padding:0;list-style:none;font-size:12.5px;color:#3a3a3c;line-height:1.5}}ul.lt li{{margin:5px 0}}
.lt-foot{{font-size:11.5px;color:#86868b;margin-top:12px;line-height:1.45}}
.details-wrap{{margin:30px 0 0;max-width:1280px;border-top:1px solid #e0e0e2;padding-top:6px}}
.infofold{{max-width:1280px;margin:0 0 26px;border:1px solid #e3e3e6;border-radius:16px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.infofold>.isum{{cursor:pointer;list-style:none;font-size:15px;font-weight:600;color:#0071e3;padding:16px 20px;display:flex;align-items:center;gap:9px}}
.infofold>.isum::-webkit-details-marker{{display:none}}
.infofold>.isum::before{{content:"\\25B8";font-size:12px;color:#0071e3}}
.infofold[open]>.isum::before{{content:"\\25BE"}}
.infofold[open]>.isum{{border-bottom:1px solid #ececef}}
.infobody{{padding:14px 22px 22px}}
.infobody .intro,.infobody .legend{{margin-top:6px}}
.infobody .methods{{border-top:1px solid #ececef;margin-top:18px;padding-top:14px}}
.methods{{max-width:920px;color:#3a3a3c;font-size:13.5px;line-height:1.6}}
.methods h4{{font-size:15px;font-weight:600;margin:18px 0 4px;color:#1d1d1f}}
.methods p{{margin:6px 0}}.methods ul{{margin:6px 0;padding-left:18px}}.methods li{{margin:4px 0}}
.methods code{{background:#eef0f4;padding:1px 4px;border-radius:4px;font-size:12px}}
.methods h3{{font-size:21px;font-weight:600;margin:6px 0 10px}}
.methods h5{{font-size:13.5px;font-weight:600;margin:12px 0 2px;color:#1d1d1f}}
.methods ol{{margin:6px 0;padding-left:20px}}
.methods hr{{border:0;border-top:1px solid #e6e6e8;margin:20px 0}}
.methods table{{border-collapse:collapse;margin:10px 0;font-size:12.5px}}
.methods th,.methods td{{border:1px solid #ddd;padding:4px 11px;text-align:left}}
.methods th{{background:#f0f0f3;font-weight:600}}
.banner{{background:#fff;border-radius:14px;padding:14px 18px;margin:0 0 20px;font-size:13.5px;line-height:1.5;box-shadow:0 1px 3px rgba(0,0,0,.06);max-width:920px;color:#3a3a3c}}
.controls{{display:flex;flex-direction:column;gap:18px;margin:0 0 26px;max-width:560px}}
.ctrlrow{{display:flex;flex-direction:column;gap:8px}}
.seg{{display:inline-flex;background:#e9e9eb;border-radius:10px;padding:2px;flex-wrap:wrap;align-self:flex-start}}
.segb{{position:relative;cursor:pointer;font:inherit;font-size:14px;color:#1d1d1f;padding:8px 15px;border-radius:8px;transition:.15s;-webkit-tap-highlight-color:transparent}}
.segb input{{position:absolute;opacity:0;width:1px;height:1px;margin:0}}
.segb:has(input:checked){{background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.12);font-weight:600}}
.segb:focus-within{{outline:2px solid #0071e3;outline-offset:1px}}
.lbl{{color:#86868b;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
.dayfilter{{display:flex;flex-direction:column;background:#fff;border:1px solid #e3e3e6;border-radius:12px;overflow:hidden;max-width:440px}}
.dayopt{{position:relative;display:flex;align-items:baseline;gap:12px;padding:11px 15px;cursor:pointer;border-top:1px solid #f1f1f3;-webkit-tap-highlight-color:transparent}}
.dayopt:first-child{{border-top:0}}
.dayopt input{{position:absolute;opacity:0;width:1px;height:1px;margin:0}}
.dayopt:has(input:checked){{background:#eef5ff}}
.dayopt:focus-within{{outline:2px solid #0071e3;outline-offset:-2px}}
.do-main{{font-size:14.5px;font-weight:600;color:#1d1d1f}}
.dayopt:has(input:checked) .do-main{{color:#0071e3}}
.do-meta{{font-size:12.5px;color:#86868b;margin-left:auto;text-align:right}}
.dh-meta{{font-weight:400;color:#86868b;font-size:14px}}
.racetag{{display:inline-block;vertical-align:middle;margin-left:8px;padding:2px 9px;border-radius:999px;background:#1d1d1f;color:#fff;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase}}
.rstart{{margin:-4px 0 14px;color:#6e6e73;font-size:13px}}
.rnums{{text-align:center;color:#6e6e73;font-size:12.5px;margin:-2px 0 10px;font-variant-numeric:tabular-nums}}
.credit{{max-width:1280px;margin:54px 0 10px;padding-top:18px;border-top:1px solid #ececef;color:#a1a1a6;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px;max-width:1280px}}
.day{{max-width:1280px}}
.dayhead{{font-size:18px;font-weight:600;color:#1d1d1f;letter-spacing:-.01em;margin:30px 0 12px;padding-bottom:6px;border-bottom:1px solid #e3e3e6;max-width:1280px}}
.card{{background:#fff;border-radius:20px;padding:22px 22px 18px;box-shadow:0 1px 3px rgba(0,0,0,.06);opacity:0;animation:fadeup .55s cubic-bezier(.2,.7,.3,1) forwards;transition:box-shadow .2s}}
.card:hover{{box-shadow:0 8px 26px rgba(0,0,0,.10)}}
@keyframes fadeup{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
.chd{{display:flex;justify-content:space-between;align-items:flex-start}}
.cmp{{text-align:right}}.cmp .cn{{font-size:18px;font-weight:600;color:#1d1d1f;line-height:1}}.cmp .cl{{font-size:9.5px;color:#86868b;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}}
.nm{{font-size:19px;font-weight:600;letter-spacing:-.01em}}
.sub{{color:#86868b;font-size:12.5px;margin:2px 0 16px}}
.hero{{display:flex;gap:20px;margin:0 0 4px}}
.stat .sv{{font-size:25px;font-weight:600;letter-spacing:-.015em}}
.stat .sl{{font-size:10px;color:#86868b;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}}
.radar{{display:flex;justify-content:center;margin:6px 0 2px}}
.distwrap{{margin-top:8px}}.dl{{font-size:10.5px;color:#86868b;margin-bottom:2px;line-height:1.35}}
.foot{{color:#86868b;font-size:12px;margin:28px 0 0;max-width:920px;line-height:1.5}}
details{{margin:22px 0;max-width:920px}}summary{{cursor:pointer;font-size:15px;font-weight:600;color:#1d1d1f}}
.details-wrap>summary.dsum{{list-style:none;color:#0071e3;font-size:15px;font-weight:600;padding:10px 0;display:inline-block}}
.details-wrap>summary.dsum::-webkit-details-marker{{display:none}}
.details-wrap>summary.dsum::before{{content:"\\25B8  ";color:#0071e3}}
.details-wrap[open]>summary.dsum::before{{content:"\\25BE  "}}
dl{{font-size:12.5px;margin-top:10px}}dt{{font-weight:600;margin-top:10px;color:#1d1d1f}}dd{{margin:2px 0 0;color:#6e6e73;line-height:1.5}}
h2,h3{{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:32px 0 8px}}
.windintro{{max-width:920px;color:#3a3a3c;font-size:13.5px;line-height:1.55}}.windintro p{{margin:6px 0}}
.windday{{display:block;margin:12px 0 16px;max-width:1280px}}.windday img{{width:100%;max-width:1280px;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.wc{{font-size:12px;color:#6e6e73;margin-top:4px;max-width:980px;line-height:1.5}}
.endlink{{max-width:1280px;margin:28px 0 0;font-size:14px;color:#6e6e73}}
.card svg,.legend svg,.distwrap svg,.radar svg{{max-width:100%;height:auto}}
.legend-radar svg{{width:360px}}
@media (max-width:760px){{
 body{{padding:20px 14px}}
 h1{{font-size:27px}}
 .grid{{grid-template-columns:1fr;gap:14px;max-width:100%}}
 .card{{padding:18px 16px 14px}}
 .legend{{padding:16px;gap:14px}}.legend-radar{{width:100%;display:flex;justify-content:center}}.legend-radar svg{{width:280px}}
 .legend-text{{min-width:0;flex-basis:100%}}
 .intro,.windintro,.methods{{font-size:14px}}
 .windday img{{border-radius:10px}}
 .methods table{{display:block;overflow-x:auto}}
}}
@media (prefers-reduced-motion:reduce){{.card{{opacity:1;animation:none}}}}
.profilefold .period{{display:inline-block;background:#f5f7fa;border-radius:10px;padding:9px 15px;margin:2px 0 14px;font-size:14px;color:#3a3a3c}}
.profilefold .ovlead{{max-width:820px;color:#3a3a3c;font-size:14px;line-height:1.55;margin:2px 0 10px}}
.profilefold .warn{{max-width:840px;background:#fff;border-left:4px solid #d08400;border-radius:10px;padding:11px 15px;margin:0 0 18px;font-size:13px;line-height:1.5;color:#3a3a3c}}
.ogrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:14px}}
.ocell{{position:relative;background:#fbfbfd;border:1px solid #eceef1;border-radius:16px;padding:12px 8px 10px;text-align:center}}
.orank{{position:absolute;top:10px;left:12px;font-size:13px;font-weight:700;color:#0071e3;background:#eef5ff;border-radius:20px;min-width:24px;height:24px;line-height:24px;padding:0 7px}}
.oradar svg{{max-width:100%;height:auto}}
.oname{{font-size:15px;font-weight:600;color:#1d1d1f;margin-top:2px}}
.ometa{{font-size:12.5px;color:#6e6e73;margin-top:1px}}
@media (max-width:760px){{.ogrid{{grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}}}}
.statgrid{{display:flex;flex-wrap:wrap;gap:18px;margin:6px 0}}
.statcol{{flex:1 1 280px;min-width:260px;background:#fbfbfd;border:1px solid #eceef1;border-radius:14px;padding:12px 14px}}
.stath{{font-size:15px;font-weight:600;color:#1d1d1f}}
.statmeta{{font-size:12px;color:#86868b;margin-bottom:6px}}
table.stat{{width:100%;border-collapse:collapse;font-size:13px}}
table.stat td{{padding:4px 2px;border-top:1px solid #f0f0f2}}
table.stat td:first-child{{width:100%}}
table.stat td.num{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:#1d1d1f;white-space:nowrap;padding-right:0}}
table.stat td.sig{{text-align:left;width:26px;padding-left:3px;color:#0071e3;font-weight:600;white-space:nowrap}}
table.stat td.num.weak{{color:#b0b0b5;font-weight:500}}
.repcol{{max-width:560px;margin-top:18px}}
.statev{{margin-top:8px;font-size:13.5px;color:#3a3a3c}}
.ctrlh{{margin-top:14px;padding-top:10px;border-top:1px solid #ececef;font-size:12px;font-weight:600;color:#86868b}}
.statnote{{font-size:12px;color:#86868b;margin-top:10px}}
{dyn_css}</style></head><body>
<h1>Fly faster</h1>
{info_html}
{profile_html}
{stats_html}
{controls}
{sections}
{credit}
</body></html>"""
    open(path, "w", encoding="utf-8").write(doc)

# ============================== PDF =============================================
def draw_table(pdf, tbl, title="Day result and parameters (sorted by result)"):
    cols = [("pilot","pilot"),("result_km","result\nkm"),("type_disp","type"),("xc_points","XC\npts"),
            ("avg_speed_kmh","speed\nkm/h"),("big_per_h","big/h"),("abandon_climb","abandon"),
            ("mean_climb","mean\ncl"),("peak_climb","peak\ncl"),("coring_gain","core+")]
    intcols = {"result_km", "xc_points"}
    rows_all = tbl.sort_values("result_km", ascending=False)
    per = 26
    for p0 in range(0, len(rows_all), per):
        chunk = rows_all.iloc[p0:p0+per]
        cell = []
        for _, r in chunk.iterrows():
            row = [str(r["pilot"])[:20]]
            for k, _ in cols[1:]:
                v = r[k]
                if isinstance(v, str): row.append(v)
                elif pd.notna(v) and isinstance(v, (int, float, np.integer, np.floating)):
                    row.append(f"{float(v):.0f}" if k in intcols else f"{float(v):.1f}")
                else: row.append("-")
            cell.append(row)
        fig = plt.figure(figsize=(8.27, 11.69)); ax = fig.add_axes([0.03, 0.04, 0.94, 0.88]); ax.axis("off")
        t = ax.table(cellText=cell, colLabels=[c[1] for c in cols], cellLoc="center",
                     colWidths=[0.20, 0.075, 0.07, 0.075, 0.06, 0.08, 0.07, 0.085, 0.075, 0.07, 0.07], loc="upper center")
        t.auto_set_font_size(False); t.set_fontsize(8); t.scale(1, 1.4)
        for (rr, cc), c in t.get_celld().items():
            c.set_edgecolor("#DDD")
            if rr == 0: c.set_facecolor("#34495E"); c.set_text_props(color="white", fontweight="bold")
            elif cc == 0: c.get_text().set_ha("left")
        fig.suptitle(title, fontsize=13, fontweight="bold", y=0.965)
        pdf.savefig(fig); plt.close(fig)

def draw_signatures(pdf, tbl, title="Per-pilot signature (most distinctive measured traits)"):
    rows = tbl.sort_values("result_km", ascending=False)
    per = 16
    for p0 in range(0, len(rows), per):
        chunk = rows.iloc[p0:p0+per]
        fig = plt.figure(figsize=(8.27, 11.69)); ax = fig.add_axes([0.05, 0.04, 0.9, 0.9]); ax.axis("off"); y = 0.97
        for _, r in chunk.iterrows():
            ax.text(0.0, y, f"{str(r['pilot'])[:30]}", fontsize=9.5, fontweight="bold", transform=ax.transAxes, va="top")
            ax.text(1.0, y, f"{r['result_km']:.0f} km", fontsize=9, ha="right", transform=ax.transAxes, va="top", color="#444")
            body = textwrap.fill(str(r["signature"]), 92)
            ax.text(0.0, y-0.018, body, fontsize=8, transform=ax.transAxes, va="top", color="#333")
            y -= 0.018 + 0.016*(body.count("\n")+1) + 0.016
        fig.suptitle(title, fontsize=13, fontweight="bold")
        pdf.savefig(fig); plt.close(fig)

def draw_corr(pdf, corr, target_label, n_days):
    fig = plt.figure(figsize=(8.27, 11.69)); ax = fig.add_axes([0.06, 0.05, 0.88, 0.86]); ax.axis("off")
    rows = [[r["parameter"], f"{r['r']:+.2f}", f"{r['p']:.3f}", str(r["n"]), str(r["days"])] for _, r in corr.iterrows()]
    t = ax.table(cellText=rows, colLabels=["parameter", f"r vs {target_label}", "p~", "n", "days"],
                 cellLoc="center", colWidths=[0.5, 0.2, 0.12, 0.09, 0.09], loc="upper center")
    t.auto_set_font_size(False); t.set_fontsize(8.5); t.scale(1, 1.5)
    for (rr, cc), c in t.get_celld().items():
        c.set_edgecolor("#DDD")
        if rr == 0: c.set_facecolor("#34495E"); c.set_text_props(color="white", fontweight="bold")
        else:
            c.get_text().set_ha("left" if cc == 0 else "center")
            v = corr.iloc[rr-1]["r"]
            if abs(v) >= 0.4: c.set_facecolor("#E8F6EF" if v > 0 else "#FCEDEC")
    note = "" if n_days >= 3 else "  (one/few days: exploratory only)"
    fig.suptitle(f"What correlates with {target_label}{note}", fontsize=13, fontweight="bold", y=0.965)
    pdf.savefig(fig); plt.close(fig)

GLOSSARY = [
    ("result km", "The day's RESULT. Uses your official figure from results.csv when matched (src=XC); otherwise our APPROXIMATE best of FAI-triangle / flat-triangle / free distance computed from the track (src=approx). Approximations are usually a little under official and are not certified scoring.", "outcome"),
    ("avg speed km/h", "Result km divided by air-time. A simple speed-of-the-day.", "outcome"),
    ("big thermals /h", "Committed thermals (>=3 circles) per hour. Low = selective.", "selectivity"),
    ("abandon climb m/s", "When he LEAVES a short thermal, how fast he was STILL climbing in the last ~10 s = the strength of the lift he walked away from (NOT how soon). High = picky; low = he hangs on until it nearly dies.", "selectivity"),
    ("mean climb m/s", "Average climb in his big thermals. An OUTCOME (confirms he climbs well, doesn't explain how).", "outcome"),
    ("peak climb m/s", "Strongest single second of climb in a thermal.", "outcome"),
    ("frac time in strong lift", "Share of thermal time above 75% of that thermal's peak. Higher = sits in the core.", "core centring"),
    ("climb variation (CV)", "How jumpy the climb is within a thermal. Lower = steadier = better centred.", "core centring"),
    ("min circle radius m", "Tightest circle (curvature radius, drift-independent). Larger = circles wider/faster.", "centring technique"),
    ("1st-circle radius m", "How wide the very first circle is on entering.", "centring technique"),
    ("circles to tightest", "How many circles before reaching the tightest. Fewer = homes in faster.", "centring technique"),
    ("time to peak climb s", "Seconds from entering until the strongest climb. Lower = finds the core fast.", "centring technique"),
    ("centring gain m/s", "Climb in the 2nd half of a thermal minus the 1st. Positive = centring keeps improving the climb.", "centring technique"),
    ("glide directness", "Straight-line / actual path of the glide INTO a thermal. ~1 = beelined there; lower = searched on the way.", "approach"),
    ("entry->exit bearing chg", "Change of travel direction across a thermal. Small = carried straight on.", "approach"),
    ("post-exit turn scatter", "How much he's still turning in the minute after leaving. Low = committed glide; high = searching.", "approach"),
    ("probes before big", "Short thermals sampled within ~2 min / 1.5 km before committing to a big one. High = tries several, takes the best.", "search"),
    ("short thermals /h", "How often he samples-and-leaves (per hour).", "selectivity"),
    ("short/big ratio", "Sampled-and-left thermals per committed one. High = selective sampling.", "selectivity"),
    ("% time circling", "Share of the whole flight spent turning. Lower = more time cruising.", "outcome / efficiency"),
]

def draw_glossary(pdf):
    per = 8
    for p in range(0, len(GLOSSARY), per):
        chunk = GLOSSARY[p:p+per]
        fig = plt.figure(figsize=(8.27, 11.69)); ax = fig.add_axes([0.06, 0.04, 0.9, 0.9]); ax.axis("off"); y = 0.97
        for nice, meaning, idea in chunk:
            ax.text(0.0, y, nice, fontsize=9.5, fontweight="bold", transform=ax.transAxes, va="top")
            body = textwrap.fill(meaning, 98); nl = body.count("\n")+1
            ax.text(0.0, y-0.020, body, fontsize=8, transform=ax.transAxes, va="top", color="#333")
            ax.text(0.0, y-0.020-0.017*nl, idea, fontsize=7.5, style="italic", transform=ax.transAxes, va="top", color="#2471A3")
            y -= 0.020 + 0.017*nl + 0.017 + 0.014
        fig.suptitle("What each column means", fontsize=13, fontweight="bold")
        pdf.savefig(fig); plt.close(fig)

# ============================== WIND-CORRECTED GLIDE AIRSPEED ===================
# Per day, harvest wind vectors from every pilot's circling (the mean ground velocity
# over full turns IS the wind drift), build a local (x,y,z,t) field, then on each
# flight's glide legs subtract the interpolated wind from ground velocity to get
# airspeed. Only well-sampled glide fixes are used; coverage is reported alongside.
WIND_HXY, WIND_HZ, WIND_HT = 4000.0, 400.0, 1800.0     # metres, metres, seconds
WIND_MIN_TURNS, WIND_MIN_DUR, WIND_MAX_DRIFT = 1.5, 30, 40.0
TZ_OFFSET_H = 2   # local clock = UTC + this (Alps summer/CEST); only labels the wind time-of-day windows
GLIDE_COV_MIN, GLIDE_AS_MAX, GLIDE_GS_MAX = 1.5, 70.0, 110.0
GLIDE_MIN_LEG_S, GLIDE_MIN_LEG_FIX, GLIDE_MIN_LEGS, GLIDE_LEG_GAP = 20, 8, 3, 20   # leg: >=20 s span & >=8 sampled fixes; brief <=20 s climbs/turns don't split a glide; need >=3 legs
BAUSTELLE_BLOCK, BAUSTELLE_SPEED = 30, 30.0          # 30 s blocks; "advancing" = >=30 km/h air-relative progress
BAUSTELLE_CLIMB_FRAC, BAUSTELLE_CLIMB_MIN = 0.5, 0.3  # "climbing" = >= half the day's mean climb (>= 0.3 m/s floor)

def _start_sod(path):
    for ln in open(path, encoding="latin-1", errors="ignore"):
        if ln.startswith("B") and len(ln.strip()) >= 35:
            return int(ln[1:3])*3600 + int(ln[3:5])*60 + int(ln[5:7])
    return 0.0

def _wind_samples(frames, CX, CY, terrain=None):
    S = []
    for fr in frames:
        lat = fr["lat"].to_numpy(); lon = fr["lon"].to_numpy()
        X = CX(lat, lon); Y = CY(lat, lon)
        t = fr["t"].to_numpy(); alt = fr["alt"].to_numpy(); sd = fr["sod"].to_numpy(); turn = fr["turn_deg_s"].to_numpy()
        big, short = detect_bouts(fr)
        for seg in big + short:
            a, b = seg["a"], seg["b"]
            if b-a < WIND_MIN_DUR or abs(np.sum(turn[a:b+1]))/360.0 < WIND_MIN_TURNS: continue
            a2, b2 = a+4, b-4; dt = t[b2]-t[a2]
            if dt < 20: continue
            vx = (X[b2]-X[a2])/dt; vy = (Y[b2]-Y[a2])/dt
            if np.hypot(vx, vy)*3.6 > WIND_MAX_DRIFT: continue
            z = float(alt[a:b+1].mean())
            if terrain is not None:
                ev = terrain.elevation(lat[a:b+1], lon[a:b+1]); ev = ev[np.isfinite(ev)]
                if ev.size: z = z - float(ev.mean())              # height above ground
            S.append([X[a2:b2+1].mean(), Y[a2:b2+1].mean(), z, sd[a2:b2+1].mean(), vx, vy])
    return np.array(S) if S else np.zeros((0, 6))

def _glide_airspeed(fr, S, CX, CY, terrain=None):
    if len(S) == 0: return np.nan, 0.0, np.array([]), np.nan
    sx, sy, sz, st, svx, svy = S.T
    lat = fr["lat"].to_numpy(); lon = fr["lon"].to_numpy(); X = CX(lat, lon); Y = CY(lat, lon)
    alt = fr["alt"].to_numpy(); sd = fr["sod"].to_numpy(); turn = fr["turn_deg_s"].to_numpy(); vario = fr["vario"].to_numpy()
    vx = np.convolve(np.gradient(X), np.ones(9)/9, "same"); vy = np.convolve(np.gradient(Y), np.ones(9)/9, "same")
    idx = np.where((np.abs(turn) <= TURN_GATE_DEG_S) & (vario < 0.5))[0]   # cruising, not circling, not climbing
    if len(idx) == 0: return np.nan, 0.0, np.array([]), np.nan
    qz = alt[idx].astype(float)
    if terrain is not None:
        g = terrain.elevation(lat[idx], lon[idx]); qz = np.where(np.isfinite(g), qz-g, qz)
    dx = X[idx][:, None]-sx[None, :]; dy = Y[idx][:, None]-sy[None, :]
    w = np.exp(-0.5*((np.hypot(dx, dy)/WIND_HXY)**2 + ((qz[:, None]-sz[None, :])/WIND_HZ)**2 + ((sd[idx][:, None]-st[None, :])/WIND_HT)**2))
    cov = w.sum(1); sw = np.where(cov > 1e-6, cov, np.nan)
    wx = (w*svx[None, :]).sum(1)/sw; wy = (w*svy[None, :]).sum(1)/sw
    asp = np.hypot(vx[idx]-wx, vy[idx]-wy)*3.6
    gs = np.hypot(vx[idx], vy[idx])*3.6
    ok = (cov >= GLIDE_COV_MIN) & np.isfinite(asp) & (asp < GLIDE_AS_MAX) & (gs > 10) & (gs < GLIDE_GS_MAX)
    cov_frac = float(ok.mean())
    # tailwind component on glides: wind projected onto the direction of travel (m/s, + = tailwind).
    speed = np.hypot(vx[idx], vy[idx])
    twc = np.where(speed > 1e-6, (wx*vx[idx] + wy*vy[idx])/np.maximum(speed, 1e-6), np.nan)
    tailwind = float(np.nanmean(twc[ok])) if ok.any() and np.isfinite(twc[ok]).any() else np.nan
    # split the glide fixes into contiguous legs (small lift/turn blips of <=4 s don't break a leg);
    # one airspeed per leg = median over its well-sampled fixes, so brief fast blips get averaged out
    legs = []
    for seg in np.split(np.arange(len(idx)), np.where(np.diff(idx) > GLIDE_LEG_GAP)[0] + 1):
        if len(seg) == 0: continue
        dur = idx[seg[-1]] - idx[seg[0]] + 1
        so = seg[ok[seg]]
        if dur >= GLIDE_MIN_LEG_S and len(so) >= GLIDE_MIN_LEG_FIX:
            legs.append(float(np.median(asp[so])))
    legs = np.array(legs)
    if len(legs) < GLIDE_MIN_LEGS: return np.nan, cov_frac, np.array([]), tailwind
    return float(np.median(legs)), cov_frac, legs, tailwind

def _baustelle_pct(fr, S, CX, CY, terrain=None, climb_thr=0.8):
    """Share of 30 s blocks that are 'Baustellen': the pilot neither advances through the air (>=30 km/h
    wind-corrected straight-line progress) nor climbs decently (>= half the day's mean climb). A real
    distinct failure mode (scratching low, drifting, flying into sink) separate from raw technique."""
    blk = BAUSTELLE_BLOCK
    if len(S) == 0: return np.nan
    sx, sy, sz, st, svx, svy = S.T
    lat = fr["lat"].to_numpy(); lon = fr["lon"].to_numpy(); X = CX(lat, lon); Y = CY(lat, lon)
    alt = fr["alt"].to_numpy(); sd = fr["sod"].to_numpy()
    n = (len(X)//blk)*blk
    if n < blk*3: return np.nan
    rb = lambda a: a[:n].reshape(-1, blk)
    Xb, Yb, Ab, Sb, LAb, LOb = rb(X), rb(Y), rb(alt), rb(sd), rb(lat), rb(lon)
    dt = Sb[:, -1]-Sb[:, 0]; dt = np.where(dt > 0, dt, blk)
    xc, yc, tc, zc = Xb.mean(1), Yb.mean(1), Sb.mean(1), Ab.mean(1)
    if terrain is not None:
        g = terrain.elevation(LAb.mean(1), LOb.mean(1)); zc = np.where(np.isfinite(g), zc-g, zc)
    dx = xc[:, None]-sx[None, :]; dy = yc[:, None]-sy[None, :]
    w = np.exp(-0.5*((np.hypot(dx, dy)/WIND_HXY)**2 + ((zc[:, None]-sz[None, :])/WIND_HZ)**2 + ((tc[:, None]-st[None, :])/WIND_HT)**2))
    cov = w.sum(1); sw = np.where(cov > 1e-6, cov, 1.0)
    wx = (w*svx[None, :]).sum(1)/sw; wy = (w*svy[None, :]).sum(1)/sw
    low = cov < GLIDE_COV_MIN; wx[low] = 0.0; wy[low] = 0.0          # unsampled -> assume calm (use ground progress)
    air = np.hypot((Xb[:, -1]-Xb[:, 0])-wx*dt, (Yb[:, -1]-Yb[:, 0])-wy*dt)/dt*3.6
    climb = (Ab[:, -1]-Ab[:, 0])/dt
    dead = (air < BAUSTELLE_SPEED) & (climb < climb_thr)
    return 100.0*float(dead.mean())

def add_glide_airspeed(flights, dbg, terrain=None):
    kept = set(flights["file"])
    byday = {}
    for d in dbg: byday.setdefault(d.get("day", "?"), []).append(d)
    mc_map = dict(zip(flights["file"], flights["mean_climb"]))
    out = {}; outb = {}
    for day, items in byday.items():
        frames = [it["df"] for it in items]
        lat = np.concatenate([fr["lat"].to_numpy() for fr in frames]); lon = np.concatenate([fr["lon"].to_numpy() for fr in frames])
        rlat, rlon = lat.mean(), lon.mean(); kx = np.cos(np.radians(rlat))*111320.0; ky = 110540.0
        CX = (lambda rlon, kx: (lambda la, lo: (lo-rlon)*kx))(rlon, kx)
        CY = (lambda rlat, ky: (lambda la, lo: (la-rlat)*ky))(rlat, ky)
        S = _wind_samples(frames, CX, CY, terrain)
        dc = [mc_map[it["file"]] for it in items if it["file"] in kept and np.isfinite(mc_map.get(it["file"], np.nan))]
        day_climb = float(np.mean(dc)) if dc else np.nan
        thr = max(BAUSTELLE_CLIMB_FRAC*day_climb, BAUSTELLE_CLIMB_MIN) if np.isfinite(day_climb) else 0.8
        for it in items:
            if it["file"] in kept:
                out[it["file"]] = _glide_airspeed(it["df"], S, CX, CY, terrain)
                outb[it["file"]] = _baustelle_pct(it["df"], S, CX, CY, terrain, thr)
    NA = (np.nan, 0.0, np.array([]), np.nan)
    flights["glide_airspeed"] = flights["file"].map(lambda f: round(out.get(f, NA)[0], 1) if np.isfinite(out.get(f, NA)[0]) else np.nan)
    flights["glide_cov"] = flights["file"].map(lambda f: int(round(100*out.get(f, NA)[1])))
    flights["flow_pct"] = flights["file"].map(lambda f: round(100.0-outb[f]) if (f in outb and np.isfinite(outb[f])) else np.nan)
    flights["tailwind_use"] = flights["file"].map(lambda f: round(out.get(f, NA)[3], 2) if np.isfinite(out.get(f, NA)[3]) else np.nan)
    # shared airspeed range + per-flight histogram for the in-table distribution
    pooled = np.concatenate([a for (_, _, a, _) in out.values() if len(a)]) if out else np.array([])
    if pooled.size:
        lo = max(15.0, np.floor(np.percentile(pooled, 1)/5)*5); hi = 70.0
    else:
        lo, hi = 25.0, 70.0
    NB = 14; edges = np.linspace(lo, hi, NB+1)
    hist = {}
    for f, (_, _, a, _) in out.items():
        hist[f] = (np.histogram(np.clip(a, lo, hi), bins=edges)[0].tolist() if len(a) else [])
    flights.attrs["speeder_hist"] = hist; flights.attrs["speeder_lo"] = float(lo); flights.attrs["speeder_hi"] = float(hi)
    return flights

def build_wind_section(dbg, terrain=None, daycov=None):
    """Honest per-day wind display: the measured drift vectors (only where pilots circled),
    plus a leave-one-pilot-out cross-check of how well pooling predicts a held-out pilot's wind."""
    import io, base64
    daycov = daycov or {}
    byday = {}
    for d in dbg: byday.setdefault(d.get("day", "?"), []).append(d)
    agl = terrain is not None
    blocks = []
    for day in sorted(byday, reverse=True):
        frames = [(it.get("name") or it["file"], it["df"]) for it in byday[day]]
        lat = np.concatenate([fr["lat"].to_numpy() for _, fr in frames]); lon = np.concatenate([fr["lon"].to_numpy() for _, fr in frames])
        rlat, rlon = lat.mean(), lon.mean(); kx = np.cos(np.radians(rlat))*111.320; ky = 110.540  # km/deg
        S = []
        for pi, (nm, fr) in enumerate(frames):
            la = fr["lat"].to_numpy(); lo = fr["lon"].to_numpy(); X = (lo-rlon)*kx; Y = (la-rlat)*ky
            t = fr["t"].to_numpy(); alt = fr["alt"].to_numpy(); sd = fr["sod"].to_numpy(); turn = fr["turn_deg_s"].to_numpy()
            big, short = detect_bouts(fr)
            for seg in big+short:
                a, b = seg["a"], seg["b"]
                if b-a < WIND_MIN_DUR or abs(np.sum(turn[a:b+1]))/360.0 < WIND_MIN_TURNS: continue
                a2, b2 = a+4, b-4; dt = t[b2]-t[a2]
                if dt < 20: continue
                vx = (X[b2]-X[a2])/dt*3600.0; vy = (Y[b2]-Y[a2])/dt*3600.0   # km/h
                if np.hypot(vx, vy) > WIND_MAX_DRIFT: continue
                z = float(alt[a:b+1].mean())
                if agl:
                    ev = terrain.elevation(la[a:b+1], lo[a:b+1]); ev = ev[np.isfinite(ev)]
                    if ev.size: z = z - float(ev.mean())
                if (agl and not (-200 <= z <= 4500)) or ((not agl) and not (-100 <= z <= 6500)): continue
                S.append([X[a2:b2+1].mean(), Y[a2:b2+1].mean(), z, sd[a2:b2+1].mean(), vx, vy, pi])
        S = np.array(S) if S else np.zeros((0, 7))
        # leave-one-pilot-out cross-check
        val, nval = np.nan, 0
        if len(S) >= 8:
            sx, sy, sz, st, svx, svy, spi = S.T
            errs = []
            for i in range(len(S)):
                m = spi != spi[i]
                if m.sum() < 3: continue
                w = np.exp(-0.5*((np.hypot(sx[m]-sx[i], sy[m]-sy[i])/(WIND_HXY/1000))**2
                                 + ((sz[m]-sz[i])/WIND_HZ)**2 + ((st[m]-st[i])/WIND_HT)**2))
                sw = w.sum()
                if sw < 1.0: continue
                px = (w*svx[m]).sum()/sw; py = (w*svy[m]).sum()/sw
                errs.append(float(np.hypot(px-svx[i], py-svy[i])))
            if errs: val, nval = float(np.median(errs)), len(errs)
        # figure: one small map per time-of-day window, arrows = grid-averaged measured wind, numbers = km/h
        cell = 7.0
        def grid(selx, sely, selh, selu, selv):
            ix = np.floor(selx/cell).astype(int); iy = np.floor(sely/cell).astype(int)
            gx, gy, gu, gv, gh = [], [], [], [], []
            for key in set(zip(ix.tolist(), iy.tolist())):
                m = (ix == key[0]) & (iy == key[1])
                if m.sum() < 3: continue
                gx.append((key[0]+0.5)*cell); gy.append((key[1]+0.5)*cell)
                gu.append(selu[m].mean()); gv.append(selv[m].mean()); gh.append(np.median(selh[m]))
            return map(np.array, (gx, gy, gu, gv, gh))
        ncells = 0
        if len(S):
            sx, sy, sz, ssd, svx, svy = S[:, 0], S[:, 1], S[:, 2], S[:, 3], S[:, 4], S[:, 5]
            lhour = (ssd + TZ_OFFSET_H*3600.0)/3600.0
            w = 2 if (np.nanmax(lhour)-np.nanmin(lhour)) <= 8 else 3
            b0 = int(np.floor(np.nanmin(lhour)/w)*w); b1 = int(np.ceil(np.nanmax(lhour)/w)*w)
            wins = [(h, h+w) for h in range(b0, max(b1, b0+w), w)]
            wins = [(a, b) for (a, b) in wins if ((lhour >= a) & (lhour < b)).sum() >= 6] or [(b0, b0+w)]
            vmin, vmax = (np.percentile(sz, [5, 95]) if len(sz) >= 5 else (float(sz.min()), float(sz.max())))
            allx = np.concatenate([(fr["lon"].to_numpy()-rlon)*kx for _, fr in frames])
            ally = np.concatenate([(fr["lat"].to_numpy()-rlat)*ky for _, fr in frames])
            xlim = (float(allx.min())-2, float(allx.max())+2); ylim = (float(ally.min())-2, float(ally.max())+2)
            L = min(max(0.06*(xlim[1]-xlim[0]), 3.0), 7.0)   # fixed arrow length (km): direction is shown, speed is the label
            import matplotlib.colors as _mc
            norm = _mc.Normalize(vmin=float(vmin), vmax=float(vmax)); cmap = plt.get_cmap("viridis")
            n = len(wins)
            fig, axs = plt.subplots(1, n, figsize=(3.2*n, 3.4), squeeze=False); axs = axs[0]
            drew = False
            for ax, (a, b) in zip(axs, wins):
                sel = (lhour >= a) & (lhour < b)
                for nm, fr in frames:   # only the track flown inside this window (honest about time)
                    fsd = (fr["sod"].to_numpy() + TZ_OFFSET_H*3600.0)/3600.0; fm = (fsd >= a) & (fsd < b)
                    if fm.any():
                        ax.plot((fr["lon"].to_numpy()[fm]-rlon)*kx, (fr["lat"].to_numpy()[fm]-rlat)*ky,
                                ".", ms=0.3, color="#e2e2e2", zorder=1)
                gx, gy, gu, gv, gh = grid(sx[sel], sy[sel], sz[sel], svx[sel], svy[sel])
                ax.set_title("%02d\u2013%02d h  (%d)" % (a % 24, b % 24, int(sel.sum())), fontsize=8)
                if len(gx):
                    ncells += len(gx); drew = True
                    mag = np.hypot(gu, gv); safe = np.where(mag > 1e-6, mag, 1.0)
                    U = gu/safe*L; V = gv/safe*L
                    for x, y, u, v, m, hh in zip(gx, gy, U, V, mag, gh):
                        ax.annotate("", xy=(x+u, y+v), xytext=(x, y), zorder=3,
                                    arrowprops=dict(arrowstyle="->", color=cmap(norm(hh)), lw=0.7,
                                                    mutation_scale=6, shrinkA=0, shrinkB=0))
                        ax.annotate("%.0f" % m, (x+u, y+v), textcoords="offset points", xytext=(2, 1),
                                    fontsize=5, color="#444", zorder=4)
                ax.set_aspect("equal"); ax.grid(alpha=0.2, lw=0.4); ax.tick_params(labelsize=6)
                ax.set_xlim(*xlim); ax.set_ylim(*ylim)
                ax.set_xlabel("km E", fontsize=7)
            axs[0].set_ylabel("km N", fontsize=7)
            if drew:
                sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap); sm.set_array([])
                cb = fig.colorbar(sm, ax=list(axs), shrink=0.8, pad=0.012)
                cb.set_label("height (%s)" % ("m AGL" if agl else "m MSL"), fontsize=7); cb.ax.tick_params(labelsize=6)
            fig.suptitle("%s \u2014 Wind by time of day, local (UTC+%d); arrow = direction, number = km/h" % (day, TZ_OFFSET_H), fontsize=9, y=1.04)
        else:
            fig, ax = plt.subplots(figsize=(4, 3)); ax.text(0.5, 0.5, "no wind measurements this day", ha="center", va="center"); ax.axis("off")
        buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=160, bbox_inches="tight"); plt.close(fig)
        b64 = base64.b64encode(buf.getvalue()).decode()
        note = "Each panel is a 2\u20133-hour window; an arrow shows the wind direction in a 7-km cell (3+ measurements), the number is the speed in km/h. Empty space = nobody circled there at that time. "
        covtxt = (" The wind-corrected airspeed is reliable over ~%d%% of the glide time this day on average." % round(daycov[day])) if day in daycov else ""
        chk = note + (("Cross-check: holding out each pilot in turn and predicting their wind from the others "
               "is off by %.1f km/h in the median (%d measurements)." % (val, nval)) if np.isfinite(val) else "too few measurements for a cross-check this day.") + covtxt
        blocks.append(f'<div class="windday" data-day="{_html.escape(str(day))}"><img src="data:image/png;base64,{b64}" alt="wind {day}"/>'
                      f'<div class="wc">{_html.escape(chk)}</div></div>')
    intro = ('<h3>Wind \u2014 measured by the gliders themselves</h3>'
             '<div class="windintro"><p>These wind maps come from the flights, not from a forecast. A glider '
             'circling in a thermal drifts with the air \u2013 every climb is also a wind measurement: we use the '
             'paragliders as anemometers. From the circles of all pilots the day wind is reconstructed and subtracted from every '
             'glide leg to obtain the wind-corrected airspeed. Each day is split into time-of-day windows '
             'because the wind turns through the day; within a window one arrow per 7-km cell shows the mean wind '
             '(labelled in km/h), coloured by altitude. Only actually measured wind is shown \u2013 empty '
             'areas just mean nobody circled there at the time \u2013 and it is cross-checked by holding out each pilot '
             'and predicting their wind from the others.</p></div>')
    return intro + "".join(blocks)

def wind_strength_bias(dbg, terrain=None, strong_pct=80):
    """Diagnostic: do strong thermals drift slower than the air ('chimney' effect), biasing the wind field low?
    For each thermal, predict the ambient wind from nearby WEAK thermals of OTHER pilots (same place/height/time),
    then compare its own along-wind drift to that local ambient. Held-out weak thermals are the control (~1.0)."""
    agl = terrain is not None
    byday = {}
    for d in dbg: byday.setdefault(d.get("day", "?"), []).append(d)
    out = {"weak": [], "strong": []}
    for day, items in byday.items():
        frames = [(it.get("name") or it["file"], it["df"]) for it in items]
        lat = np.concatenate([fr["lat"].to_numpy() for _, fr in frames]); lon = np.concatenate([fr["lon"].to_numpy() for _, fr in frames])
        rlat, rlon = lat.mean(), lon.mean(); kx = np.cos(np.radians(rlat))*111320.0; ky = 110540.0
        rows = []
        for pi, (nm, fr) in enumerate(frames):
            la = fr["lat"].to_numpy(); lo = fr["lon"].to_numpy(); X = (lo-rlon)*kx; Y = (la-rlat)*ky
            t = fr["t"].to_numpy(); alt = fr["alt"].to_numpy(); sd = fr["sod"].to_numpy(); turn = fr["turn_deg_s"].to_numpy()
            big, short = detect_bouts(fr)
            for seg in big+short:
                a, b = seg["a"], seg["b"]
                if b-a < WIND_MIN_DUR or abs(np.sum(turn[a:b+1]))/360.0 < WIND_MIN_TURNS: continue
                a2, b2 = a+4, b-4; dt = t[b2]-t[a2]
                if dt < 20: continue
                vx = (X[b2]-X[a2])/dt; vy = (Y[b2]-Y[a2])/dt
                if np.hypot(vx, vy)*3.6 > WIND_MAX_DRIFT: continue
                z = float(alt[a:b+1].mean())
                if agl:
                    ev = terrain.elevation(la[a:b+1], lo[a:b+1]); ev = ev[np.isfinite(ev)]
                    if ev.size: z = z - float(ev.mean())
                if (agl and not (-200 <= z <= 4500)) or ((not agl) and not (-100 <= z <= 6500)): continue
                rows.append([X[a2:b2+1].mean(), Y[a2:b2+1].mean(), z, sd[a2:b2+1].mean(), vx, vy, pi, (alt[b]-alt[a])/(t[b]-t[a])])
        if len(rows) < 12: continue
        S = np.array(rows); x, y, z, td, vx, vy, pid, strg = S.T
        thr = np.percentile(strg, strong_pct); strong = strg >= thr
        if strong.sum() < 3 or (~strong).sum() < 5: continue
        for i in range(len(S)):
            ref = (~strong) & (pid != pid[i]); ref[i] = False
            if ref.sum() < 3: continue
            w = np.exp(-0.5*((np.hypot(x[ref]-x[i], y[ref]-y[i])/WIND_HXY)**2 + ((z[ref]-z[i])/WIND_HZ)**2 + ((td[ref]-td[i])/WIND_HT)**2))
            sw = w.sum()
            if sw < 1.0: continue
            Px = (w*vx[ref]).sum()/sw; Py = (w*vy[ref]).sum()/sw; Pm = np.hypot(Px, Py)
            if Pm < 1e-3: continue
            dpar = (vx[i]*Px + vy[i]*Py)/Pm
            out["strong" if strong[i] else "weak"].append((Pm*3.6, np.hypot(vx[i], vy[i])*3.6, dpar*3.6, float(strg[i])))
    print("\n=== WIND TRACER BIAS CHECK (do strong thermals defy the wind?) ===")
    print("Reference wind per thermal = nearby WEAK thermals of OTHER pilots (same place/height/time). Strong = top %d%% climb." % (100-strong_pct))
    for g in ("weak", "strong"):
        A = np.array(out[g])
        if len(A) == 0: print("  %-7s no usable comparisons" % g); continue
        Pm, Dm, dpar, st = A.T
        print("  %-7s n=%4d | ambient %4.1f | total drift %4.1f | along-wind drift %4.1f km/h | along/ambient %.2f | deficit %+4.1f km/h"
              % (g, len(A), np.median(Pm), np.median(Dm), np.median(dpar), np.median(dpar/Pm), np.median(Pm-dpar)))
    print("  Read: weak ratio ~1.0 = method valid & sampling controlled; strong ratio <1 = strong thermals drift slower than the")
    print("  surrounding air, so pooling them biases the wind LOW (and speeder airspeeds slightly HIGH). Big gap + many flights = real.\n")

# ============================== MAIN ============================================
def _route_tp_idx(x, y, max_legs=4):
    """Original-fix indices of the free-distance turnpoints (start..end). Up to max_legs
    legs (max_legs+1 points); races use more so the reconstructed course follows the task."""
    si = simplify_indices(x, y, max_pts=180)
    P = np.c_[x[si], y[si]]; N = len(P)
    if N < 2: return np.array([0, len(x) - 1])
    L = max_legs + 1
    D = np.sqrt(((P[:, None, :] - P[None, :, :]) ** 2).sum(-1)) / 1000.0
    dp = np.zeros((L, N)); bk = -np.ones((L, N), int)
    for l in range(1, L):
        for j in range(1, N):
            v = dp[l - 1, :j] + D[:j, j]; i = int(np.argmax(v)); dp[l, j] = v[i]; bk[l, j] = i
    bl = bj = 0; bv = -1.0
    for l in range(1, L):
        j = int(np.argmax(dp[l]))
        if dp[l, j] > bv: bv = dp[l, j]; bl, bj = l, j
    p = [bj]; l, j = bl, bj
    while l >= 1:
        i = bk[l, j]; p.append(i); j = i; l -= 1
    return si[np.array(p[::-1])]

def _richtung_pct(df, is_race=False):
    """Richtungseffizienz: share of flight time making real progress toward the next
    reconstructed turnpoint. Over a RICHTUNG_WIN window each fix gets a velocity-made-good
    (closing speed on the current goal); the value is 100 * fraction of time with VMG > 0.
    For races the tail after the final turnpoint is dropped (post-goal flight isn't 'backwards')."""
    x = df["x_m"].to_numpy(); y = df["y_m"].to_numpy(); N = len(x)
    if N < 2 * RICHTUNG_WIN: return np.nan
    tps = _route_tp_idx(x, y, max_legs=6 if is_race else 4)
    P = np.c_[x, y]; H = RICHTUNG_WIN // 2; aidx = np.arange(N)
    gi = np.clip(np.searchsorted(tps, aidx, side="right"), 0, len(tps) - 1); G = P[tps[gi]]
    lo = np.clip(aidx - H, 0, N - 1); hi = np.clip(aidx + H, 0, N - 1); dt = np.maximum(hi - lo, 1)
    dlo = np.hypot(G[:, 0] - P[lo, 0], G[:, 1] - P[lo, 1])
    dhi = np.hypot(G[:, 0] - P[hi, 0], G[:, 1] - P[hi, 1])
    vmg = (dlo - dhi) / dt * 3.6
    end = max(int(tps[-1]) + 1 if is_race else N, 2)
    vmg = vmg[:end]
    return 100.0 * float(np.mean(vmg > 0))

def add_richtung(flights, dbg):
    """Map the per-flight Richtungseffizienz onto the table (race tail trimmed on race days)."""
    kept = set(flights["file"])
    race_files = set(flights.loc[flights["is_race"] == True, "file"]) if "is_race" in flights.columns else set()
    out = {d["file"]: _richtung_pct(d["df"], is_race=(d["file"] in race_files)) for d in dbg if d["file"] in kept}
    flights["richtung_pct"] = flights["file"].map(lambda f: round(out[f], 1) if (f in out and np.isfinite(out[f])) else np.nan)
    return flights

def _slug(s): return re.sub(r"[^A-Za-z0-9_-]", "-", str(s))

def _avg_radar_svg(rows, w=224, h=178):
    """Mean radar across a pilot's flights with a +/-1 SE band; returns (svg, mean_per_spoke).
    Applies each axis's invert flag (like the flight cards). A spoke with no finite data
    (e.g. 'Flies far' for a pilot with only race flights) is suppressed: skipped in the
    shape, faded axis, greyed label, and returned as NaN so it stays out of any ranking."""
    axes = RADAR_AXES; n = len(axes)
    M = []; LO = []; HI = []; miss = []; band = False
    for _label, c, inv in axes:
        vals = np.array([float(r.get(c, np.nan)) for r in rows], float)
        if inv:
            vals = 1.0 - vals
        v = vals[np.isfinite(vals)]
        if len(v) == 0:
            miss.append(True); M.append(np.nan); LO.append(np.nan); HI.append(np.nan); continue
        miss.append(False)
        m = float(v.mean())
        se = (float(v.std(ddof=1))/np.sqrt(len(v))) if len(v) >= 2 else 0.0
        if se > 0: band = True
        M.append(m); LO.append(m-se); HI.append(m+se)
    cx, cy = w/2.0, h/2.0; R = min(w, h)/2.0 - 34
    def ang(i): return np.radians(-90 + i*360.0/n)
    def xy(f, i):
        f = 0.5 if (f is None or not np.isfinite(f)) else max(0.08, min(1.0, f))
        a = ang(i); return cx+R*f*np.cos(a), cy+R*f*np.sin(a)
    present = [i for i in range(n) if not miss[i]]
    ax = lab = ""
    for i in range(n):
        a = ang(i); ca, sa = float(np.cos(a)), float(np.sin(a))
        ax += '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="0.6"/>' % (
            cx, cy, cx+R*ca, cy+R*sa, "#efefef" if miss[i] else "#e0e0e0")
        anchor = "start" if ca > 0.3 else ("end" if ca < -0.3 else "middle")
        lx = cx+(R+9)*ca; ly = cy+(R+9)*sa; lines = _wrap2(RADAR_SHORT[i])
        fill = "#c8c8c8" if miss[i] else "#555"
        for j, ln in enumerate(lines):
            dy = (j-(len(lines)-1)/2.0)*7.6
            lab += ('<text x="%.1f" y="%.1f" font-size="7" fill="%s" text-anchor="%s" dominant-baseline="middle">%s</text>'
                    % (lx, ly+dy, fill, anchor, _html.escape(ln)))
    ring = ""
    if band and len(present) >= 3:
        o = " ".join("%.1f,%.1f" % xy(HI[i], i) for i in present)
        ii = " ".join("%.1f,%.1f" % xy(LO[i], i) for i in reversed(present))
        ring = '<path d="M %s Z M %s Z" fill="rgba(52,96,127,0.16)" fill-rule="evenodd" stroke="none"/>' % (o, ii)
    poly = " ".join("%.1f,%.1f" % xy(M[i], i) for i in present)
    svg = (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
           f'<circle cx="{cx}" cy="{cy}" r="{R:.1f}" fill="none" stroke="#e6e6e6" stroke-width="0.8"/>'
           f'<circle cx="{cx}" cy="{cy}" r="{R*0.5:.1f}" fill="none" stroke="#aab" stroke-width="0.7" stroke-dasharray="2,2"/>'
           f'{ax}{ring}<polygon points="{poly}" fill="rgba(52,96,127,0.30)" stroke="#34607f" stroke-width="1.5"/>{lab}</svg>')
    return svg, M

def _merge_pilots(tbl):
    """Group rows by person, merging whitespace/case name variants; return {display_name: [rows]}."""
    groups = {}; disp = {}
    for _, r in tbl.iterrows():
        raw = str(r.get("pilot") or r.get("name") or "?")
        key = "".join(raw.split()).casefold()
        groups.setdefault(key, []).append(r)
        cur = disp.get(key)
        if cur is None or (raw.count(" "), len(raw)) > (cur.count(" "), len(cur)):
            disp[key] = raw
    return {disp[k]: rows for k, rows in groups.items()}

def write_pilots_html(tbl, path, author=""):
    """Second view: one section per pilot, their day-radars in chronological order, to show how
    a profile develops over time. Big caveat: every radar is day-relative, so shapes from
    different days are only loosely comparable. Pure CSS, no JavaScript (works in any viewer)."""
    # Merge name variants of the same person (e.g. "Stephanie Westerhuis" vs "StephanieWesterhuis").
    pilots = _merge_pilots(tbl)
    def ndays(rows): return len({str(x["day"]) for x in rows})
    order = sorted([p for p in pilots if len(pilots[p]) >= 3], key=lambda p: (-len(pilots[p]), p.lower()))
    ref = _radar_svg([0.7]*len(RADAR_AXES), labels=[l for l, _, _ in RADAR_AXES], w=360, h=248)
    def mini(r):
        fr = [(1-r.get(col, np.nan)) if inv else r.get(col, np.nan) for _, col, inv in RADAR_AXES]
        radar = _radar_svg(fr, labels=RADAR_SHORT, w=224, h=178)
        sp = r.get("avg_speed_kmh", np.nan)
        spd = (f"{float(sp):.1f} km/h") if (isinstance(sp, (int, float)) and np.isfinite(sp)) else ""
        race = " \u00b7 race" if bool(r.get("is_race")) else ""
        return (f'<div class="pcell"><div class="pday">{_html.escape(_day_label(str(r["day"])))}{race}</div>'
                f'<div class="pradar">{radar}</div><div class="pspd">{spd}</div></div>')
    def avg_cell(rows):
        svg, _ = _avg_radar_svg(rows)
        return (f'<div class="pcell pavg"><div class="pday">Average</div>'
                f'<div class="pradar">{svg}</div><div class="pspd">{len(rows)} flights \u00b7 \u00b11 SE</div></div>')
    sel_rows = ""; secs = ""
    for k, p in enumerate(order):
        rows = sorted(pilots[p], key=lambda x: (_parse_day(str(x["day"])) or datetime.date.min))
        nd = ndays(rows); slug = _slug(p); chk = " checked" if k == 0 else ""
        tage = "day" if nd == 1 else "days"
        sel_rows += (f'<label class="popt"><input type="radio" name="pilot" id="pilot-{slug}" value="{_html.escape(p)}"{chk}>'
                     f'<span class="po-main">{_html.escape(p)}</span>'
                     f'<span class="po-meta">{nd} {tage} \u00b7 {len(rows)} flights</span></label>')
        prog = "" if nd >= 2 else '<div class="ponote">Only one day in the data \u2013 no development visible yet.</div>'
        avg = avg_cell(rows) if len(rows) >= 3 else ""
        secs += (f'<section class="pilotsec" data-pilot="{_html.escape(p)}"><h2 class="phead">{_html.escape(p)}</h2>'
                 f'{prog}<div class="pstrip">{avg}{"".join(mini(r) for r in rows)}</div></section>')
    pilot_css = "".join('body:has(#pilot-%s:checked) .pilotsec:not([data-pilot="%s"]){display:none}'
                        % (_slug(p), _html.escape(p)) for p in order)
    if not order:
        sel_rows = '<div class="popt"><span class="po-meta" style="margin:0">\u2013</span></div>'
        secs = '<p class="lead">No pilot with at least three flights in the data yet \u2013 a development profile only shows over several shared days.</p>'
    credit = (f'<footer class="credit">Analysis by {_html.escape(author)}</footer>') if author else ""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Pilot profiles</title>
<style>*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#f5f5f7;color:#1d1d1f;margin:0;padding:34px 24px;-webkit-font-smoothing:antialiased}}
h1{{font-size:34px;font-weight:600;letter-spacing:-.022em;margin:0 0 6px}}
.lead{{max-width:780px;color:#3a3a3c;font-size:15px;line-height:1.55;margin:0 0 18px}}
.warn{{max-width:780px;background:#fff;border-left:4px solid #d08400;border-radius:10px;padding:12px 16px;margin:0 0 22px;font-size:14px;line-height:1.5;color:#3a3a3c;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.reflegend{{display:flex;gap:18px;align-items:center;flex-wrap:wrap;background:#fff;border-radius:18px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.06);max-width:760px;margin:0 0 24px}}
.reflegend .rt{{font-size:13.5px;color:#3a3a3c;line-height:1.5;flex:1 1 240px;min-width:220px}}
.controls{{display:flex;flex-direction:column;gap:8px;margin:0 0 26px;max-width:560px}}
.lbl{{color:#86868b;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
.pfilter{{display:flex;flex-direction:column;background:#fff;border:1px solid #e3e3e6;border-radius:12px;overflow:hidden;max-width:440px;max-height:340px;overflow-y:auto}}
.popt{{position:relative;display:flex;align-items:baseline;gap:12px;padding:10px 15px;cursor:pointer;border-top:1px solid #f1f1f3;-webkit-tap-highlight-color:transparent}}
.popt:first-child{{border-top:0}}
.popt input{{position:absolute;opacity:0;width:1px;height:1px;margin:0}}
.popt:has(input:checked){{background:#eef5ff}}
.popt:focus-within{{outline:2px solid #0071e3;outline-offset:-2px}}
.po-main{{font-size:14.5px;font-weight:600;color:#1d1d1f}}
.popt:has(input:checked) .po-main{{color:#0071e3}}
.po-meta{{font-size:12.5px;color:#86868b;margin-left:auto;text-align:right}}
.phead{{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:8px 0 4px}}
.ponote{{color:#86868b;font-size:13.5px;margin:0 0 8px}}
.pstrip{{display:flex;gap:14px;overflow-x:auto;padding:6px 2px 14px;scroll-snap-type:x proximity}}
.pcell{{flex:0 0 auto;width:240px;background:#fff;border-radius:16px;padding:10px 8px 6px;box-shadow:0 1px 3px rgba(0,0,0,.06);text-align:center;scroll-snap-align:start}}
.pavg{{background:#eef5ff;border:1px solid #d8e6f7}}
.pday{{font-size:12.5px;font-weight:600;color:#1d1d1f;margin-bottom:2px}}
.pspd{{font-size:12px;color:#6e6e73;margin-top:2px}}
.pradar svg{{max-width:100%;height:auto}}
.reflegend svg{{max-width:100%;height:auto}}
.credit{{max-width:1280px;margin:48px 0 10px;padding-top:18px;border-top:1px solid #ececef;color:#a1a1a6;font-size:13px}}
{pilot_css}
@media (max-width:760px){{body{{padding:22px 14px}}h1{{font-size:27px}}}}
</style></head><body>
<h1>Pilot profiles</h1>
<p class="lead">For each pilot, all day profiles in chronological order \u2013 making visible how a profile develops. Pick a pilot below.</p>
<div class="warn"><b>Important:</b> Every radar is <b>day-relative</b> \u2013 it compares the pilot only with the field of <b>the same day</b>. Shapes from different days are therefore only loosely comparable; a \u201cbetter\u201d day can also mean a weaker field. Read progress with caution.</div>
<div class="reflegend"><div class="legend-radar">{ref}</div><div class="rt">For orientation: the eight axes with their full names. The radars below carry the short form. At the front of each strip sits an <b>average</b> card with a spread band (\u00b1 1 standard error). Only pilots with at least three flights are shown.</div></div>
<div class="controls"><div class="lbl">Choose pilot</div><div class="pfilter" role="radiogroup" aria-label="Choose pilot">{sel_rows}</div></div>
{secs}
{credit}
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)

def _overview_block(tbl):
    """Inner HTML for the Piloten-Profile overview (average radar per pilot, ranked by mean
    spoke length). Rendered as a fold inside the main page."""
    pilots = _merge_pilots(tbl)
    items = []
    for name, rows in pilots.items():
        if len(rows) < 3:
            continue
        svg, M = _avg_radar_svg(rows)
        Mv = [m for m in M if np.isfinite(m)]
        metric = float(np.mean(Mv)) if Mv else 0.0
        items.append((metric, name, rows, svg))
    items.sort(key=lambda x: (-x[0], x[1].lower()))
    alldays = sorted({str(r["day"]) for _, r in tbl.iterrows()},
                     key=lambda d: (_parse_day(d) or datetime.date.min))
    if alldays:
        d0, d1 = _day_label(alldays[0]), _day_label(alldays[-1])
        period = d0 if d0 == d1 else f"{d0} \u2013 {d1}"
    else:
        period = "\u2013"
    cells = ""
    for rank, (metric, name, rows, svg) in enumerate(items, 1):
        cells += (f'<div class="ocell"><div class="orank">{rank}</div>'
                  f'<div class="oradar">{svg}</div>'
                  f'<div class="oname">{_html.escape(name)}</div>'
                  f'<div class="ometa">{len(rows)} flights \u00b7 \u00d8 {round(metric*100)}%</div></div>')
    if not items:
        cells = '<p class="ovlead">No pilot with at least three flights in the data yet.</p>'
    return (f'<p class="ovlead">For each pilot the mean profile over all analysed flights (spread band = \u00b1 1 standard error), '
            f'sorted by mean axis length \u2013 the most complete profile first.</p>'
            f'<div class="period">Period: <b>{period}</b> \u00b7 {len(alldays)} days \u00b7 {len(items)} pilots (3+ flights)</div>'
            f'<div class="warn"><b>Important:</b> All axes are <b>day-relative</b> \u2013 they compare each pilot with the field '
            f'of the same day. The order therefore says who stands roundest relative to the field on average, not absolute skill.</div>'
            f'<div class="ogrid">{cells}</div>')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("--group", choices=["subfolder", "date"], default="subfolder")
    ap.add_argument("--results", default=None, help="CSV of official results (default: results.csv in FOLDER)")
    ap.add_argument("--min-thermals", type=int, default=5)
    ap.add_argument("--min-per-day", type=int, default=1)
    ap.add_argument("--target", default="avg_speed_kmh", help="column to correlate parameters against")
    ap.add_argument("--target-sign", type=int, choices=[-1, 1], default=1)
    ap.add_argument("--wind-bias-check", action="store_true", help="diagnose whether strong thermals are poor wind tracers")
    ap.add_argument("--start-cut-min", type=float, default=10.0,
                    help="non-race days: begin analysis at (latest launch in the day + this many minutes); negative disables")
    ap.add_argument("--strong-climb", type=float, default=STRONG_CLIMB_MS,
                    help="m/s; a thermal whose mean climb is >= this counts as strong (splits the two core spokes)")
    ap.add_argument("--core-min-turns", type=float, default=CORE_MIN_TURNS,
                    help="min full turns a thermal needs to count for 'Finds the core' (default 5)")
    ap.add_argument("--core-min-gain", type=float, default=CORE_MIN_GAIN_M,
                    help="min height gain (m) a thermal needs to count for 'Finds the core' (default 20)")
    ap.add_argument("--public", action="store_true", help="(kept for compatibility; the single shared file is always written)")
    ap.add_argument("--author", default="J\u00f6rg Korner", help="small credit shown at the foot of the page (pass '' to omit)")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.folder, "**", "*.igc"), recursive=True) +
                   glob.glob(os.path.join(args.folder, "**", "*.IGC"), recursive=True))
    if not paths: print(f"No IGC files under {args.folder}"); return
    res_path = args.results or os.path.join(args.folder, "results.csv")
    results = read_results(res_path) if os.path.exists(res_path) else {}
    print(f"Found {len(paths)} IGC files. Results file: {'loaded '+str(len(results))+' entries' if results else 'none (using route proxy)'}")

    # ---- pass 0: per-day analysis-window start -------------------------------------------
    # Race day  -> UTC token in the folder name.  Non-race day -> latest launch + offset min.
    day_of = {p: ((read_header(p)["date"] or "?") if args.group == "date" else Path(p).parent.name) for p in paths}
    launch_sod = {}
    for p in paths:
        try: launch_sod[p] = float(_start_sod(p))
        except Exception: launch_sod[p] = np.nan
    day_cut = {}   # day -> (cut_sod or None, is_race)
    for day in sorted(set(day_of.values())):
        members = [p for p in paths if day_of[p] == day]
        tok = next((parse_cutoff_sod(Path(p).parent.name) for p in members
                    if parse_cutoff_sod(Path(p).parent.name) is not None), None)
        if tok is not None:
            day_cut[day] = (tok, True)
        else:
            lat = [launch_sod[p] for p in members if np.isfinite(launch_sod[p])]
            cut = (max(lat) + args.start_cut_min*60.0) if (lat and args.start_cut_min >= 0) else None
            day_cut[day] = (cut, False)
    for day, (cut, is_race) in sorted(day_cut.items()):
        if cut is not None:
            tag = " (race)" if is_race else f" (latest launch + {args.start_cut_min:g} min)"
            hh, mm = int(cut)//3600, (int(cut)%3600)//60
            print(f"  window {day}: start {hh:02d}:{mm:02d} UTC{tag}")

    recs, skipped, dbg = [], 0, []
    for p in paths:
        day = day_of[p]; cut_sod, is_race = day_cut.get(day, (None, False))
        try:
            meta = read_header(p); rec, df = flight_record(p, meta, cut_sod=cut_sod, strong_climb=args.strong_climb,
                                                            core_min_turns=args.core_min_turns, core_min_gain=args.core_min_gain)
            rec["day"] = day; rec["is_race"] = bool(is_race)
            rec["race_cut_sod"] = float(cut_sod) if (is_race and cut_sod is not None) else np.nan
            df["sod"] = _start_sod(p) + df["t"]
            recs.append(rec); dbg.append({"file": rec["file"], "name": rec["name"], "df": df, "day": rec["day"]})
        except Exception as e:
            skipped += 1; print(f"  skip {Path(p).name}: {e}")
    flights = pd.DataFrame(recs)
    if flights.empty: print("No usable flights."); return
    flights = flights[flights["n_big"] >= args.min_thermals].copy()
    # ---- drop physically impossible flights (altitude-data glitches) ----
    bad = ((flights["peak_climb"] > MAX_PLAUS_CLIMB) | (flights["mean_climb"] > MAX_PLAUS_MEAN_CLIMB) |
           (flights["mean_climb"] <= 0) | (flights["coring_gain"] > MAX_PLAUS_CLIMB) |
           (flights["abandon_climb"] < MIN_PLAUS_ABANDON) | (flights["abandon_climb"] > MAX_PLAUS_CLIMB))
    if bad.any():
        print(f"Dropped {int(bad.sum())} flight(s) with implausible data (likely altitude glitches):")
        for _, r in flights[bad].iterrows():
            who = r.get("name") if (isinstance(r.get("name"), str) and r.get("name").strip()) else r["file"]
            print(f"  {str(who)[:24]:24s} {r['day']}  peak={r['peak_climb']:.1f} mean={r['mean_climb']:.1f} "
                  f"core+={r['coring_gain']:.1f} abandon={r['abandon_climb']:.1f}")
        flights = flights[~bad].copy()
    if flights.empty: print("Nothing left after plausibility filter."); return
    counts = flights["day"].value_counts(); flights = flights[flights["day"].isin(counts[counts >= args.min_per_day].index)].copy()
    if flights.empty: print("Nothing meets thresholds."); return

    # ---- wind-corrected glide airspeed (optional AGL via terrain.py + dem/riederalp.tif) ----
    terr = None
    try:
        if os.path.exists(os.path.join("dem", "riederalp.tif")):
            from terrain import Terrain; terr = Terrain()
            print("terrain DEM loaded - wind field uses height above ground")
    except Exception as e:
        print(f"terrain not used ({e}) - wind field uses MSL altitude")
    flights = add_glide_airspeed(flights, dbg, terr)
    if args.wind_bias_check:
        try: wind_strength_bias(dbg, terr)
        except Exception as e: print(f"wind bias check failed ({e})")
    gc = flights["glide_airspeed"].notna().mean()
    print(f"glide airspeed computed for {int(flights['glide_airspeed'].notna().sum())}/{len(flights)} flights "
          f"(median coverage {int(flights['glide_cov'].median())}% of glide time)")
    try:
        daycov = flights.groupby("day")["glide_cov"].mean().to_dict()
        wind_html = build_wind_section(dbg, terr, daycov)
    except Exception as e:
        print(f"wind section skipped ({e})"); wind_html = ""

    # discipline (free/flat/FAI) by XContest-style weighted score, + official vs approximate
    FACT = {"FAI": 1.4, "flat": 1.2, "free": 1.0}
    km, src, spd, rtype, pts = [], [], [], [], []
    for _, r in flights.iterrows():
        cand = {"FAI": r["fai_tri_km"], "flat": r["flat_tri_km"], "free": r["free_km"]}
        disc = max(cand, key=lambda k: (cand[k]*FACT[k] if np.isfinite(cand[k]) else -1))
        hit = match_result(results, r["file"], r["name"])
        if hit:
            okm, osp, oty, opt = hit
            ty = oty if oty in FACT else disc
            km.append(okm); src.append("XC"); rtype.append(ty)
            pts.append(round(opt, 1) if opt is not None else round(okm*FACT[ty], 1))
            spd.append(osp if osp is not None else (okm/r["flight_h"] if r["flight_h"] > 0 else np.nan))
        else:
            best = cand[disc]
            km.append(best); src.append("approx"); rtype.append(disc)
            pts.append(round(best*FACT[disc], 1))
            spd.append(best/r["flight_h"] if r["flight_h"] > 0 else np.nan)
    flights["result_km"] = np.round(km).astype(int)
    for c in ["fai_tri_km", "flat_tri_km", "free_km"]:
        flights[c] = np.round(flights[c]).astype(int)
    flights["result_src"] = src; flights["result_type"] = rtype
    flights["type_disp"] = [t + ("~" if s == "approx" else "") for t, s in zip(rtype, src)]
    flights["xc_points"] = pts; flights["avg_speed_kmh"] = np.round(spd, 1)
    flights["pilot"] = [n if (isinstance(n, str) and n.strip() and n.strip().lower() not in ("none", "nan")) else f
                        for n, f in zip(flights["name"], flights["file"])]
    flights = add_richtung(flights, dbg)                  # Hohe Richtungseffizienz (replaces Baustelle)
    flights["flies_long"] = flights["result_km"].astype(float)   # Flies far (radar only, not a speed lever)
    flights["turns_per_km"] = flights.apply(
        lambda r: round(r["total_turns"]/r["result_km"], 2) if r.get("result_km", 0) > 0 and np.isfinite(r.get("total_turns", np.nan)) else np.nan, axis=1)

    # within-day percentiles -> signatures / radar
    for c in ["big_per_h","abandon_climb","mean_climb","peak_climb",
              "coring_gain","core_strong","core_weak","circling_pct","climb_straight_pct",
              "glide_airspeed","flow_pct","richtung_pct","flies_long","tailwind_use","glide_ratio","turns_per_km"]:
        if c in flights.columns:
            flights[c+"_p"] = flights.groupby("day")[c].transform(lambda s: s.rank(pct=True))
    # 'Flies far' carries no information in a race (the task distance is fixed), so suppress
    # that spoke on race flights; it then also drops out of a pilot's average automatically.
    if "is_race" in flights.columns and "flies_long_p" in flights.columns:
        flights.loc[flights["is_race"] == True, "flies_long_p"] = np.nan
    flights["signature"] = flights.apply(signature, axis=1)

    # ---- launch site: header value if present, else nearest labelled take-off within radius
    SITE_RADIUS_KM = 3.0
    known = [(nm, la, lo) for nm, la, lo in
             flights.loc[flights["site_hdr"].notna(), ["site_hdr", "launch_lat", "launch_lon"]].itertuples(index=False, name=None)]
    def _launch(r):
        if isinstance(r["site_hdr"], str) and r["site_hdr"].strip(): return r["site_hdr"]
        best, bd = "", SITE_RADIUS_KM*1000
        for nm, la, lo in known:
            d = haversine_m(r["launch_lat"], r["launch_lon"], la, lo)
            if d < bd: bd, best = d, nm
        return best
    flights["launch"] = flights.apply(_launch, axis=1)

    flights.sort_values(["day", "result_km"], ascending=[True, False]).to_csv("schnell-fliegen.csv", index=False)
    n_proxy = int((flights["result_src"] == "approx").sum())
    foot = ("result km = official figure where matched; a '~' after the type means our approximate "
            f"distance instead ({n_proxy} flights). Full FAI/flat/free distances are in the CSV.")
    ev_n, ev_adj = explained_variance(flights)
    if ev_adj is None:
        banner = ""
    else:
        ex = round(ev_adj*100); gap = 100-ex
        banner = (f"Across {ev_n} flights on {flights['day'].nunique()} days, the five levers tracked here explain about "
                  f"{ex}% of the differences in average cross-country speed between pilots on a given day. The other "
                  f"~{gap}% comes from things outside a pilot's direct control or not captured here - thermal strength and selection, "
                  f"line and route choice, how well glides exploit lift and sink, conditions and luck. More flights sharpen this estimate but do not shrink the gap.")
        print("\n" + banner)
    _sorted = flights.sort_values("result_km", ascending=False)
    _sh = flights.attrs.get("speeder_hist", {}); _sr = (flights.attrs.get("speeder_lo"), flights.attrs.get("speeder_hi"))
    open("METHODS.md", "w", encoding="utf-8").write(METHODS_MD.lstrip("\n").rstrip() + "\n")
    write_html(_sorted, "schnell-fliegen.html", foot, banner, wind_html, speeder_hist=_sh, speeder_range=_sr, author=args.author)
    write_pilots_html(_sorted, "schnell-fliegen-piloten.html", author=args.author)

    corr = correlate(flights, args.target, args.target_sign)
    tlabel = NICE.get(args.target, args.target)
    days = sorted(flights["day"].astype(str).unique())
    with PdfPages("schnell-fliegen.pdf") as pdf:
        for day in days:
            sub = flights[flights["day"].astype(str) == day]
            draw_table(pdf, sub, f"Day {day}  \u2014  result & parameters ({len(sub)} pilots)")
            draw_signatures(pdf, sub, f"Day {day}  \u2014  per-pilot signatures")
        if len(days) > 1 and not corr.empty:
            draw_corr(pdf, corr, tlabel, flights["day"].nunique())
        draw_glossary(pdf)

    print(f"Usable {len(flights)} flights over {flights['day'].nunique()} day(s); "
          f"{n_proxy} on approximate distance, {len(flights)-n_proxy} official.")
    for day in days:
        sub = flights[flights["day"].astype(str) == day].sort_values("result_km", ascending=False)
        print(f"\n=== {day}  ({len(sub)} pilots) ===")
        for _, r in sub.head(8).iterrows():
            print(f"  {r['result_km']:4d} km {r['result_type']:>5}  {r['xc_points']:6.1f} pts  "
                  f"{r['avg_speed_kmh']:5.1f} km/h  {str(r['pilot'])[:22]:22s} | {r['signature']}")
        if len(sub) > 8: print(f"  ... and {len(sub)-8} more (see table)")
    print("\nWrote schnell-fliegen.html, schnell-fliegen-piloten.html, schnell-fliegen.csv, schnell-fliegen.pdf, METHODS.md")

if __name__ == "__main__":
    main()
