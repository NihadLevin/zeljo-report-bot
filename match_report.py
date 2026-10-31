"""
Match report bot for FK Zeljeznicar.

Reads an NFSBiH match page (https://www.nfsbih.ba/stats/match/<id>/),
extracts score, goals, cards and substitutions from the event icons
next to each player, and posts a summary to Discord (in Bosnian).

Usage:
  python match_report.py <match_url>            # print + post one match
  python match_report.py <match_url> --dry-run  # print only, no Discord post
  python match_report.py <match_url> --debug    # show raw icon signatures
  python match_report.py <match_url> --html     # show raw HTML of player cells
  python match_report.py --auto                 # find new Zeljo matches and post them
  python match_report.py --stats                # post season player stats now
  python match_report.py --rebuild-stats        # recompute stats from all posted matches

Env:
  DISCORD_WEBHOOK_URL  - Discord webhook to post to
"""

import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

import player_stats

BASE = "https://www.nfsbih.ba"
TEAM_PAGE = BASE + "/stats/team/54152223/411-fk-zeljeznicar/"  # league, season 26/27
CUP_PAGE = BASE + "/takmicenja/nogomet-muskarci/kup-bih/"         # Kup BiH (current season)
OUR_TEAM = "ŽELJEZNIČAR"
STATE_FILE = "posted_matches.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (ZeljoMatchBot)"}

MINUTE_RE = re.compile(r"(\d{1,3}(?:\s*\+\s*\d{1,2})?)\s*['’]")

# If the automatic detection gets an icon wrong, run with --debug, copy the
# signature it prints and map it here. Exact-substring match, checked first.
# Example: ICON_OVERRIDES = {"ico_7.png": "goal"}
ICON_OVERRIDES = {}

# Keyword rules, checked in order (more specific first).
ICON_RULES = [
    ("own_goal", ["own", "autogol", "auto-gol", "autogoal"]),
    ("penalty", ["penal", "penalty", "pen-goal", "goal-pen"]),
    ("second_yellow", ["yellow-red", "yellowred", "zuto-crven", "zutocrven", "2yellow", "second"]),
    ("red", ["red", "crven"]),
    ("yellow", ["yellow", "zut", "žut", "card"]),
    ("goal", ["goal", "gol", "ball", "lopta", "score"]),
    ("sub", ["sub", "izmjen", "zamjen", "change", "swap", "arrow", "inout", "in-out"]),
]


def fetch(url, tries=3):
    """GET with retries; the site is sometimes slow or briefly returns errors."""
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if attempt == tries:
                raise
            print(f"retry {attempt}/{tries - 1} for {url}: {e}")
            time.sleep(5 * attempt)


def icon_signature(tag):
    """Text that describes an icon tag: src, alt, title, class, style."""
    parts = []
    for attr in ("src", "data-src", "alt", "title", "style"):
        v = tag.get(attr)
        if v:
            parts.append(str(v))
    cls = tag.get("class")
    if cls:
        parts.append(" ".join(cls))
    return " ".join(parts).lower()


def classify(sig):
    for key, kind in ICON_OVERRIDES.items():
        if key.lower() in sig:
            return kind
    for kind, words in ICON_RULES:
        if any(w in sig for w in words):
            return kind
    return "unknown"


def is_icon(tag):
    """An icon is an img, or an empty element (span/i/div) carrying a class/style."""
    if not isinstance(tag, Tag):
        return False
    if tag.name == "img":
        return True
    if tag.name in ("span", "i", "div", "em") and not tag.get_text(strip=True):
        return bool(tag.get("class") or tag.get("style") or tag.get("title"))
    return False


def parse_player_cell(cell):
    """Return (name, flags, events[(kind, minute, signature)])."""
    link = cell.find("a")
    full = cell.get_text(" ", strip=True)
    if link and link.get_text(strip=True):
        name = link.get_text(" ", strip=True)
    else:
        # Plain-text name: the cell text without minutes and (GK)/(C)
        name = re.sub(r"\((GK|C)\)", "", MINUTE_RE.sub("", full))
        name = re.sub(r"\s+", " ", name).strip(" ,")
    flags = re.findall(r"\((GK|C)\)", full)

    # Every minute text ("74'") is one event. Its type comes from, in order:
    #  1. an icon element right before it (<img>, empty <span class=..>)
    #  2. the element(s) wrapping the minute (<span class="goal">74'</span>)
    #  3. an icon element right after it ("74' <img ...>")
    events = []
    pending = None  # icon seen before the minute
    for node in cell.descendants:
        if isinstance(node, Tag) and is_icon(node):
            pending = icon_signature(node)
        elif isinstance(node, NavigableString):
            if link and node.find_parent("a") is link:
                continue  # player name
            for m in MINUTE_RE.finditer(str(node)):
                minute = m.group(1).replace(" ", "")
                sig = pending or wrapper_signature(node, cell)
                pending = None
                events.append([classify(sig) if sig else "unknown", minute, sig, None])
    # case 3: minutes with no info, followed by a trailing icon
    events = attach_trailing_icons(cell, link, events)
    return name, flags, [tuple(e[:3]) for e in events]


def wrapper_signature(text_node, cell):
    """Signature of the elements wrapping a minute text, up to the table cell."""
    parts = []
    el = text_node.parent
    while el is not None and el is not cell:
        sig = icon_signature(el)
        if sig:
            parts.append(sig)
        el = el.parent
    return " ".join(parts)


def attach_trailing_icons(cell, link, events):
    """If minutes carry no type at all, try icons that come right after them."""
    if not any(e[0] == "unknown" and not e[2] for e in events):
        return events
    seq = []  # ordered list of ("min", value) and ("icon", signature)
    for node in cell.descendants:
        if isinstance(node, Tag) and is_icon(node):
            seq.append(("icon", icon_signature(node)))
        elif isinstance(node, NavigableString) and not (link and node.find_parent("a") is link):
            for m in MINUTE_RE.finditer(str(node)):
                seq.append(("min", m.group(1).replace(" ", "")))
    # pair each minute with the icon that directly follows it
    i, out = 0, []
    while i < len(seq):
        kind, val = seq[i]
        if kind == "min":
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            if nxt and nxt[0] == "icon":
                out.append([classify(nxt[1]), val, nxt[1], None])
                i += 2
                continue
            out.append(["unknown", val, "", None])
        i += 1
    return out if out else events


def parse_team_table(table, team_name):
    """Players of one lineup table. A player row starts with a shirt number."""
    players = []
    bench = False
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        text = tr.get_text(" ", strip=True)
        if not cells:
            continue
        if "Izmjene" in text and not re.fullmatch(r"\d{1,3}", cells[0].get_text(strip=True)):
            bench = True
            continue
        if len(cells) < 2 or not re.fullmatch(r"\d{1,3}", cells[0].get_text(strip=True)):
            continue
        if not cells[1].get_text(strip=True):
            continue
        number = cells[0].get_text(strip=True)
        name, flags, events = parse_player_cell(cells[1])
        players.append({"no": number, "name": name, "flags": flags,
                        "bench": bench, "events": events})
    return team_name, players


def parse_match(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    m = re.search(r"(.+?)\s+-\s+(.+?)\s+\((\d+)\s*:\s*(\d+)\)\s+(\d+)\s*:\s*(\d+)", title)
    if not m:
        raise ValueError(f"Can't read score from title: {title!r} (match not played yet?)")
    home, away = m.group(1).strip(), m.group(2).strip()
    ht = (m.group(3), m.group(4))
    ft = (m.group(5), m.group(6))
    # Anything after the score in the title (e.g. penalty shootout) is kept as-is
    after_score = title[m.end():].strip(" -|")

    page_text = soup.get_text("\n", strip=True)

    def field(label):
        mm = re.search(r"(?<![\w])" + re.escape(label) + r"\s*:\s*([^\n|]+)", page_text)
        return mm.group(1).strip(" |") if mm else None

    info = {
        "competition": None,
        "date": field("Datum"),
        "stadium": field("Stadion"),
        "attendance": field("Gledalaca"),
        "referee": field("Sudija"),
        "a1": field("A1"),
        "a2": field("A2"),
        "delegate": field("Delegat"),
        "observer": field("Posmatrač suđenja"),
        "status": field("Status"),
    }
    for h in soup.find_all(["h4", "h3"]):
        if "liga" in h.get_text().lower() or "kup" in h.get_text().lower():
            info["competition"] = h.get_text(strip=True)
            break

    # Lineup tables: the first row holds the team name (and its big crest).
    # Player links are not required, the site sometimes leaves them out.
    teams = []
    for table in soup.find_all("table"):
        first = table.find("tr")
        head = first.get_text(" ", strip=True).upper() if first else ""
        for team in (home, away):
            if head == team.upper() or (team.upper() in head and table.find("img", src=re.compile("tim_big"))):
                teams.append(parse_team_table(table, team))
                break
    low = page_text.lower()
    info["extra_time"] = "produž" in low or "produz" in low
    info["penalties"] = after_score or None
    info["length"] = 120 if info["extra_time"] else 90
    return {"home": home, "away": away, "ht": ht, "ft": ft, "info": info, "teams": teams}


def build_events(match):
    """Flatten player events into a timeline."""
    out = []
    for team_name, players in match["teams"]:
        for p in players:
            for kind, minute, sig in p["events"]:
                if kind == "sub":
                    kind = "sub_in" if p["bench"] else "sub_out"
                out.append({"team": team_name, "player": p["name"], "kind": kind,
                            "minute": minute, "sig": sig})

    def sort_key(e):
        if not e["minute"]:
            return (999, 0)
        base, _, extra = e["minute"].partition("+")
        return (int(base), int(extra or 0))

    return sorted(out, key=sort_key)


def group_subs(events):
    """Group substitutions by team and minute.

    The page doesn't say who replaced whom, so with several changes in the
    same minute we list everyone who came on and everyone who went off.
    """
    groups = {}
    for e in events:
        if e["kind"] in ("sub_in", "sub_out"):
            g = groups.setdefault((e["minute"], e["team"]), {"in": [], "out": []})
            g["in" if e["kind"] == "sub_in" else "out"].append(e["player"])
    return [(m, t, g["in"], g["out"]) for (m, t), g in groups.items()]


CLUB_PREFIXES = ("FK", "NK", "HŠK", "HNK", "OFK", "NŠK")
ACRONYMS = {"BSK", "GOŠK", "TOŠK", "SAŠK", "OFK"}  # club names written in capitals


def short(team):
    """'FK ŽELJEZNIČAR' -> 'Željezničar', 'BSK' -> 'BSK', 'NK TOŠK TEŠANJ' -> 'TOŠK Tešanj'."""
    words = team.split()
    if len(words) > 1 and words[0].upper() in CLUB_PREFIXES:
        words = words[1:]

    def fix(w):
        if w.upper() in ACRONYMS:
            return w.upper()
        return "-".join(p.capitalize() for p in w.split("-"))

    return " ".join(fix(w) for w in words)


def short_names(players):
    """Surname only ("Kyeremeh"), full name if two players share a surname."""
    first = [p["name"].split()[0] for p in players]
    return {p["name"]: (p["name"] if first.count(f) > 1 else f)
            for p, f in zip(players, first)}


def team_block(team_name, players, events, all_players=None):
    """Text for one team: goals, cards, substitutions."""
    nm = short_names(all_players or players)
    # Own goals count for the other side, so show them in the opponent's column
    ev = [e for e in events
          if (e["team"] == team_name) != (e["kind"] == "own_goal")]
    lines = []

    for e in ev:
        if e["kind"] in ("goal", "penalty", "own_goal"):
            tag = " (pen.)" if e["kind"] == "penalty" else " (ag)" if e["kind"] == "own_goal" else ""
            lines.append(f"⚽ **{nm.get(e['player'], e['player'])}** {e['minute'] or '?'}'{tag}")

    card_icon = {"yellow": "🟨", "second_yellow": "🟨🟥", "red": "🟥"}
    for e in ev:
        if e["kind"] in card_icon:
            lines.append(f"{card_icon[e['kind']]} {nm.get(e['player'], e['player'])} {e['minute'] or '?'}'")

    subs = group_subs(ev)
    if subs:
        if lines:
            lines.append("")
        for minute, _, ins, outs in subs:
            i = ", ".join(nm.get(x, x) for x in ins) or "?"
            o = ", ".join(nm.get(x, x) for x in outs) or "?"
            lines.append(f"🔄 {minute or '?'}' ⬆️ {i}\n      ⬇️ {o}")

    return "\n".join(lines) or "—"


def lineup_block(players):
    """Starting XI one per line with shirt numbers; bench below.
    Bench players who came on are in bold."""
    nm = short_names(players)

    def label(p):
        txt = f"`{p['no']:>2}` {nm[p['name']]}"
        if "GK" in p["flags"]:
            txt += " (GK)"
        if "C" in p["flags"]:
            txt += " (C)"
        return txt

    xi = [label(p) for p in players if not p["bench"]]
    bench = []
    for p in players:
        if p["bench"]:
            came_on = any(k == "sub" for k, _, _ in p["events"])
            bench.append(f"**{label(p)}**" if came_on else label(p))
    text = "\n".join(xi) or "—"
    if bench:
        text += "\n\n*Klupa*\n" + "\n".join(bench)
    return text


def build_embed(match, url):
    """Discord embed: score in title, one column per team, info in footer."""
    ev = build_events(match)
    info = match["info"]
    home, away = short(match["home"]), short(match["away"])
    hg, ag = int(match["ft"][0]), int(match["ft"][1])

    # Colour from Zeljo's point of view: green win, red loss, grey draw
    zeljo_home = OUR_TEAM in match["home"].upper()
    ours, theirs = (hg, ag) if zeljo_home else (ag, hg)
    color = 0x2E9E4F if ours > theirs else 0xD13B3B if ours < theirs else 0x8A8F98

    desc = [f"Poluvrijeme {match['ht'][0]}:{match['ht'][1]}"]
    if info.get("extra_time"):
        desc[0] += " · nakon produžetaka"
    if info.get("penalties"):
        desc[0] += f" · {info['penalties']}"
    meta = [x for x in (info["competition"], info["date"]) if x]
    if meta:
        desc.append(" · ".join(meta))

    fields = []
    for team_name, players in match["teams"]:
        fields.append({"name": short(team_name), "value": team_block(team_name, players, ev,
                       [p for _, ps in match["teams"] for p in ps])[:1024],
                       "inline": True})

    # Lineups in a second row of columns; a full-width field forces the new row
    if any(players for _, players in match["teams"]):
        fields.append({"name": "📋 Sastavi", "value": "\u200b", "inline": False})
        for team_name, players in match["teams"]:
            fields.append({"name": short(team_name), "value": lineup_block(players)[:1024],
                           "inline": True})

    officials = []
    if info["referee"]:
        officials.append(f"Sudija: **{info['referee']}**")
    assistants = " · ".join(f"{k}: {info[k.lower()]}" for k in ("A1", "A2") if info[k.lower()])
    if assistants:
        officials.append(assistants)
    if info["delegate"]:
        officials.append(f"Delegat: {info['delegate']}")
    if info["observer"]:
        officials.append(f"Posmatrač suđenja: {info['observer']}")
    if officials:
        fields.append({"name": "🧑‍⚖️ Službena lica", "value": "\n".join(officials), "inline": False})

    unknown = sum(1 for e in ev if e["kind"] == "unknown")
    if unknown:
        fields.append({"name": "⚠️", "value": f"Neprepoznato ikonica: {unknown}", "inline": False})

    footer = []
    if info["stadium"]:
        footer.append(info["stadium"])
    if info["attendance"]:
        footer.append(f"{info['attendance']} gledalaca")
    if info["status"]:
        footer.append(info["status"])

    return {
        "title": f"{home} {hg} : {ag} {away}",
        "url": url,
        "description": "\n".join(desc),
        "color": color,
        "fields": fields,
        "footer": {"text": " · ".join(footer)} if footer else None,
    }


def embed_to_text(embed):
    """Plain-text preview of the embed for the console."""
    out = [embed["title"], embed["description"], ""]
    for f in embed["fields"]:
        out += [f"[{f['name']}]", f["value"], ""]
    if embed.get("footer"):
        out.append(embed["footer"]["text"])
    out.append(embed["url"])
    return "\n".join(out)


def post_discord(embed):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        print("DISCORD_WEBHOOK_URL not set, skipping post")
        return
    embed = {k: v for k, v in embed.items() if v is not None}
    for _ in range(5):
        r = requests.post(hook, json={"embeds": [embed]}, timeout=30)
        if r.status_code == 429:  # rate limited: wait as Discord says, then retry
            time.sleep(float(r.json().get("retry_after", 5)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def debug_dump(match):
    for team_name, players in match["teams"]:
        print(f"\n== {team_name} ==")
        for p in players:
            for kind, minute, sig in p["events"]:
                where = "klupa" if p["bench"] else "start"
                print(f"{p['name']:<32} {where:<6} {minute or '?':>5}'  {kind:<14} sig: {sig}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def match_datetime(match):
    """Kick-off time from the 'Datum' field, for chronological order."""
    try:
        return datetime.strptime(match["info"]["date"] or "", "%d.%m.%Y %H:%M")
    except ValueError:
        return datetime.max


def find_candidates():
    """(match_id, [urls to try]) for league and cup matches involving Zeljo."""
    out = {}
    # League: team page links look like /stats/match/<id>-<home>-<away>/.
    # The page also lists other clubs' matches of the round, skip those by slug.
    try:
        for mid, slug in re.findall(r"/stats/match/(\d+)(?:-([\w-]+))?/", fetch(TEAM_PAGE)):
            if slug and "zeljeznicar" not in slug:
                continue
            urls = [f"{BASE}/stats/match/{mid}-{slug}/"] if slug else []
            out.setdefault(mid, urls + [f"{BASE}/stats/match/{mid}/"])
    except requests.RequestException as e:
        print("league page failed:", e)
    # Cup: links look like kup-bih?view=match&id=<id>%3A<slug>
    try:
        html = fetch(CUP_PAGE)
        for mid, slug in re.findall(r"view=match(?:&amp;|&)id=(\d+)(?:%3A|:)([\w-]+)", html):
            if "zeljeznicar" in slug:
                out.setdefault(mid, [
                    f"{BASE}/stats/match/{mid}-{slug}/",
                    f"{BASE}/stats/match/{mid}/",
                    f"{BASE}/takmicenja/nogomet-muskarci/kup-bih?view=match&id={mid}%3A{slug}",
                ])
    except requests.RequestException as e:
        print("cup page failed:", e)
    return list(out.items())


def page_diag(html):
    """Short description of a page, to see in the log why lineups are missing."""
    return (f"{len(html)} bytes, {html.count('<table')} tables, "
            f"{html.count('/stats/player/')} player links")


def fetch_match(urls):
    """Return ("played", url, match), ("upcoming", url, match) or None.

    The site sometimes returns a page with the score but without lineups,
    so a played page without lineups is fetched again before giving up.
    """
    for url in urls:
        for attempt in (1, 2):
            try:
                html = fetch(url)
                match = parse_match(html)
            except requests.RequestException as e:
                print(f"SKIP {url}: page error ({e})")
                break
            except ValueError as e:
                print(f"SKIP {url}: {e}")
                break
            if (match["info"]["status"] or "").strip().lower() != "odigrana":
                return ("upcoming", url, match)  # not played yet, nothing to log
            if match["teams"]:
                return ("played", url, match)
            print(f"no lineups on {url} (attempt {attempt}): {page_diag(html)}")
            if attempt == 1:
                time.sleep(15)
    return None


def run_auto():
    """Post every played Zeljo match from the team page that was not posted yet.

    On the first run (empty state) this posts all matches played so far,
    one message per match, oldest first.
    """
    posted = load_state()
    new_matches = []
    for mid, urls in find_candidates():
        if mid in posted:
            continue
        found = fetch_match(urls)
        time.sleep(2)  # be gentle with the site between match pages
        if not found:
            print(f"SKIP match {mid}: page not readable now, will retry next run")
            continue
        state, url, match = found
        if state != "played":
            continue  # upcoming match
        if OUR_TEAM not in (match["home"] + match["away"]).upper():
            continue
        new_matches.append((match_datetime(match), mid, url, match))

    new_matches.sort(key=lambda x: x[0])
    for _, mid, url, match in new_matches:
        embed = build_embed(match, url)
        print(embed_to_text(embed), "\n")
        post_discord(embed)
        # Save after every post so a failure halfway never re-posts old matches
        player_stats.record_match(mid, match, OUR_TEAM)
        posted.add(mid)
        save_state(posted)
        time.sleep(3)  # stay under Discord webhook rate limit
        # Stats of this match's competition right after its report (league
        # after league games, cup after cup games); matches go oldest first,
        # so each table shows totals up to and including this match
        post_stats(player_stats.competition_of(match))
        time.sleep(3)
    save_state(posted)


def post_stats(competition=None):
    """Post the stats table of one competition, or of both when None."""
    for comp in ([competition] if competition else ["liga", "kup"]):
        embed = player_stats.build_stats_embed(comp)
        if not embed:
            continue  # no matches in this competition yet
        print(embed_to_text({"title": embed["title"], "description": embed["description"],
                             "fields": embed["fields"], "footer": embed["footer"], "url": ""}))
        post_discord(embed)
        time.sleep(2)


def rebuild_stats():
    """Re-read every posted match and recompute stats (e.g. after an icon fix)."""
    for mid in sorted(load_state()):
        found = fetch_match([f"{BASE}/stats/match/{mid}/"])
        time.sleep(2)
        if found and found[0] == "played":
            player_stats.record_match(mid, found[2], OUR_TEAM)
            print("ok", found[1])
        else:
            # Keep the stats we already have for this match rather than wiping them
            print(f"skip {mid}: page not readable now, old stats kept")


def main():
    args = sys.argv[1:]
    if "--auto" in args:
        run_auto()
        return
    if "--rebuild-stats" in args:
        rebuild_stats()
        if "--dry-run" not in args:
            post_stats()
        return
    if "--stats" in args:  # post current league and cup stats now
        post_stats()
        return
    urls = [a for a in args if a.startswith("http")]
    if not urls:
        print(__doc__)
        sys.exit(1)
    html = fetch(urls[0])
    if "--html" in args:  # raw HTML of player cells that have minutes
        soup = BeautifulSoup(html, "html.parser")
        shown = 0
        for td in soup.find_all("td"):
            prev = td.find_previous_sibling("td")
            if (prev and re.fullmatch(r"\d{1,3}", prev.get_text(strip=True))
                    and MINUTE_RE.search(td.get_text())):
                print(str(td.find_parent("tr") or td), "\n")
                shown += 1
                if shown >= 6:
                    break
        return
    match = parse_match(html)
    if not match["teams"] and "--debug" not in args:
        # The site sometimes leaves the lineups out; try once more
        found = fetch_match([urls[0]])
        if not found or not found[2]["teams"]:
            print(f"No lineups on {urls[0]} ({page_diag(html)}). Try again later.")
            sys.exit(1)
        match = found[2]
    if "--debug" in args:
        debug_dump(match)
        return
    embed = build_embed(match, urls[0])
    print(embed_to_text(embed))
    if "--dry-run" in args:
        return
    post_discord(embed)
    # A manually posted match counts like an automatic one: remember it,
    # add it to the stats and post that competition's stats table
    mid = re.search(r"/stats/match/(\d+)", urls[0]) or re.search(r"id=(\d+)", urls[0])
    if mid and OUR_TEAM in (match["home"] + match["away"]).upper():
        posted = load_state()
        posted.add(mid.group(1))
        save_state(posted)
        player_stats.record_match(mid.group(1), match, OUR_TEAM)
        time.sleep(3)
        post_stats(player_stats.competition_of(match))


if __name__ == "__main__":
    main()
