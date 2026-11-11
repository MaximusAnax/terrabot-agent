#!/usr/bin/env python3
"""Grow Period A metrics and charts for the TerraBot write-up (standard library only).

Input is the team's 30-minute readings workbook. A telemetry process on the
robot averaged the agent's 10-second log rows into one row per half hour, and
a daily sync folded each day into one sheet. The workbook stays private with
the rest of the course work; this script is here so every number in the
README can be traced to how it was computed.

    python3 scripts/grow_a_metrics.py GROW_A.xlsx [INTERIM.xlsx] [--svg-dir assets]

Columns used per half-hour row: time, log_min (minutes the agent was logging),
temp, humid0, humid1, smoist, smoist0, smoist1, light, level, led_on_min,
fan_on_min, pump_s, photos.

Method notes:
- Shares of time are weighted by log_min, so a half hour with the agent down
  counts for nothing and a partial half hour counts partly.
- Values are half-hour means, which smooths short spikes. A share computed
  from the raw 10 s rows could differ by a little.
- Limits are half-open [lo, hi) and optimal bands are closed [lo, hi], the
  convention the course's behaviors and tester use.
"""
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Ranges in force during Grow A (course limits file; moisture lower limit
# raised from 500 to 525 at deployment because the aging sensors read high).
LIMITS = {"temp": (22, 29), "humid1": (60, 90), "smoist": (525, 650)}
OPTIMAL = {"temp": (25, 27), "humid1": (70, 80), "smoist": (550, 600)}
INSOLATION_TARGET = 8500   # light-sensor units x hours per day
TANK_STOP = 30             # watering is skipped below this reservoir level
LIGHTS_ON = (8, 22)        # LEDs allowed 08:00-22:00


# --------------------------------------------------------------- reading xlsx
def read_book(path):
    """Return {sheet name: list of row dicts} from an .xlsx, stdlib only."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    book = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).iter(NS + "sheet"):
        target = rels[s.get(RNS + "id")].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        grid = []
        for row in ET.fromstring(z.read(target)).iter(NS + "row"):
            cells = {}
            for c in row.findall(NS + "c"):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r")).group(0):
                    col = col * 26 + ord(ch) - 64
                if c.get("t") == "inlineStr":
                    v = "".join(t.text or "" for t in c.iter(NS + "t"))
                else:
                    ve = c.find(NS + "v")
                    v = ve.text if ve is not None else ""
                    if c.get("t") == "s":
                        v = shared[int(v)]
                cells[col - 1] = v
            grid.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        header = grid[0]
        book[s.get("name")] = [dict(zip(header, r)) for r in grid[1:] if r]
    return book


def num(cell):
    """Cell text to float; blank cells (agent not logging) become None."""
    cell = (cell or "").strip()
    return float(cell) if re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", cell) else None


def half_hours(book):
    """All day sheets as one time-ordered list of half-hour rows."""
    rows = []
    for name, sheet in book.items():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            continue
        for r in sheet:
            d = {k: num(v) for k, v in r.items() if k != "time"}
            d["t"] = datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
            d["log_min"] = d.get("log_min") or 0.0
            rows.append(d)
    rows.sort(key=lambda r: r["t"])
    return rows


# --------------------------------------------------------------------- metrics
def share(rows, key, lo, hi, closed):
    """Share of logged time with lo <= value < hi (or <= hi when closed)."""
    tot = inside = 0.0
    for r in rows:
        v, w = r.get(key), r["log_min"]
        if v is None or w <= 0:
            continue
        tot += w
        if lo <= v and (v <= hi if closed else v < hi):
            inside += w
    return inside / tot if tot else None


def is_night(t):
    return not (LIGHTS_ON[0] <= t.hour < LIGHTS_ON[1])


def metrics(rows):
    logged = [r for r in rows if r["log_min"] > 0]
    start = logged[0]["t"]
    rows = [r for r in rows if r["t"] >= start]
    end = rows[-1]["t"] + timedelta(minutes=30)
    m = {"start": start, "end": end,
         "days": (end - start).total_seconds() / 86400,
         "uptime": sum(r["log_min"] for r in rows) / (30 * len(rows)),
         "gaps": [(r["t"], 30 - r["log_min"]) for r in rows[1:] if r["log_min"] < 29]}

    for key in LIMITS:
        m["in_limits_" + key] = share(rows, key, *LIMITS[key], closed=False)
        m["in_optimal_" + key] = share(rows, key, *OPTIMAL[key], closed=True)

    # Where the cold time falls: below the temperature limit at night (lights off)
    cold = [r for r in rows if r.get("temp") is not None and r["log_min"] > 0
            and r["temp"] < LIMITS["temp"][0]]
    cold_w = sum(r["log_min"] for r in cold)
    m["temp_below_share"] = cold_w / sum(r["log_min"] for r in rows if r.get("temp") is not None)
    m["temp_below_at_night"] = (sum(r["log_min"] for r in cold if is_night(r["t"])) / cold_w
                                if cold_w else None)
    m["temp_min"] = min(r["temp"] for r in rows if r.get("temp") is not None)
    m["temp_max"] = max(r["temp"] for r in rows if r.get("temp") is not None)
    m["humid1_max"] = max(r["humid1"] for r in rows if r.get("humid1") is not None)

    # Which side soil moisture left its limits on, and when
    sm = [r for r in rows if r.get("smoist") is not None and r["log_min"] > 0]
    sm_w = sum(r["log_min"] for r in sm)
    wet = [r for r in sm if r["smoist"] >= LIMITS["smoist"][1]]
    m["smoist_above_share"] = sum(r["log_min"] for r in wet) / sm_w
    m["smoist_above_at_night"] = (sum(r["log_min"] for r in wet if is_night(r["t"]))
                                  / sum(r["log_min"] for r in wet)) if wet else None
    m["smoist_below_share"] = sum(r["log_min"] for r in sm if r["smoist"] < LIMITS["smoist"][0]) / sm_w

    # Light reading in half hours with the LEDs at full power, per day
    full_led = {}
    for r in rows:
        if (r.get("led") or 0) >= 254 and (r.get("led_on_min") or 0) >= 29 and r.get("light") is not None:
            full_led.setdefault(r["t"].date(), []).append(r["light"])
    m["light_at_full_led"] = {d: sum(v) / len(v) for d, v in full_led.items()}

    # Redundant-sensor cross-checks
    m["humid0_stuck_share"] = share(rows, "humid0", 95, 1e9, closed=False)
    for k in ("smoist0", "smoist1"):
        on = [r[k] for r in rows if r.get(k) is not None and (r.get("led_on_min") or 0) >= 29]
        off = [r[k] for r in rows if r.get(k) is not None and r["log_min"] > 0
               and (r.get("led_on_min") or 0) == 0]
        m[k + "_led_on_mean"] = sum(on) / len(on)
        m[k + "_led_off_mean"] = sum(off) / len(off)

    # Actuators and resources, per calendar day
    days = {}
    for r in rows:
        d = days.setdefault(r["t"].date(), {"pump_s": 0.0, "fan_min": 0.0, "led_h": 0.0,
                                            "photos": 0, "insolation": 0.0, "logged_min": 0.0,
                                            "level_min": None, "smoist_min": None, "smoist_max": None,
                                            "weight_sum": 0.0, "weight_min": 0.0})
        d["pump_s"] += r.get("pump_s") or 0
        d["fan_min"] += r.get("fan_on_min") or 0
        d["led_h"] += (r.get("led_on_min") or 0) / 60
        d["photos"] += int(r.get("photos") or 0)
        d["logged_min"] += r["log_min"]
        if r.get("light") is not None:
            d["insolation"] += r["light"] * r["log_min"] / 60
        if r.get("level") is not None:
            d["level_min"] = r["level"] if d["level_min"] is None else min(d["level_min"], r["level"])
        if r.get("smoist") is not None and r["log_min"] > 0:
            d["smoist_min"] = r["smoist"] if d["smoist_min"] is None else min(d["smoist_min"], r["smoist"])
            d["smoist_max"] = r["smoist"] if d["smoist_max"] is None else max(d["smoist_max"], r["smoist"])
        if r.get("weight") is not None and r["log_min"] > 0:
            d["weight_sum"] += r["weight"] * r["log_min"]
            d["weight_min"] += r["log_min"]
    for d in days.values():
        d["weight_mean"] = d["weight_sum"] / d["weight_min"] if d["weight_min"] else float("nan")
    m["daily"] = days
    full = [d for d in days.values() if d["logged_min"] >= 23 * 60]
    m["full_days"] = len(full)
    m["insolation_mean"] = sum(d["insolation"] for d in full) / len(full)
    m["insolation_days_at_target"] = sum(d["insolation"] >= 0.95 * INSOLATION_TARGET for d in full)
    m["led_h_mean"] = sum(d["led_h"] for d in full) / len(full)
    m["pump_s_total"] = sum(d["pump_s"] for d in days.values())
    m["pump_days"] = sum(d["pump_s"] > 0 for d in days.values())
    m["photos_total"] = sum(d["photos"] for d in days.values())
    m["photo_free_days"] = [k for k, d in days.items() if d["photos"] == 0]
    m["fan_min_total"] = sum(d["fan_min"] for d in days.values())

    # Fan use before and after the agent stopped trusting humidity sensor 0
    switch = datetime(2026, 10, 1, 12, 30)
    m["fan_min_before_switch"] = sum((r.get("fan_on_min") or 0) for r in rows if r["t"] < switch)
    m["fan_min_after_switch"] = sum((r.get("fan_on_min") or 0) for r in rows if r["t"] >= switch)
    m["hours_before_switch"] = (switch - start).total_seconds() / 3600

    # Reservoir: low-water stop and refill
    lv = [(r["t"], r["level"]) for r in rows if r.get("level") is not None]
    m["level_first"], m["level_low"] = lv[0][1], min(v for _, v in lv)
    m["first_below_stop"] = next(t for t, v in lv if v < TANK_STOP)
    jumps = [(t1, v0, v1) for (t0, v0), (t1, v1) in zip(lv, lv[1:]) if v1 - v0 > 15]
    m["refills"] = jumps
    m["dry_stop_days"] = [k for k, d in days.items()
                          if d["pump_s"] == 0 and d["level_min"] is not None
                          and d["level_min"] < TANK_STOP]
    return m, rows


# ------------------------------------------------------------------------ SVG
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE, BLUE_BAND, LIMIT_BAND = "#2a78d6", "#cde2fb", "#efeeea"
GRAY_LINE = "#a8a69f"
FONT = 'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=12, fill=INK2, anchor="start", weight="normal"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}" {FONT}>{esc(s)}</text>')


class Panel:
    """One small-multiple panel: shared time axis, its own y scale."""

    def __init__(self, x0, y0, w, h, t0, t1, ylo, yhi):
        self.x0, self.y0, self.w, self.h = x0, y0, w, h
        self.t0, self.span = t0, (t1 - t0).total_seconds()
        self.ylo, self.yhi = ylo, yhi

    def x(self, t):
        return self.x0 + self.w * (t - self.t0).total_seconds() / self.span

    def y(self, v):
        v = max(self.ylo, min(self.yhi, v))
        return self.y0 + self.h * (1 - (v - self.ylo) / (self.yhi - self.ylo))

    def band(self, lo, hi, fill):
        y1, y2 = self.y(hi), self.y(lo)
        return f'<rect x="{self.x0}" y="{y1:.1f}" width="{self.w}" height="{y2 - y1:.1f}" fill="{fill}"/>'

    def yaxis(self, ticks, fmt="{:g}"):
        out = []
        for v in ticks:
            yy = self.y(v)
            out.append(f'<line x1="{self.x0}" x2="{self.x0 + self.w}" y1="{yy:.1f}" y2="{yy:.1f}" '
                       f'stroke="{GRID}" stroke-width="1"/>')
            out.append(text(self.x0 - 8, yy + 4, fmt.format(v), 11, MUTED, "end"))
        return out

    def xaxis(self, days, labels=True):
        out = [f'<line x1="{self.x0}" x2="{self.x0 + self.w}" y1="{self.y0 + self.h}" '
               f'y2="{self.y0 + self.h}" stroke="{AXIS}" stroke-width="1"/>']
        for d in days:
            xx = self.x(d)
            out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{self.y0 + self.h}" '
                       f'y2="{self.y0 + self.h + 4}" stroke="{AXIS}"/>')
            if labels:
                out.append(text(xx, self.y0 + self.h + 17, f"{d.month}/{d.day}", 11, MUTED, "middle"))
        return out

    def line(self, pts, color, width=1.5, dash=None):
        """Polyline that breaks wherever the agent was not logging."""
        out, seg = [], []
        for t, v in pts:
            if v is None:
                if len(seg) > 1:
                    out.append(seg)
                seg = []
                continue
            seg.append(f"{self.x(t):.1f},{self.y(v):.1f}")
        if len(seg) > 1:
            out.append(seg)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        return [f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" '
                f'stroke-width="{width}" stroke-linejoin="round"{d}/>' for s in out]


def svg_doc(w, h, body, title, desc):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>'
            f'<rect width="{w}" height="{h}" fill="#ffffff"/>' + "".join(body) + "</svg>\n")


def day_ticks(t0, t1, step=2):
    d = datetime(t0.year, t0.month, t0.day) + timedelta(days=1)
    out = []
    while d <= t1:
        if (d - datetime(2026, 10, 1)).days % step == 0:
            out.append(d)
        d += timedelta(days=1)
    return out


def chart_environment(rows, m, path):
    W, H = 960, 640
    x0, pw = 70, 740
    t0, t1 = m["start"], m["end"]
    ticks = day_ticks(t0, t1)
    body = [text(x0 - 60, 28, "Grow Period A: the greenhouse as the agent saw it", 16, INK, weight="600"),
            text(x0 - 60, 48, "Half-hour means, 9/30 20:30 to 10/14 24:00. Gray band: limits (behaviors "
                 "act outside it). Blue band: optimal (they stop inside it).", 12, INK2)]
    specs = [
        ("Temperature (°C)", "temp", None, 20, 30, [20, 22, 24, 26, 28, 30]),
        ("Relative humidity (%)", "humid1", "humid0", 50, 100, [50, 60, 70, 80, 90, 100]),
        ("Soil moisture (sensor units, mean of 2 sensors)", "smoist", None, 450, 700,
         [450, 500, 550, 600, 650, 700]),
    ]
    ph, gap, top = 138, 52, 92
    for i, (label, key, ghost, lo, hi, yt) in enumerate(specs):
        p = Panel(x0, top + i * (ph + gap), pw, ph, t0, t1, lo, hi)
        body.append(text(x0, p.y0 - 10, label, 13, INK, weight="600"))
        body.append(p.band(*LIMITS[key], LIMIT_BAND))
        body.append(p.band(*OPTIMAL[key], BLUE_BAND))
        body += p.yaxis(yt)
        body += p.xaxis(ticks, labels=(i == len(specs) - 1))
        if ghost:
            body += p.line([(r["t"], r.get(ghost) if r["log_min"] > 0 else None) for r in rows],
                           GRAY_LINE, 1.25)
        body += p.line([(r["t"], r.get(key) if r["log_min"] > 0 else None) for r in rows], BLUE, 1.5)
        lx = x0 + pw + 12
        # Label the limits band on whichever side of the optimal band is free
        side = (OPTIMAL[key][1] + LIMITS[key][1]) / 2 if ghost else (LIMITS[key][0] + OPTIMAL[key][0]) / 2
        body.append(text(lx, p.y(side) + 4, "limits", 11, MUTED))
        body.append(text(lx, p.y(sum(OPTIMAL[key]) / 2) + 4, "optimal", 11, INK2))
        if ghost:
            body.append(text(lx, p.y(99) + 4, "sensor 0: stuck", 11, MUTED))
            body.append(text(lx, p.y(99) + 18, "near 99, excluded", 11, MUTED))
            body.append(text(lx, p.y(64) + 4, "sensor 1: used", 11, INK2))
        if key == "temp":
            body.append(text(lx, p.y(21) + 4, "cold nights,", 11, INK2))
            body.append(text(lx, p.y(21) + 18, "LEDs off 22-08", 11, INK2))
    open(path, "w").write(svg_doc(W, H, body, "Grow Period A environment",
                                  "Temperature, humidity and soil moisture over Grow Period A "
                                  "with limit and optimal bands."))


def chart_water(rows, m, path):
    W, H = 960, 470
    x0, pw = 70, 740
    t0, t1 = m["start"], m["end"]
    ticks = day_ticks(t0, t1)
    body = [text(x0 - 60, 28, "Water: the reservoir ran low, the agent stopped pumping, a refill resumed it",
                 16, INK, weight="600"),
            text(x0 - 60, 48, "Top: reservoir level (half-hour means, ultrasonic sensor). "
                 "Bottom: pump run time per day.", 12, INK2)]
    p = Panel(x0, 92, pw, 170, t0, t1, 20, 70)
    body.append(text(x0, p.y0 - 10, "Reservoir level (sensor units)", 13, INK, weight="600"))
    stop = [d for d in m["dry_stop_days"]]
    if stop:
        xa = p.x(datetime.combine(min(stop), datetime.min.time()))
        xb = p.x(datetime.combine(max(stop), datetime.min.time()) + timedelta(days=1))
        body.append(f'<rect x="{xa:.1f}" y="{p.y0}" width="{xb - xa:.1f}" height="{p.h}" fill="{LIMIT_BAND}"/>')
        body.append(text((xa + xb) / 2, p.y0 + 16, "no watering: level below 30", 11, INK2, "middle"))
    body += p.yaxis([20, 30, 40, 50, 60, 70])
    body += p.xaxis(ticks, labels=False)
    ys = p.y(TANK_STOP)
    body.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{ys:.1f}" y2="{ys:.1f}" stroke="{INK2}" '
                f'stroke-width="1" stroke-dasharray="5 4"/>')
    body.append(text(x0 + pw + 12, ys + 4, "stop level (30)", 11, INK2))
    body += p.line([(r["t"], r.get("level") if r["log_min"] > 0 else None) for r in rows], BLUE, 2)
    for t, v0, v1 in m["refills"]:
        body.append(text(p.x(t) + 8, p.y(43), f"refill {t.month}/{t.day}", 11, INK2))

    q = Panel(x0, 330, pw, 90, t0, t1, 0, 60)
    body.append(text(x0, q.y0 - 10, "Pump run time per day (s)", 13, INK, weight="600"))
    body += q.yaxis([0, 30, 60])
    body += q.xaxis(ticks)
    for day, d in sorted(m["daily"].items()):
        ds = datetime.combine(day, datetime.min.time())
        xa, xb = q.x(max(ds, t0)), q.x(min(ds + timedelta(days=1), t1))
        if d["pump_s"] <= 0:
            continue
        bw = min(18, xb - xa - 4)
        xc = (xa + xb) / 2
        yt = q.y(d["pump_s"])
        body.append(f'<path d="M{xc - bw / 2:.1f},{q.y0 + q.h:.1f} V{yt + 3:.1f} '
                    f'Q{xc - bw / 2:.1f},{yt:.1f} {xc - bw / 2 + 3:.1f},{yt:.1f} '
                    f'H{xc + bw / 2 - 3:.1f} Q{xc + bw / 2:.1f},{yt:.1f} {xc + bw / 2:.1f},{yt + 3:.1f} '
                    f'V{q.y0 + q.h:.1f} Z" fill="{BLUE}"/>')
    open(path, "w").write(svg_doc(W, H, body, "Grow Period A water",
                                  "Reservoir level and daily pump time; watering paused while "
                                  "the level was below 30, until a refill on 10/12."))


# ---------------------------------------------------------------------- report
def pct(x):
    return f"{100 * x:.1f}%"


def report(name, m):
    print(f"== {name}")
    print(f"window             {m['start']:%m/%d %H:%M} -> {m['end']:%m/%d %H:%M} "
          f"({m['days']:.2f} days, {m['full_days']} full days)")
    print(f"agent logging      {pct(m['uptime'])} of the window; gaps: "
          + ", ".join(f"{t:%m/%d %H:%M} ({g:.0f} min)" for t, g in m["gaps"]))
    for k in LIMITS:
        print(f"{k:<18} in limits {pct(m['in_limits_' + k]):>6}   in optimal {pct(m['in_optimal_' + k]):>6}")
    print(f"temp range         {m['temp_min']:.1f} to {m['temp_max']:.1f} C; below 22 C "
          f"{pct(m['temp_below_share'])} of the time, {pct(m['temp_below_at_night'] or 0)} of that "
          f"with the LEDs scheduled off")
    print(f"humid1 max         {m['humid1_max']:.1f}%   humid0 >= 95%: {pct(m['humid0_stuck_share'])} of the time")
    print(f"soil moisture      above limit {pct(m['smoist_above_share'])} of the time "
          f"({pct(m['smoist_above_at_night'] or 0)} of that at night), below {pct(m['smoist_below_share'])}")
    if m["hours_before_switch"] > 0:
        print(f"fan                {m['fan_min_before_switch']:.1f} min in the first "
              f"{m['hours_before_switch']:.0f} h (both humidity sensors), "
              f"{m['fan_min_after_switch']:.1f} min after (sensor 1 only)")
    else:
        print(f"fan                {m['fan_min_total']:.1f} min in total")
    print("light, LEDs full   " + ", ".join(f"{d:%m/%d} {v:.0f}" for d, v in sorted(m["light_at_full_led"].items())))
    print(f"smoist0 LEDs on/off {m['smoist0_led_on_mean']:.0f} / {m['smoist0_led_off_mean']:.0f}; "
          f"smoist1 {m['smoist1_led_on_mean']:.0f} / {m['smoist1_led_off_mean']:.0f}")
    print(f"light              {m['led_h_mean']:.1f} LED h/day; insolation mean "
          f"{m['insolation_mean']:.0f} vs target {INSOLATION_TARGET}; "
          f"{m['insolation_days_at_target']}/{m['full_days']} full days within 5% of target")
    print(f"water              pump {m['pump_s_total']:.0f} s total on {m['pump_days']} days; "
          f"level {m['level_first']:.0f} -> low {m['level_low']:.0f}; first below {TANK_STOP} "
          f"{m['first_below_stop']:%m/%d %H:%M}; no-watering days below stop: "
          + ", ".join(f"{d:%m/%d}" for d in m["dry_stop_days"])
          + "; refills: " + ", ".join(f"{t:%m/%d %H:%M} {a:.0f}->{b:.0f}" for t, a, b in m["refills"]))
    print(f"photos             {m['photos_total']} total; days with none: "
          + ", ".join(f"{d:%m/%d}" for d in m["photo_free_days"]))
    print("daily              date   logged_h  LED_h  fan_min  pump_s  photos  insolation  level_min"
          "  smoist_min-max  weight_mean_g")
    nan = float("nan")
    for d, v in sorted(m["daily"].items()):
        print(f"                   {d:%m/%d}  {v['logged_min'] / 60:7.1f}  {v['led_h']:5.1f}  "
              f"{v['fan_min']:7.1f}  {v['pump_s']:6.0f}  {v['photos']:6d}  {v['insolation']:10.0f}  "
              f"{v['level_min'] if v['level_min'] is not None else nan:9.1f}  "
              f"{v['smoist_min'] if v['smoist_min'] is not None else nan:6.1f}-"
              f"{v['smoist_max'] if v['smoist_max'] is not None else nan:6.1f}  {v['weight_mean']:13.0f}")


def main(argv):
    svg_dir = None
    if "--svg-dir" in argv:
        i = argv.index("--svg-dir")
        svg_dir = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        sys.exit(__doc__)
    rows = half_hours(read_book(argv[0]))
    m, rows = metrics(rows)
    report("Grow Period A", m)
    # Cross-check against the team's own analysis window (10/1-10/14)
    sub = [r for r in rows if r["t"] >= datetime(2026, 10, 1)]
    print(f"   (10/1-10/14 only: temp in limits {pct(share(sub, 'temp', 22, 29, False))}, "
          f"soil moisture in limits {pct(share(sub, 'smoist', 525, 650, False))})")
    if svg_dir:
        chart_environment(rows, m, f"{svg_dir}/grow_a_environment.svg")
        chart_water(rows, m, f"{svg_dir}/grow_a_water.svg")
    if len(argv) > 1:
        m2, _ = metrics(half_hours(read_book(argv[1])))
        report("After Grow A: same agent, unchanged", m2)


if __name__ == "__main__":
    main(sys.argv[1:])
