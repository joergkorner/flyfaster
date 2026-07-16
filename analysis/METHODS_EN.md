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
