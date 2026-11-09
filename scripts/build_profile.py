"""Build the SVG cards for the GitHub profile README.

Every card comes in a light and a dark version and embeds a subset of
Bricolage Grotesque, so it renders in that font on GitHub.

    python scripts/build_profile.py --out dist                  # live data (needs GITHUB_TOKEN)
    python scripts/build_profile.py --out dist --sample         # offline preview with made-up numbers
    python scripts/build_profile.py --out dist --previous prev.json

If a fetch fails, the last good values from --previous are used, so a
GitHub or PyPI hiccup never blanks a card.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import random
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "BricolageGrotesque.woff2"
OPA = ROOT / "assets" / "opa"

# ---------------------------------------------------------------- settings

USER = "DivyanshuSingh96"

MACHYON = {
    "repo": "machyon",
    "pypi": "machyon",
    "tagline": "Every image. Mach speed. Zero lost.",
    "about": (
        "Async image downloader for Python. It tunes its own speed, checks that every "
        "file really is an image, and records why anything couldn't be downloaded."
    ),
}

QUANTUROS = {
    "name": "QUANTUROS",
    "long": "Quantum-based Unified Architecture for Next-gen Transport Using Resilient Asynchronous Security",
    "about": "A hybrid post-quantum cryptographic protocol for electric connected vehicles: ECDH combined with post-quantum primitives.",
    "before_ms": 120,
    "after_ms": 2,
    "enc": "257 MB/s",
    "dec": "~700 MB/s",
}

# Rows in the project list. "about" overrides the GitHub description; leave it
# out to use the repository's own description.
PROJECTS = [
    {"repo": "AiMi-Anime-RAG-System", "title": "AiMi",
     "about": "Anime search and recommendations built on retrieval, served from a CPU."},
    {"repo": "AnimeX", "title": "AnimeX"},
    {"repo": "stylizers", "title": "stylizers", "pypi": "stylizers",
     "about": "Turns a name into a styled name. My first PyPI library, written in school."},
]

# Notebook outputs (embedded images) inflate byte counts, so they're left out.
EXCLUDE_LANGUAGES = {"Jupyter Notebook"}

W = 840  # card width; GitHub scales it to the column

THEMES = {
    "light": {
        "bg": "#F6F8FC", "rule": "#D5DEF2", "dot": "#C3CEEA", "text": "#18224F",
        "muted": "#56628F", "accent": "#3B54F0", "signal": "#D93A60",
        "ramp": ["#C3CEEA", "#A9B6F5", "#7C8EF5", "#566EF3", "#3B54F0"],
        "code": "#E9EEF9",
    },
    "dark": {
        "bg": "#0C1633", "rule": "#1E2C5C", "dot": "#26366B", "text": "#E8EDFF",
        "muted": "#9AA7D6", "accent": "#7D8FFF", "signal": "#FF5C82",
        "ramp": ["#26366B", "#33479A", "#4A60D0", "#6B80F6", "#9AA8FF"],
        "code": "#15214A",
    },
}

# ---------------------------------------------------------------- data


def _get(url: str, token: str | None = None, body: dict | None = None, timeout: int = 20):
    headers = {"User-Agent": f"{USER}-profile-builder", "Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


GRAPHQL = """
query($login: String!) {
  user(login: $login) {
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      totalCount
      nodes {
        name description stargazerCount forkCount pushedAt
        primaryLanguage { name }
        latestRelease { tagName }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
      }
    }
    contributionsCollection {
      totalCommitContributions totalPullRequestContributions
      totalIssueContributions totalPullRequestReviewContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount contributionLevel } }
      }
    }
  }
}
"""

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def fetch_github(token: str) -> dict:
    res = _get("https://api.github.com/graphql", token, {"query": GRAPHQL, "variables": {"login": USER}})
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    u = res["data"]["user"]
    nodes = u["repositories"]["nodes"]
    sizes: dict[str, int] = {}
    for n in nodes:
        for e in n["languages"]["edges"]:
            if e["node"]["name"] not in EXCLUDE_LANGUAGES:
                sizes[e["node"]["name"]] = sizes.get(e["node"]["name"], 0) + e["size"]
    total = sum(sizes.values()) or 1
    langs = [{"name": k, "share": v / total} for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])]
    cc = u["contributionsCollection"]
    weeks = [[{"date": d["date"], "count": d["contributionCount"], "level": LEVELS[d["contributionLevel"]]}
              for d in w["contributionDays"]] for w in cc["contributionCalendar"]["weeks"]]
    return {
        "user": {"followers": u["followers"]["totalCount"], "public_repos": u["repositories"]["totalCount"],
                 "stars": sum(n["stargazerCount"] for n in nodes)},
        "contrib": {"total": cc["contributionCalendar"]["totalContributions"],
                    "commits": cc["totalCommitContributions"], "prs": cc["totalPullRequestContributions"],
                    "issues": cc["totalIssueContributions"], "reviews": cc["totalPullRequestReviewContributions"],
                    "weeks": weeks},
        "languages": langs,
        "repos": {n["name"]: {"stars": n["stargazerCount"], "forks": n["forkCount"],
                              "language": (n["primaryLanguage"] or {}).get("name"),
                              "description": n["description"],
                              "release": (n["latestRelease"] or {}).get("tagName"),
                              "pushed": n["pushedAt"]} for n in nodes},
    }


def fetch_pypi(pkg: str) -> dict:
    out = {}
    try:
        out["version"] = _get(f"https://pypi.org/pypi/{pkg}/json")["info"]["version"]
    except Exception as e:  # noqa: BLE001
        print(f"pypi {pkg}: {e}", file=sys.stderr)
    try:
        out["last_month"] = _get(f"https://pypistats.org/api/packages/{pkg}/recent")["data"]["last_month"]
    except Exception as e:  # noqa: BLE001
        print(f"pypistats {pkg}: {e}", file=sys.stderr)
    return out


def sample_data() -> dict:
    rng = random.Random(7)
    start = date.today() - timedelta(days=date.today().weekday() + 1 + 52 * 7)
    weeks, day = [], start
    while day <= date.today():
        week = []
        for _ in range(7):
            if day > date.today():
                break
            c = 0 if rng.random() < 0.35 else int(rng.expovariate(1 / 5))
            week.append({"date": day.isoformat(), "count": c})
            day += timedelta(days=1)
        weeks.append(week)
    counts = sorted(d["count"] for w in weeks for d in w if d["count"])
    q = [counts[int(len(counts) * f)] for f in (0.25, 0.5, 0.75)]
    for w in weeks:
        for d in w:
            d["level"] = 0 if not d["count"] else 1 + sum(d["count"] > x for x in q)
    total = sum(d["count"] for w in weeks for d in w)
    return {
        "user": {"followers": 41, "public_repos": 18, "stars": 63},
        "contrib": {"total": total, "commits": int(total * 0.82), "prs": 23, "issues": 11, "reviews": 4, "weeks": weeks},
        "languages": [{"name": "Python", "share": 0.71}, {"name": "TypeScript", "share": 0.11},
                      {"name": "C", "share": 0.08}, {"name": "JavaScript", "share": 0.05},
                      {"name": "Shell", "share": 0.03}, {"name": "CSS", "share": 0.02}],
        "repos": {"machyon": {"stars": 12, "forks": 1, "language": "Python", "release": "v1.0.0", "description": ""},
                  "AiMi-Anime-RAG-System": {"stars": 9, "forks": 2, "language": "Python", "description": ""},
                  "AnimeX": {"stars": 5, "forks": 0, "language": "Python",
                             "description": "Anime discovery app with search, filters and watchlists."},
                  "stylizers": {"stars": 3, "forks": 0, "language": "Python", "description": ""}},
        "pypi": {"machyon": {"version": "1.0.0", "last_month": 1204}, "stylizers": {"version": "0.0.1", "last_month": 96}},
    }


def merge(new: dict, old: dict) -> dict:
    """Fill gaps in new with old, recursively, so a failed fetch keeps the last good value."""
    for k, v in old.items():
        if k not in new or new[k] in (None, {}, []):
            new[k] = v
        elif isinstance(v, dict) and isinstance(new[k], dict):
            merge(new[k], v)
    return new


def collect(args) -> dict:
    if args.sample:
        return sample_data()
    old = {}
    if args.previous and Path(args.previous).is_file():
        try:
            old = json.loads(Path(args.previous).read_text())
        except json.JSONDecodeError:
            pass
    data: dict = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            data = fetch_github(token)
        except Exception as e:  # noqa: BLE001
            print(f"github: {e}", file=sys.stderr)
    else:
        print("GITHUB_TOKEN not set; using previous data", file=sys.stderr)
    pkgs = {MACHYON["pypi"], *(p["pypi"] for p in PROJECTS if p.get("pypi"))}
    data["pypi"] = {p: fetch_pypi(p) for p in sorted(pkgs)}
    data = merge(data, old)
    if "contrib" not in data:
        sys.exit("No GitHub data and no previous data.json: set GITHUB_TOKEN and run again.")
    data["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


# ---------------------------------------------------------------- type


@lru_cache(maxsize=None)
def _base_font() -> TTFont:
    return TTFont(FONT)


@lru_cache(maxsize=None)
def _metrics(wght: int, wdth: int, opsz: int):
    f = instancer.instantiateVariableFont(TTFont(FONT), {"wght": wght, "wdth": wdth, "opsz": opsz})
    return f.getBestCmap(), f["hmtx"].metrics, f["head"].unitsPerEm


def _opsz(size: float) -> int:
    return int(max(12, min(96, size)))


def measure(s: str, size: float, wght: int = 400, wdth: int = 100, ls: float = 0) -> float:
    cmap, hmtx, upem = _metrics(wght, wdth, _opsz(size))
    adv = sum(hmtx[cmap.get(ord(c), ".notdef")][0] if cmap.get(ord(c)) else upem * 0.5 for c in s)
    return adv / upem * size + ls * max(0, len(s) - 1)


def wrap(s: str, size: float, width: float, wght: int = 400, wdth: int = 100) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and measure(trial, size, wght, wdth) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _font_face(chars: set[str]) -> str:
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    f = TTFont(FONT)
    s = subset.Subsetter(opts)
    s.populate(text="".join(sorted(chars)) + " 0123456789")
    s.subset(f)
    buf = io.BytesIO()
    f.flavor = "woff2"
    f.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return (f"@font-face{{font-family:'Bric';src:url(data:font/woff2;base64,{b64}) format('woff2');"
            "font-weight:200 800;font-stretch:75% 100%;}")


# ---------------------------------------------------------------- canvas


def fmt(n) -> str:
    if n is None:
        return "–"
    n = int(n)
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n:,}"


class Canvas:
    def __init__(self, w: int, h: int, theme: str, title: str):
        self.w, self.h, self.t, self.title = w, h, THEMES[theme], title
        self.parts: list[str] = []
        self.css: list[str] = []
        self.chars: set[str] = set(title)

    def c(self, name: str) -> str:
        return self.t.get(name, name)

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, size, wght=400, wdth=100, fill="text", anchor="start", ls=0.0, cls="", op=1.0):
        self.chars.update(s)
        style = (f"font-size:{size}px;font-variation-settings:'wght' {wght},'wdth' {wdth},'opsz' {_opsz(size)};"
                 + (f"letter-spacing:{ls}px;" if ls else ""))
        attrs = f' class="f{" " + cls if cls else ""}"'
        o = f' opacity="{op}"' if op != 1 else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}"{attrs} style="{style}" fill="{self.c(fill)}"'
                 f' text-anchor="{anchor}"{o}>{escape(s)}</text>')
        return measure(s, size, wght, wdth, ls)

    def frame(self, r=16, gap=0):
        self.add(f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1 - gap}" rx="{r}" '
                 f'fill="{self.c("bg")}" stroke="{self.c("rule")}"/>')

    def render(self) -> str:
        css = (_font_face(self.chars)
               + ".f{font-family:'Bric',ui-sans-serif,system-ui,sans-serif;}"
               + "".join(self.css)
               + "@media (prefers-reduced-motion:reduce){*{animation:none!important;}}")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{escape(self.title)}">'
                f"<title>{escape(self.title)}</title><style>{css}</style>{''.join(self.parts)}</svg>")


def star(cx, cy, r, fill) -> str:
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append(f"{cx + rr * math.cos(a):.2f},{cy + rr * math.sin(a):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="{fill}"/>'


def facts(cv: Canvas, x, y, items, gap=28, size=15):
    """A row of value + label pairs."""
    for value, label in items:
        x += cv.text(x, y, value, size, wght=650)
        x += 6
        x += cv.text(x, y, label, size, fill="muted")
        x += gap
    return x


def opa(theme: str, x, y, size) -> str:
    src = (OPA / f"opa-symbol-{theme}.svg").read_text()
    vb = re.search(r'viewBox="([^"]+)"', src).group(1)
    inner = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", src, flags=re.S)
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="{vb}">{inner}</svg>'


# ---------------------------------------------------------------- cards

B1 = (44.0, 0.0)   # lattice basis: a sheared lattice, as in lattice cryptography
B2 = (17.0, 38.0)


def header(theme: str) -> str:
    h = 270
    cv = Canvas(W, h, theme, "Divyanshu Singh. Research engineer working where post-quantum security meets real-time performance.")
    cv.frame(18)
    pts = [(i * B1[0] + j * B2[0] + 12, j * B2[1] + 20) for j in range(-1, 8) for i in range(-5, 22)]
    pts = [(x, y) for x, y in pts if 8 < x < W - 8 and 8 < y < h - 8]
    # the target t sits between lattice points; v is its closest lattice point
    base = min(pts, key=lambda q: math.hypot(q[0] - 660, q[1] - 112))
    tx, ty = base[0] + 0.45 * (B1[0] + B2[0]), base[1] + 0.45 * B2[1]
    vx, vy = min(pts, key=lambda q: math.hypot(q[0] - tx, q[1] - ty))
    ox, oy = base[0] - B1[0], base[1] - B2[1]  # draw the basis from a point up and to the left
    cv.add('<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/>'
           '<stop offset=".55" stop-color="#fff" stop-opacity=".5"/><stop offset=".72" stop-color="#fff"/></linearGradient>'
           f'<mask id="m"><rect width="{W}" height="{h}" fill="url(#g)"/></mask>'
           f'<marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           f'<path d="M1,1 L7,4 L1,7" fill="none" stroke="{cv.c("accent")}" stroke-width="1.4" stroke-linecap="round"/></marker></defs>')
    cv.add('<g mask="url(#m)">')
    for px, py in pts:
        near = math.hypot(px - tx, py - ty) < 70
        cv.add(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{2.6 if near else 2}" fill="{cv.c("accent" if near else "dot")}"/>')
    cv.add("</g>")
    for bx, by in (B1, B2):
        cv.add(f'<line x1="{ox:.1f}" y1="{oy:.1f}" x2="{ox + bx - bx / 16:.1f}" y2="{oy + by - by / 16:.1f}" '
               f'stroke="{cv.c("accent")}" stroke-width="1.6" stroke-linecap="round" marker-end="url(#ah)" class="b"/>')
    cv.add(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="3.4" fill="{cv.c("accent")}" class="b"/>')
    L = math.hypot(vx - tx, vy - ty) - 7
    ang = math.atan2(vy - ty, vx - tx)
    ex, ey = tx + L * math.cos(ang), ty + L * math.sin(ang)
    cv.add(f'<line x1="{tx:.1f}" y1="{ty:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="{cv.c("signal")}" stroke-width="2.2" '
           f'stroke-linecap="round" stroke-dasharray="{L:.1f}" class="v"/>')
    cv.add(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="5.5" fill="{cv.c("bg")}" stroke="{cv.c("signal")}" stroke-width="2.2" class="t"/>')
    cv.add(f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="6.5" fill="{cv.c("signal")}" class="p"/>')
    cv.text(W - 30, h - 26, "closest vector problem", 13, fill="muted", anchor="end", cls="cap")
    cv.css.append(
        ".b{animation:fade .6s ease-out .1s both}.t{animation:fade .5s ease-out .5s both}"
        f".v{{animation:draw .9s cubic-bezier(.5,0,.2,1) 1s both}}"
        f".p{{transform-origin:{vx:.1f}px {vy:.1f}px;animation:pop .45s cubic-bezier(.3,1.6,.5,1) 1.8s both}}"
        ".cap{animation:fade .6s ease-out 2.1s both}"
        f"@keyframes fade{{from{{opacity:0}}}}@keyframes draw{{from{{stroke-dashoffset:{L:.1f}}}to{{stroke-dashoffset:0}}}}"
        "@keyframes pop{from{transform:scale(0)}}"
    )
    cv.text(42, 126, "Divyanshu Singh", 66, wght=720, wdth=82, ls=-1.2)
    cv.text(44, 166, "Research engineer. I work where post-quantum security", 19, fill="muted")
    cv.text(44, 192, "meets real-time performance.", 19, fill="muted")
    return cv.render()


def machyon_card(theme: str, data: dict) -> str:
    h = 236
    repo = data.get("repos", {}).get(MACHYON["repo"], {})
    pk = data.get("pypi", {}).get(MACHYON["pypi"], {})
    cv = Canvas(W, h, theme, "Machyon: every image, Mach speed, zero lost. Async image downloader for Python.")
    cv.frame(18)
    cv.add(opa(theme, 30, 34, 168))
    x = 228
    cv.text(x, 74, "Machyon", 44, wght=720, wdth=82, ls=-0.6)
    cv.text(x, 102, MACHYON["tagline"], 17, wght=500, fill="accent")
    for i, line in enumerate(wrap(MACHYON["about"], 15, W - x - 36)):
        cv.text(x, 134 + i * 22, line, 15, fill="muted")
    version = pk.get("version") or (repo.get("release") or "").lstrip("v") or None
    items = []
    if version:
        items.append((version, "on PyPI"))
    items.append((fmt(repo.get("stars")), "stars"))
    if pk.get("last_month") is not None:
        items.append((fmt(pk["last_month"]), "downloads last month"))
    facts(cv, x, 204, items)
    # install command, right-aligned
    cmd = "pip install machyon"
    cw = measure(cmd, 14, 500) + 28
    cv.add(f'<rect x="{W - 30 - cw:.1f}" y="184" width="{cw:.1f}" height="30" rx="7" fill="{cv.c("code")}"/>')
    cv.text(W - 30 - cw / 2, 204, cmd, 14, wght=500, anchor="middle")
    return cv.render()


def project_row(theme: str, data: dict, p: dict) -> str:
    h = 104  # 96 of card and 8 of space below, so stacked rows don't touch
    repo = data.get("repos", {}).get(p["repo"], {})
    about = p.get("about") or repo.get("description") or ""
    cv = Canvas(W, h, theme, f'{p["title"]}: {about}')
    cv.frame(12, gap=8)
    cv.text(28, 42, p["title"], 24, wght=680, wdth=88)
    lines = wrap(about, 15, 560)
    for i, line in enumerate(lines[:2]):
        cv.text(28, 68 + i * 20, line, 15, fill="muted")
    # stats on the right
    right = W - 28
    stats = []
    if p.get("pypi") and data.get("pypi", {}).get(p["pypi"], {}).get("version"):
        stats.append(("PyPI " + data["pypi"][p["pypi"]]["version"], None))
    if repo.get("language"):
        stats.append((repo["language"], None))
    stats.append((fmt(repo.get("stars")), "star"))
    y = 42
    for label, kind in reversed(stats):
        wdt = measure(label, 14, 550)
        cv.text(right, y, label, 14, wght=550, anchor="end", fill="text" if kind else "muted")
        right -= wdt
        if kind == "star":
            cv.add(star(right - 9, y - 5, 6, cv.c("signal")))
            right -= 18
        right -= 18
    return cv.render()


def quanturos_card(theme: str) -> str:
    h = 262
    q = QUANTUROS
    cv = Canvas(W, h, theme, f'{q["name"]}, patent pending. {q["about"]} Handshake latency reduced from '
                f'{q["before_ms"]} ms to about {q["after_ms"]} ms.')
    cv.frame(18)
    nw = cv.text(30, 62, q["name"], 38, wght=720, wdth=82, ls=0.4)
    tag = "Patent pending"
    tw = measure(tag, 13, 600) + 20
    cv.add(f'<rect x="{30 + nw + 14:.1f}" y="38" width="{tw:.1f}" height="26" rx="13" fill="none" '
           f'stroke="{cv.c("signal")}" stroke-width="1.4"/>')
    cv.text(30 + nw + 14 + tw / 2, 56, tag, 13, wght=600, fill="signal", anchor="middle")
    cv.text(30, 88, q["long"], 13, fill="muted")
    for i, line in enumerate(wrap(q["about"], 16, W - 60)):
        cv.text(30, 118 + i * 23, line, 16)
    # handshake latency on a log axis from 1 ms to 1 s
    ax0, ax1, ay = 30, W - 290, 220
    lx = lambda ms: ax0 + (ax1 - ax0) * math.log10(ms) / 3  # noqa: E731
    cv.text(ax0, 174, "Handshake latency, log scale", 13, fill="muted")
    cv.add(f'<line x1="{ax0}" y1="{ay}" x2="{ax1}" y2="{ay}" stroke="{cv.c("rule")}" stroke-width="2"/>')
    for ms, lab in ((1, "1 ms"), (10, "10 ms"), (100, "100 ms"), (1000, "1 s")):
        cv.add(f'<line x1="{lx(ms):.1f}" y1="{ay - 4}" x2="{lx(ms):.1f}" y2="{ay + 4}" stroke="{cv.c("rule")}" stroke-width="2"/>')
        cv.text(lx(ms), ay + 24, lab, 12, fill="muted", anchor="middle")
    b, a = lx(q["before_ms"]), lx(q["after_ms"])
    cv.add(f'<line x1="{b - 9:.1f}" y1="{ay}" x2="{a + 12:.1f}" y2="{ay}" stroke="{cv.c("signal")}" stroke-width="3" stroke-linecap="round"/>'
           f'<path d="M{a + 19:.1f},{ay - 6} L{a + 12:.1f},{ay} L{a + 19:.1f},{ay + 6}" fill="none" '
           f'stroke="{cv.c("signal")}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>')
    cv.add(f'<circle cx="{b:.1f}" cy="{ay}" r="6" fill="{cv.c("bg")}" stroke="{cv.c("muted")}" stroke-width="2"/>')
    cv.add(f'<circle cx="{a:.1f}" cy="{ay}" r="6.5" fill="{cv.c("signal")}"/>')
    cv.text(b, ay - 16, f'{q["before_ms"]} ms', 15, wght=600, fill="muted", anchor="middle")
    cv.text(a, ay - 16, f'~{q["after_ms"]} ms', 15, wght=680, fill="signal", anchor="middle")
    # throughput
    tx = W - 240
    cv.add(f'<line x1="{tx - 26}" y1="168" x2="{tx - 26}" y2="240" stroke="{cv.c("rule")}" stroke-width="1"/>')
    cv.text(tx, 174, "Throughput", 13, fill="muted")
    for i, (val, lab) in enumerate(((q["enc"], "encryption"), (q["dec"], "decryption"))):
        y = 204 + i * 32
        vw = cv.text(tx, y, val, 22, wght=680, wdth=88)
        cv.text(tx + vw + 8, y, lab, 14, fill="muted")
    return cv.render()


def stats_card(theme: str, data: dict) -> str:
    h = 300
    c = data["contrib"]
    cv = Canvas(W, h, theme, f'{fmt(c["total"])} contributions in the last year, drawn as a lattice of days.')
    cv.frame(18)
    cv.text(30, 78, fmt(c["total"]), 54, wght=720, wdth=82, ls=-1)
    cv.text(32, 102, "contributions in the last year", 15, fill="muted")
    rows = [(fmt(c.get("commits")), "commits"), (fmt(c.get("prs")), "pull requests"),
            (fmt(c.get("issues")), "issues"), (fmt(data.get("user", {}).get("stars")), "stars earned")]
    for i, (v, lab) in enumerate(rows):
        y = 140 + i * 25
        vw = cv.text(32, y, v, 16, wght=650)
        cv.text(32 + max(vw, 44) + 8, y, lab, 15, fill="muted")
    # the lattice of days: week along b1, weekday along b2
    weeks = c["weeks"][-53:]
    b1, b2 = (9.55, 0.0), (3.3, 21.0)
    x0, y0 = 262.0, 40.0
    first = date.fromisoformat(weeks[0][0]["date"])
    off = (first.weekday() + 1) % 7  # GitHub weeks start on Sunday
    radius = [1.5, 2.3, 3.0, 3.7, 4.4]
    busiest = max((d for w in weeks for d in w), key=lambda d: d["count"])
    last_month = None
    for i, w in enumerate(weeks):
        for k, d in enumerate(w):
            wd = k + (off if i == 0 else 0)
            x, y = x0 + i * b1[0] + wd * b2[0], y0 + wd * b2[1]
            lv = d["level"]
            cv.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius[lv]}" fill="{cv.t["ramp"][lv]}"/>')
            if d is busiest and d["count"]:
                bx, by = x, y
            dt = date.fromisoformat(d["date"])
            if wd == 6 and dt.month != last_month and dt.day <= 7:
                last_month = dt.month
                if i < len(weeks) - 2:
                    cv.text(x - 2, y0 + 7 * b2[1] + 8, dt.strftime("%b"), 12, fill="muted")
    if busiest["count"]:
        cv.add(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="8" fill="none" stroke="{cv.c("signal")}" stroke-width="1.8"/>')
        bd = date.fromisoformat(busiest["date"])
        cv.text(W - 30, 214, f'Busiest day: {bd.day} {bd.strftime("%b")}, {busiest["count"]} contributions',
                13, fill="muted", anchor="end")
    # languages
    langs = [l for l in data.get("languages", []) if l["share"] >= 0.01][:5]
    if langs:
        ly, lx0, lw = 244, 30, W - 60
        x = lx0
        shades = [cv.c("accent"), cv.t["ramp"][3], cv.t["ramp"][2], cv.t["ramp"][1], cv.c("muted")]
        cv.add(f'<clipPath id="lb"><rect x="{lx0}" y="{ly}" width="{lw}" height="8" rx="4"/></clipPath>'
               f'<g clip-path="url(#lb)"><rect x="{lx0}" y="{ly}" width="{lw}" height="8" fill="{cv.c("rule")}"/>')
        rest = sum(l["share"] for l in langs)
        for l, col in zip(langs, shades):
            sw = lw * l["share"] / rest
            cv.add(f'<rect x="{x:.1f}" y="{ly}" width="{max(sw - 2, 1):.1f}" height="8" fill="{col}"/>')
            x += sw
        cv.add("</g>")
        x = lx0
        for l, col in zip(langs, shades):
            cv.add(f'<circle cx="{x + 4}" cy="{ly + 31}" r="4" fill="{col}"/>')
            x += 14
            x += cv.text(x, ly + 36, l["name"], 14, wght=550)
            x += 6
            x += cv.text(x, ly + 36, f'{l["share"] / rest:.0%}', 14, fill="muted")
            x += 24
    return cv.render()


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--previous", help="data.json from the last good run")
    ap.add_argument("--sample", action="store_true", help="made-up numbers, no network")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = collect(args)
    (out / "data.json").write_text(json.dumps(data, indent=1))
    for theme in THEMES:
        files = {
            "header": header(theme),
            "machyon": machyon_card(theme, data),
            "quanturos": quanturos_card(theme),
            "activity": stats_card(theme, data),
            **{f'project-{p["repo"].lower()}': project_row(theme, data, p) for p in PROJECTS},
        }
        for name, svg in files.items():
            (out / f"{name}-{theme}.svg").write_text(svg)
    print(f"wrote {len(list(out.glob('*.svg')))} cards to {out}/")


if __name__ == "__main__":
    main()
