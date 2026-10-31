"""
Season player stats for FK Zeljeznicar.

For every posted match we store each Zeljo player's numbers in
player_stats.json (keyed by match id, so re-running never double counts),
then sum them up and post one Discord embed with the season totals.

Minutes rule (no stoppage time on the page):
  - starter plays from 0, a sub from the minute he came on
  - he stops at the minute he went off or got a red card, otherwise 90
  - anyone who came on is counted with at least 1 minute
"""

import json

STATS_FILE = "player_stats.json"
MATCH_LENGTH = 90


def minute_int(m, length=MATCH_LENGTH):
    """'90+3' -> 90, '74' -> 74, None -> None."""
    if not m:
        return None
    return min(int(m.split("+")[0]), length)


def match_player_stats(match, our_team):
    """Per-player numbers for our team in one match."""
    for team_name, players in match["teams"]:
        if our_team in team_name.upper():
            break
    else:
        return {}

    length = match["info"].get("length", MATCH_LENGTH)  # 120 with extra time
    result = {}
    for p in players:
        kinds = {}
        for kind, minute, _ in p["events"]:
            kinds.setdefault(kind, []).append(minute_int(minute, length))

        subs = sorted(m for m in kinds.get("sub", []) if m is not None)
        came_on = p["bench"] and bool(subs)
        if p["bench"] and not came_on:
            continue  # unused substitute

        if p["bench"]:
            start = subs[0]                                   # came on
            end = subs[1] if len(subs) > 1 else length  # maybe taken off again
            went_off = len(subs) > 1
        else:
            start = 0
            end = subs[0] if subs else length           # starter taken off
            went_off = bool(subs)
        for k in ("red", "second_yellow"):                    # sent off
            sent = [m for m in kinds.get(k, []) if m is not None]
            if sent:
                end = min(end, sent[0])

        result[p["name"]] = {
            "apps": 1,
            "starts": 0 if p["bench"] else 1,
            "sub_in": 1 if came_on else 0,
            "sub_out": 1 if went_off else 0,
            "minutes": max(end - start, 1),
            "goals": len(kinds.get("goal", [])) + len(kinds.get("penalty", [])),
            "pen_goals": len(kinds.get("penalty", [])),
            "own_goals": len(kinds.get("own_goal", [])),
            "yellow": len(kinds.get("yellow", [])) + len(kinds.get("second_yellow", [])),
            "second_yellow": len(kinds.get("second_yellow", [])),
            "red": len(kinds.get("red", [])) + len(kinds.get("second_yellow", [])),
            "captain": 1 if "C" in p["flags"] else 0,
        }
    return result


def load_stats():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_stats(data):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


COMPETITIONS = {"liga": "WWIN liga BiH", "kup": "Kup BiH"}


def competition_of(match):
    """'kup' for Kup BiH matches, otherwise 'liga'."""
    return "kup" if "kup" in (match["info"]["competition"] or "").lower() else "liga"


def record_match(mid, match, our_team):
    """Store (or overwrite) one match's player numbers."""
    data = load_stats()
    data[mid] = {
        "date": match["info"]["date"],
        "title": f"{match['home']} - {match['away']} {match['ft'][0]}:{match['ft'][1]}",
        "competition": competition_of(match),
        "players": match_player_stats(match, our_team),
    }
    save_stats(data)


def season_totals(competition):
    """Sum stored matches of one competition ('liga' or 'kup') per player."""
    data = {k: m for k, m in load_stats().items()
            if m.get("competition", "liga") == competition}
    totals = {}
    for m in data.values():
        for name, s in m["players"].items():
            t = totals.setdefault(name, {k: 0 for k in s})
            for k, v in s.items():
                t[k] = t.get(k, 0) + v
    return totals, len(data)


def short_name(full, all_names):
    first = full.split()[0]
    return full if sum(1 for n in all_names if n.split()[0] == first) > 1 else first


def utakmica(n):
    """Bosnian genitive after 'nakon': 1/2/3/4/21 utakmice, 5-20/11-14 utakmica."""
    if n % 10 in (1, 2, 3, 4) and n % 100 not in (11, 12, 13, 14):
        return f"{n} utakmice"
    return f"{n} utakmica"


def build_stats_embed(competition="liga", team_label="Željezničar"):
    totals, n_matches = season_totals(competition)
    if not totals:
        return None
    names = list(totals)
    rows = sorted(totals.items(), key=lambda x: (-x[1]["minutes"], x[0]))

    # Monospace table so the columns line up in Discord
    header = f"{'Igrač':<16}{'N':>3}{'S':>3}{'Min':>5}{'G':>3}{'ŽK':>4}{'CK':>3}"
    lines = [header, "─" * len(header)]
    for name, s in rows:
        nm = short_name(name, names)[:15]
        lines.append(f"{nm:<16}{s['apps']:>3}{s['starts']:>3}{s['minutes']:>5}"
                     f"{s['goals']:>3}{s['yellow']:>4}{s['red']:>3}")
    table = "```\n" + "\n".join(lines) + "\n```"

    def top(key, label, fmt=lambda v: str(v)):
        best = [(n, s[key]) for n, s in totals.items() if s[key] > 0]
        best.sort(key=lambda x: (-x[1], x[0]))
        if not best:
            return None
        return f"{label}: " + ", ".join(f"{short_name(n, names)} {fmt(v)}" for n, v in best[:3])

    leaders = [x for x in (
        top("goals", "⚽ Strijelci"),
        top("minutes", "⏱️ Najviše minuta"),
        top("yellow", "🟨 Žuti kartoni"),
        top("red", "🟥 Crveni kartoni"),
        top("sub_in", "🔄 Ulazio s klupe", lambda v: f"{v}x"),
        top("captain", "©️ Kapiten", lambda v: f"{v}x"),
    ) if x]

    extra = []
    pens = sum(s["pen_goals"] for s in totals.values())
    owns = sum(s["own_goals"] for s in totals.values())
    if pens:
        extra.append(f"Golova iz penala: {pens}")
    if owns:
        extra.append(f"Autogolova: {owns}")

    description = table[:4000]
    fields = [{"name": "Lideri", "value": "\n".join(leaders)[:1024], "inline": False}] if leaders else []
    if extra:
        fields.append({"name": "Ostalo", "value": "\n".join(extra), "inline": False})

    return {
        "title": f"📊 Statistika igrača – {team_label} · {COMPETITIONS[competition]}",
        "description": description,
        "color": 0x185FA5 if competition == "liga" else 0xBA7517,
        "fields": fields,
        "footer": {"text": f"{COMPETITIONS[competition]} · nakon {utakmica(n_matches)} · "
                           "N nastupi · S u startu · Min minute · G golovi · ŽK žuti · CK crveni"},
    }
