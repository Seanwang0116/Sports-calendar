#!/usr/bin/env python3
"""Build subscribable .ics calendars from ESPN schedules.

Runs inside GitHub Actions (see .github/workflows/update-calendar.yml).
Standard library only, so nothing needs to be installed.

Usage: python3 build_ics.py --config calendar-config.json --out _site/calendar
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("ESPN_API", "https://site.api.espn.com/apis/site/v2/sports")

# Keep in sync with LEAGUES in index.html.
LEAGUES = {
    "nba":    dict(name="NBA",  icon="🏀", sport="basketball", league="nba",            kind="us",     dur=150),
    "mlb":    dict(name="MLB",  icon="⚾", sport="baseball",   league="mlb",            kind="us",     dur=180),
    "f1":     dict(name="F1",   icon="🏁", sport="racing",     league="f1",             kind="race",   dur=120),
    "epl":    dict(name="英超", icon="⚽", sport="soccer",     league="eng.1",          kind="soccer", dur=120),
    "laliga": dict(name="西甲", icon="⚽", sport="soccer",     league="esp.1",          kind="soccer", dur=120),
    "ucl":    dict(name="歐冠", icon="⚽", sport="soccer",     league="uefa.champions", kind="soccer", dur=120),
    "seriea": dict(name="義甲", icon="⚽", sport="soccer",     league="ita.1",          kind="soccer", dur=120),
    "bundes": dict(name="德甲", icon="⚽", sport="soccer",     league="ger.1",          kind="soccer", dur=120),
    "nfl":    dict(name="NFL",  icon="🏈", sport="football",   league="nfl",            kind="us",     dur=210),
    "nhl":    dict(name="NHL",  icon="🏒", sport="hockey",     league="nhl",            kind="us",     dur=150),
}

CHUNK_DAYS = 14  # ESPN rejects long date ranges; stay well under its limit
PAUSE = 0.5      # seconds between requests; ESPN's CDN blocks bursts with 403 for several minutes

# The web page reads calendar/data/*.json instead of calling ESPN, because the
# iOS home-screen app blocks cross-origin requests. This window is what the
# page can browse without ESPN.
DATA_DAYS_BACK = 70   # enough for the whole previous month's grid
DATA_DAYS_AHEAD = 150


# ---------------------------------------------------------------- fetching

_lock = threading.Lock()
_next_at = 0.0


def throttle():
    global _next_at
    with _lock:
        now = time.monotonic()
        wait = _next_at - now
        _next_at = max(now, _next_at) + PAUSE
    if wait > 0:
        time.sleep(wait)


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sports-calendar/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if 400 <= e.code < 500 and e.code not in (403, 429):
                raise  # a bad request will not get better by retrying
        except Exception as e:  # network hiccup, timeout, bad JSON
            last = e
        time.sleep(2 * (i + 1))
    raise last


def ymd(d):
    return d.strftime("%Y%m%d")


def board_url(lg, dates):
    return f"{API}/{lg['sport']}/{lg['league']}/scoreboard?dates={dates}"


def fetch_chunk(lg, start, end):
    """One range request; if ESPN refuses it, fall back to one request per day."""
    try:
        return [get_json(board_url(lg, f"{ymd(start)}-{ymd(end)}") + "&limit=500")]
    except Exception:
        pass
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

    def one(d):
        try:
            return get_json(board_url(lg, ymd(d)))
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(one, days))
    boards = [r for r in results if not isinstance(r, Exception)]
    if not boards:
        raise results[0]
    return boards


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch_league(lg, start, end):
    """One request per month (dates=YYYYMM). ESPN currently refuses day ranges, so falling
    back to them means one request per day, which is slow and gets us rate-limited."""
    boards = []
    for y, m in months(start, end):
        try:
            boards.append(get_json(board_url(lg, f"{y}{m:02d}") + "&limit=1000"))
            continue
        except Exception:
            pass
        first = max(start, dt.date(y, m, 1))
        last = min(end, dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1))
        s = first
        while s <= last:
            e = min(s + dt.timedelta(days=CHUNK_DAYS - 1), last)
            boards.extend(fetch_chunk(lg, s, e))
            s = e + dt.timedelta(days=1)
    return boards


def fetch_teams(lg):
    for q in ("", "?limit=300"):
        try:
            j = get_json(f"{API}/{lg['sport']}/{lg['league']}/teams{q}")
        except Exception:
            continue
        rows = (((j.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams")) or []
        teams = [(r or {}).get("team") or {} for r in rows]
        teams = [dict(id=str(t["id"]), name=t.get("displayName") or t.get("name") or "",
                      abbr=t.get("abbreviation") or "") for t in teams if t.get("id")]
        if teams:
            return teams
    return []


# ---------------------------------------------------------------- page data

def pick(d, keys):
    return {k: d[k] for k in keys if isinstance(d, dict) and d.get(k) is not None}


def slim_status(s):
    return {"type": pick((s or {}).get("type") or {}, ("name", "state"))}


def slim_event(ev, kind):
    """Keep only the ESPN fields that normalize() in index.html reads."""
    out = pick(ev, ("id", "date", "name", "shortName"))
    if ev.get("status"):
        out["status"] = slim_status(ev["status"])
    for k in ("circuit", "venue"):
        if ev.get(k):
            out[k] = pick(ev[k], ("fullName",))
    comps = []
    for c in ev.get("competitions") or []:
        sc = pick(c, ("id", "date", "startDate"))
        if c.get("type"):
            sc["type"] = pick(c["type"], ("abbreviation", "text"))
        if c.get("status"):
            sc["status"] = slim_status(c["status"])
        if c.get("venue"):
            sc["venue"] = pick(c["venue"], ("fullName",))
        if kind != "race":  # F1 competitors are drivers; the page does not use them
            cs = []
            for x in c.get("competitors") or []:
                sx = pick(x, ("homeAway",))
                score = x.get("score")
                if isinstance(score, dict):
                    score = score.get("displayValue", score.get("value"))
                if score is not None:
                    sx["score"] = str(score)
                if x.get("team"):
                    sx["team"] = pick(x["team"], ("id", "displayName", "name", "shortDisplayName", "abbreviation"))
                cs.append(sx)
            sc["competitors"] = cs
        comps.append(sc)
    out["competitions"] = comps
    return out


def write_data(path, lg, boards, start, end, now):
    seen, events, cal = set(), [], {}
    for b in boards:
        for ev in b.get("events") or []:
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            events.append(slim_event(ev, lg["kind"]))
        if lg["kind"] == "race":
            for it in ((b.get("leagues") or [{}])[0].get("calendar") or []):
                if isinstance(it, dict) and it.get("startDate"):
                    cal[it["startDate"]] = pick(it, ("label", "startDate"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(updated=now.isoformat(), range=[ymd(start), ymd(end)],
                       events=events, calendar=list(cal.values())),
                  f, ensure_ascii=False, separators=(",", ":"))
    return len(events)


# ---------------------------------------------------------------- normalizing

def parse_time(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if len(s) == 22 and s[16] == "+":  # "2026-10-13T23:30+00:00" (no seconds)
        s = s[:16] + ":00" + s[16:]
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def team(c):
    t = (c or {}).get("team") or {}
    score = (c or {}).get("score")
    if isinstance(score, dict):
        score = score.get("displayValue", score.get("value"))
    return dict(
        id=str(t.get("id", "")),
        name=t.get("displayName") or t.get("name") or "待定",
        short=t.get("shortDisplayName") or t.get("displayName") or "待定",
        score="" if score is None else str(score),
    )


def session_label(raw):
    s = (raw or "").lower()
    if re.search(r"fp ?1|practice 1", s): return "一練", False
    if re.search(r"fp ?2|practice 2", s): return "二練", False
    if re.search(r"fp ?3|practice 3", s): return "三練", False
    if re.search(r"shootout|sprint qual|\bss\b|\bsq\b", s): return "衝刺排位", True
    if re.search(r"sprint|\bsr\b", s): return "衝刺賽", True
    if "qual" in s: return "排位賽", True
    if "race" in s: return "正賽", True
    return (raw or "場次").strip() or "場次", True


def normalize(key, lg, ev):
    out = []
    if lg["kind"] == "race":
        gp = ev.get("shortName") or ev.get("name") or "Grand Prix"
        venue = (ev.get("circuit") or {}).get("fullName") or ""
        comps = ev.get("competitions") or []
        if not comps:
            comps = [dict(id="event", date=ev.get("date"), type={"text": "Race"})]
        for c in comps:
            ty = c.get("type") or {}
            label, main = session_label(f"{ty.get('abbreviation', '')} {ty.get('text', '')}")
            start = parse_time(c.get("date") or c.get("startDate") or ev.get("date"))
            if not start:
                continue
            state = ((c.get("status") or {}).get("type") or {}).get("state")
            out.append(dict(
                uid=f"{key}-{ev.get('id')}-{c.get('id')}",
                start=start, minutes=120 if label == "正賽" else 60,
                summary=f"{lg['icon']} {lg['name']}｜{gp} {label}",
                description=f"{ev.get('name') or gp}・{label}" + ("\n已結束" if state == "post" else ""),
                location=venue, main=main, teams=set(), cancelled=False,
            ))
        return out

    c = (ev.get("competitions") or [{}])[0]
    cs = c.get("competitors") or []
    h = team(next((x for x in cs if x.get("homeAway") == "home"), cs[0] if cs else None))
    a = team(next((x for x in cs if x.get("homeAway") == "away"), cs[1] if len(cs) > 1 else None))
    st = ((c.get("status") or ev.get("status") or {}).get("type")) or {}
    name = st.get("name") or ""
    start = parse_time(ev.get("date") or c.get("date"))
    if not start:
        return out
    note = ""
    if "POSTPONED" in name:
        note = "（延期）"
    elif "CANCEL" in name:
        note = "（取消）"
    desc = [f"{a['name']} @ {h['name']}"]
    if st.get("state") == "post" and h["score"] and a["score"] and not note:
        desc.append(f"比分（客–主）：{a['score']}–{h['score']}")
    venue = (c.get("venue") or {}).get("fullName") or ""
    out.append(dict(
        uid=f"{key}-{ev.get('id')}",
        start=start, minutes=lg["dur"],
        summary=f"{lg['icon']} {lg['name']}｜{a['short']} @ {h['short']}{note}",
        description="\n".join(desc), location=venue, main=True,
        teams={t for t in (h["id"], a["id"]) if t}, cancelled="CANCEL" in name,
    ))
    return out


def calendar_fallback(key, lg, boards, start, end):
    """F1 off-weekends: ESPN may list only the season calendar. Use it as all-day entries."""
    out = []
    for b in boards:
        for it in ((b.get("leagues") or [{}])[0].get("calendar") or []):
            if not isinstance(it, dict) or not it.get("startDate"):
                continue
            t = parse_time(it["startDate"])
            if not t or not (start <= t.date() <= end):
                continue
            label = it.get("label") or "Grand Prix"
            out.append(dict(uid=f"{key}-cal-{t.date().isoformat()}", start=t, minutes=0, all_day=True,
                            summary=f"{lg['icon']} {lg['name']}｜{label} 週末", description="場次時間尚未公布",
                            location="", main=True, teams=set(), cancelled=False))
    return out


# ---------------------------------------------------------------- writing .ics

def esc(s):
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line):
    """RFC 5545: max 75 octets per line, continuation lines start with a space."""
    b = line.encode("utf-8")
    parts = []
    limit = 75
    while len(b) > limit:
        cut = limit
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # never split a UTF-8 character
            cut -= 1
        parts.append(b[:cut])
        b = b[cut:]
        limit = 74  # the leading space counts toward the 75
    parts.append(b)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def stamp(t):
    return t.strftime("%Y%m%dT%H%M%SZ")


def write_ics(path, cal_name, events, now):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//my-sports-calendar//ZH-TW",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(cal_name)}", "X-WR-TIMEZONE:Asia/Taipei",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for e in sorted(events, key=lambda x: x["start"]):
        lines += ["BEGIN:VEVENT", f"UID:{esc(e['uid'])}@my-sports-calendar", f"DTSTAMP:{stamp(now)}"]
        if e.get("all_day"):
            d = e["start"].date()
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=3):%Y%m%d}"]
        else:
            lines += [f"DTSTART:{stamp(e['start'])}",
                      f"DTEND:{stamp(e['start'] + dt.timedelta(minutes=e['minutes']))}"]
        lines.append(f"SUMMARY:{esc(e['summary'])}")
        if e.get("description"):
            lines.append(f"DESCRIPTION:{esc(e['description'])}")
        if e.get("location"):
            lines.append(f"LOCATION:{esc(e['location'])}")
        if e.get("cancelled"):
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="calendar-config.json")
    ap.add_argument("--out", default="_site/calendar")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(args.out, exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.date()
    start = today - dt.timedelta(days=int(cfg.get("days_back", 7)))
    end = today + dt.timedelta(days=int(cfg.get("days_ahead", 90)))

    data_start = min(start, today - dt.timedelta(days=DATA_DAYS_BACK))
    data_end = max(end, today + dt.timedelta(days=DATA_DAYS_AHEAD))
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)

    # Fetch every league once over the wider page-data window; the .ics files use a slice of it.
    # Subscribed leagues go first, so if ESPN starts refusing us only the page-only extras suffer.
    wanted = cfg.get("leagues") or {}
    fetched = {}
    for key in sorted(LEAGUES, key=lambda k: k not in wanted):
        try:
            fetched[key] = fetch_league(LEAGUES[key], data_start, data_end), None
        except Exception as e:
            fetched[key] = None, e
    teams_by_league = {k: fetch_teams(lg) for k, lg in LEAGUES.items() if lg["kind"] != "race"}

    for key, lg in LEAGUES.items():
        boards, err = fetched[key]
        if boards is not None:
            n = write_data(os.path.join(data_dir, f"{key}.json"), lg, boards, data_start, data_end, now)
            print(f"  網頁資料 {lg['name']}：{n} 場")
        elif key not in wanted:
            # Not in the subscription: the page falls back to ESPN for it, so just warn.
            print(f"  網頁資料 {lg['name']} 抓取失敗：{err!r}", file=sys.stderr)
    with open(os.path.join(data_dir, "teams.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in teams_by_league.items() if v}, f, ensure_ascii=False, separators=(",", ":"))

    all_events, status, failed = [], {}, []
    lo, hi = start - dt.timedelta(days=1), end + dt.timedelta(days=1)
    for key, conf in wanted.items():
        lg = LEAGUES.get(key)
        if not lg:
            print(f"略過未知聯賽：{key}", file=sys.stderr)
            continue
        conf = conf or {}
        boards, e = fetched[key]
        if boards is None:
            print(f"✗ {lg['name']} 抓取失敗：{e!r}", file=sys.stderr)
            failed.append(lg["name"])
            status[key] = dict(name=lg["name"], ok=False, error=str(e))
            continue

        seen, events = set(), []
        for b in boards:
            for ev in b.get("events") or []:
                try:
                    for item in normalize(key, lg, ev):
                        if item["uid"] not in seen:
                            seen.add(item["uid"])
                            events.append(item)
                except Exception as e:
                    print(f"  略過一場 {key} 比賽：{e!r}", file=sys.stderr)
        events = [e for e in events if lo <= e["start"].date() <= hi]
        if lg["kind"] == "race" and not events:
            events = calendar_fallback(key, lg, boards, start, end)

        teams = {str(t) for t in conf.get("teams") or []}
        if teams:
            events = [e for e in events if e["teams"] & teams]
        if lg["kind"] == "race" and conf.get("main_sessions_only", True):
            events = [e for e in events if e["main"]]

        write_ics(os.path.join(args.out, f"{key}.ics"), f"{lg['name']} 賽程", events, now)
        all_events += events
        status[key] = dict(name=lg["name"], ok=True, events=len(events))
        print(f"✓ {lg['name']}：{len(events)} 場")

    write_ics(os.path.join(args.out, "all.ics"), cfg.get("calendar_name", "我的賽程"), all_events, now)
    with open(os.path.join(args.out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(dict(updated=now.isoformat(), range=[start.isoformat(), end.isoformat()],
                       total=len(all_events), leagues=status), f, ensure_ascii=False, indent=2)
    print(f"共 {len(all_events)} 場，範圍 {start} ~ {end}")

    if failed:
        # Fail the run so GitHub keeps the previous (complete) calendar online and emails you.
        print(f"以下聯賽抓取失敗，這次不更新：{'、'.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
