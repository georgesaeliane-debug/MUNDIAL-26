"""
MUNDIAL 26 — Live World Cup 2026 tracker (Python / Flask companion)
===================================================================
The "keep it updated" half of the project. Serves fresh data as JSON so the
HTML front-end can show live scores AND minute-to-minute match stats.

ENDPOINTS
  GET /              -> a minimal live page (quick sanity check)
  GET /api/matches   -> all matches: scores, status, minute, venue
  GET /api/live      -> ONLY in-play matches, WITH live stats + event feed
                        (possession, shots, corners, fouls, goals, cards…)

DATA SOURCES
  1. API-Football (api-sports.io) -> live scores, statistics, events.
     Free tier = 100 requests/day. Set a free key:
         export API_FOOTBALL_KEY=your_key
  2. openfootball/worldcup.json -> public-domain schedule + results,
     NO KEY REQUIRED. Automatic fallback for /api/matches so the app runs
     out-of-the-box. (Live per-minute stats require the API key.)
  Cross-check public facts against FIFA.com (the source of truth).

QUICK START
    pip install flask requests flask-cors
    export API_FOOTBALL_KEY=xxxxxxxx      # optional, needed for /api/live stats
    python worldcup_live.py
    # open http://127.0.0.1:5000

Educational project — respect each provider's terms of service and limits.
"""

import os
import time
import requests
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# Allow the static HTML site (hosted elsewhere) to read this API from the browser.
try:
    from flask_cors import CORS
    CORS(app)
except Exception:
    print("  (tip: pip install flask-cors so your hosted site can call this API)")

API_KEY = os.environ.get("API_FOOTBALL_KEY")           # optional
API_BASE = "https://v3.football.api-sports.io"
WORLD_CUP_LEAGUE_ID = 1                                  # FIFA World Cup
SEASON = 2026
OPENFOOTBALL_URL = (
    "https://raw.githubusercontent.com/openfootball/"
    "worldcup.json/master/2026/worldcup.json"
)

# ---- tiny cache so we don't burn the 100 req/day free tier ----
_cache = {}
CACHE_TTL = 60      # seconds for /api/matches
LIVE_TTL = 15       # seconds for /api/live (live data changes fast)


def _num(v):
    """Coerce API stat values ('63%', '12', None) to an int."""
    if v is None:
        return 0
    if isinstance(v, str):
        v = v.replace("%", "").strip()
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _cached(key, ttl, producer):
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    data = producer()
    _cache[key] = (data, time.time())
    return data


# ---------------- /api/matches ----------------
def _fetch_api_football():
    headers = {"x-apisports-key": API_KEY}
    params = {"league": WORLD_CUP_LEAGUE_ID, "season": SEASON}
    fixtures = requests.get(f"{API_BASE}/fixtures", headers=headers,
                            params=params, timeout=15).json().get("response", [])
    matches = []
    for f in fixtures:
        fx, goals, teams = f["fixture"], f["goals"], f["teams"]
        matches.append({
            "home": teams["home"]["name"], "away": teams["away"]["name"],
            "home_score": goals["home"], "away_score": goals["away"],
            "status": fx["status"]["short"], "minute": fx["status"].get("elapsed"),
            "kickoff": fx["date"], "venue": (fx.get("venue") or {}).get("name"),
        })
    return {"source": "API-Football (live)", "matches": matches}


def _fetch_openfootball():
    raw = requests.get(OPENFOOTBALL_URL, timeout=15).json()
    matches = []
    for m in raw.get("matches", []):
        score = m.get("score", {}).get("ft")
        matches.append({
            "home": m.get("team1"), "away": m.get("team2"),
            "home_score": score[0] if score else None,
            "away_score": score[1] if score else None,
            "status": "FT" if score else "NS", "minute": None,
            "kickoff": f'{m.get("date","")} {m.get("time","")}'.strip(),
            "venue": m.get("ground"),
        })
    return {"source": "openfootball (public domain)", "matches": matches}


def get_matches():
    def produce():
        try:
            return _fetch_api_football() if API_KEY else _fetch_openfootball()
        except Exception as e:
            try:
                d = _fetch_openfootball()
                d["note"] = f"primary feed failed ({e}); used fallback"
                return d
            except Exception as e2:
                return {"source": "none", "matches": [], "error": str(e2)}
    return _cached("matches", CACHE_TTL, produce)


# ---------------- /api/live (in-play + stats + events) ----------------
def _fetch_live_full():
    headers = {"x-apisports-key": API_KEY}
    live = requests.get(
        f"{API_BASE}/fixtures", headers=headers,
        params={"league": WORLD_CUP_LEAGUE_ID, "season": SEASON, "live": "all"},
        timeout=15,
    ).json().get("response", [])

    out = []
    for f in live:
        fid = f["fixture"]["id"]

        # --- statistics (one block per team) ---
        stats = {"home": {}, "away": {}}
        try:
            sresp = requests.get(f"{API_BASE}/fixtures/statistics", headers=headers,
                                 params={"fixture": fid}, timeout=15).json().get("response", [])
            for idx, team in enumerate(sresp[:2]):
                d = {s["type"]: s["value"] for s in team.get("statistics", [])}
                stats["home" if idx == 0 else "away"] = {
                    "poss": _num(d.get("Ball Possession")),
                    "shots": _num(d.get("Total Shots")),
                    "sot": _num(d.get("Shots on Goal")),
                    "soff": _num(d.get("Shots off Goal")),
                    "sblk": _num(d.get("Blocked Shots")),
                    "corners": _num(d.get("Corner Kicks")),
                    "saves": _num(d.get("Goalkeeper Saves")),
                    "fouls": _num(d.get("Fouls")),
                    "offs": _num(d.get("Offsides")),
                    "yc": _num(d.get("Yellow Cards")),
                }
        except Exception:
            pass

        # --- events (goals, cards, subs) with full detail ---
        events = []
        try:
            eresp = requests.get(f"{API_BASE}/fixtures/events", headers=headers,
                                 params={"fixture": fid}, timeout=15).json().get("response", [])
            for ev in eresp:
                etype = ev.get("type", "")
                detail = ev.get("detail", "") or ""
                kind = "goal" if etype == "Goal" else "card" if etype == "Card" else "sub"
                team = (ev.get("team") or {}).get("name", "")
                player = (ev.get("player") or {}).get("name", "")
                assist = (ev.get("assist") or {}).get("name", "")
                minute = (ev.get("time") or {}).get("elapsed")
                extra = (ev.get("time") or {}).get("extra")
                mtxt = f"{minute}+{extra}'" if extra else (f"{minute}'" if minute is not None else "")

                # card colour + short offence reason (1-2 words) from `comments`
                card = None
                offense = ""
                if kind == "card":
                    card = "r" if "Red" in detail else "y"
                    # API-Football puts the reason in `comments` e.g. "Argument", "Foul"
                    offense = (ev.get("comments") or "").strip()
                    if not offense:
                        offense = "Foul" if card == "y" else "Serious foul"
                    offense = " ".join(offense.split()[:2])  # keep to 1-2 words

                if kind == "goal":
                    label = "GOAL"
                    text = f"GOAL {mtxt} — {team} · {player}" + (f" (assist {assist})" if assist else "")
                    if detail and detail not in ("Normal Goal",):
                        text += f" [{detail}]"   # Penalty / Own Goal
                elif kind == "card":
                    colour = "Red card" if card == "r" else "Yellow card"
                    text = f"{colour} {mtxt} — {player} ({team}) · {offense}"
                else:
                    text = f"Sub {mtxt} — {team}: {player}" + (f" ⭤ {assist}" if assist else "")

                events.append({
                    "minute": minute, "extra": extra, "type": kind,
                    "detail": detail, "team": team, "player": player,
                    "assist": assist, "card": card, "offense": offense,
                    "text": text,
                })
        except Exception:
            pass

        # --- per-player match ratings + Man of the Match (highest rating) ---
        ratings = {}
        motm = None
        best = -1.0
        try:
            presp = requests.get(f"{API_BASE}/fixtures/players", headers=headers,
                                 params={"fixture": fid}, timeout=15).json().get("response", [])
            for team_block in presp:
                tname = (team_block.get("team") or {}).get("name", "")
                for pl in team_block.get("players", []):
                    name = (pl.get("player") or {}).get("name")
                    stat = (pl.get("statistics") or [{}])[0].get("games", {})
                    rating = stat.get("rating")
                    if name and rating:
                        r = round(float(rating), 1)
                        ratings[name.split(" ")[-1]] = r     # key by last name for the front-end
                        if r > best:
                            best = r
                            motm = f"{name} ({tname})"
        except Exception:
            pass

        # --- lineups (startXI + subs) formatted "Name (position)" for the pitch ---
        lineups = {"home": [], "away": []}
        try:
            POS = {"G": "goalkeeper", "D": "defender", "M": "midfielder", "F": "forward"}
            lresp = requests.get(f"{API_BASE}/fixtures/lineups", headers=headers,
                                 params={"fixture": fid}, timeout=15).json().get("response", [])
            for idx, block in enumerate(lresp[:2]):
                side = "home" if idx == 0 else "away"
                arr = []
                for grp in ("startXI", "substitutes"):     # 11 starters first, then the bench
                    for row in block.get(grp, []):
                        pl = row.get("player") or {}
                        nm = pl.get("name") or ""
                        if nm:
                            arr.append(f"{nm} ({POS.get(pl.get('pos') or 'M', 'midfielder')})")
                lineups[side] = arr
        except Exception:
            pass

        g, teams, fx = f["goals"], f["teams"], f["fixture"]
        venue = fx.get("venue") or {}
        out.append({
            "home": teams["home"]["name"], "away": teams["away"]["name"],
            "home_score": g["home"], "away_score": g["away"],
            "status": fx["status"]["short"], "minute": fx["status"].get("elapsed"),
            "venue": venue.get("name"), "city": venue.get("city"),
            "attendance": fx.get("attendance"),   # official crowd once published
            "stats": stats, "events": events, "ratings": ratings, "motm": motm,
            "lineups": lineups,
        })
    return {"source": "API-Football (live)", "matches": out}


@app.route("/api/matches")
def api_matches():
    return jsonify(get_matches())


@app.route("/api/live")
def api_live():
    if not API_KEY:
        return jsonify({"source": "no key — set API_FOOTBALL_KEY for live stats",
                        "matches": []})
    try:
        return jsonify(_cached("live", LIVE_TTL, _fetch_live_full))
    except Exception as e:
        return jsonify({"source": "error", "matches": [], "error": str(e)})


PAGE = """
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>MUNDIAL 26 — Live</title>
<style>
 body{margin:0;background:#0A0E1C;color:#EEF2FF;font-family:system-ui,sans-serif;padding:24px}
 h1{font-size:22px;letter-spacing:.5px} .src{color:#37E6FF;font-size:13px;font-family:monospace;margin-bottom:18px}
 .m{display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid #27314f;background:#121829;border-radius:12px;padding:12px 16px;margin:8px 0}
 .t{font-weight:700} .sc{font-family:monospace;font-weight:700;font-size:18px;color:#C8FF3D}
 .st{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#8A95B8} .live{color:#FF3B3B}
 .v{font-size:12px;color:#8A95B8;font-family:monospace}
</style></head><body>
<h1>MUNDIAL 26 · live feed</h1><div class=src id=src>loading…</div><div id=list></div>
<script>
async function load(){
  const r=await fetch('/api/matches'); const d=await r.json();
  document.getElementById('src').textContent='source: '+d.source+(d.note?(' — '+d.note):'');
  document.getElementById('list').innerHTML=(d.matches||[]).map(m=>{
    const live=!['FT','NS','PST'].includes(m.status);
    const st=live?`<span class="st live">${m.minute?m.minute+"'":m.status}</span>`:`<span class=st>${m.status}</span>`;
    const sc=(m.home_score!=null)?`${m.home_score} – ${m.away_score}`:'vs';
    return `<div class=m><div><div class=t>${m.home} <span class=sc>${sc}</span> ${m.away}</div>
            <div class=v>${m.venue||''} · ${m.kickoff||''}</div></div>${st}</div>`;
  }).join('')||'<p>No matches returned.</p>';
}
load(); setInterval(load,30000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    mode = "API-Football (live scores + stats)" if API_KEY else "openfootball fallback (no key; /api/live needs a key)"
    port = int(os.environ.get("PORT", 5000))   # hosts like Render/Railway set PORT for you
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"\n  MUNDIAL 26 live feed starting in: {mode}")
    print(f"  http://127.0.0.1:{port}   ·   /api/matches   ·   /api/live\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
