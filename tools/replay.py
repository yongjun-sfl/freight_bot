"""Replay a day of real Telegram messages through the bot, then compare the
legs it produces against the dispatcher's hand-entered log.

This is the check before trusting the bot in production: not "do the tests
pass" but "does a real working day come out looking like what a human
recorded". It writes to a scratch database and never touches production.

    docker compose -f docker-compose.yml -f docker-compose.test.yml \
        run --rm replay

Options come from the environment:
    REPLAY_SHEET=08282026     which day of the workbook is the truth
    REPLAY_PHOTOS=1           also run vision on photos (slow, costs more)
    REPLAY_DRIVER="JOHN SHIM" limit to one driver
    REPLAY_DEBUG=1             per-driver mismatch diagnostics (recommended!)
"""

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/src/bot")
sys.path.insert(0, "/tools")

import aiomysql

import ai_engine
import load_types
from ground_truth import load_day
from routes import refresh_route_cache, seed_default_routes
from schema_ddl import apply_schema
from seed_network import read_drivers_csv, seed_drivers, seed_network
from state_machine import commit_trip_leg

EXPORT = os.getenv("REPLAY_EXPORT", "/data/ChatExport_2026-08-29/result.json")
WORKBOOK = os.getenv("REPLAY_WORKBOOK", "/data/SFL Log 08_2026.xlsx")
SHEET = os.getenv("REPLAY_SHEET", "08282026")
WITH_PHOTOS = os.getenv("REPLAY_PHOTOS") == "1"
ONLY_DRIVER = (os.getenv("REPLAY_DRIVER") or "").upper() or None
DB = "replay_scratch"
MATCH_WINDOW_MIN = 105  # 1h45m - covers lunch breaks and delayed batch reporting
REPLAY_DEBUG = os.getenv("REPLAY_DEBUG") == "1"
EXPORT_DIR = os.path.dirname(EXPORT)


def message_text(m):
    t = m.get("text")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in t)
    return ""


async def build_scratch(password):
    # Emptied at the START of a run, never at the end: the point of the scratch
    # database is to still be there afterwards, so the legs the bot produced can
    # be read row by row instead of only through this script's summary.
    conn = await aiomysql.connect(host="mysql_db", user="root",
                                  password=password, autocommit=True)
    async with conn.cursor() as cur:
        await cur.execute(f"DROP DATABASE IF EXISTS {DB};")
        await cur.execute(f"CREATE DATABASE {DB};")
    conn.close()

    pool = await aiomysql.create_pool(
        host="mysql_db", user="root", password=password, db=DB, autocommit=True,
        init_command="SET time_zone = 'America/New_York';", minsize=1, maxsize=4)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await apply_schema(cur, DB)
            await seed_drivers(cur)
            await seed_default_routes(cur)
            await seed_network(cur)
    await ai_engine.refresh_location_cache(pool)
    await refresh_route_cache(pool)
    load_types.load_lane_map()
    return pool


async def replay(pool):
    export = json.load(open(EXPORT, encoding="utf-8"))
    roster = {str(r["user_id"]): r for r in read_drivers_csv() if r["user_id"]}

    messages = [m for m in export.get("messages", []) if m.get("type") == "message"]
    messages.sort(key=lambda m: m.get("date", ""))

    loop = asyncio.get_running_loop()
    processed = skipped = 0
    for m in messages:
        # A driver's report forwarded into the channel by someone else belongs
        # to the ORIGINAL driver, not the person who forwarded it. Telegram
        # Desktop exports carry the original in forwarded_from_id.
        sender = None
        for raw_id in (m.get("forwarded_from_id"), m.get("from_id")):
            candidate = str(raw_id or "").replace("user", "")
            if candidate in roster:
                sender = candidate
                break
        driver = roster.get(sender) if sender else None
        if not driver:
            skipped += 1
            continue
        if ONLY_DRIVER and driver["name_eng"].upper() != ONLY_DRIVER:
            continue

        text = message_text(m).strip()
        stamp = m.get("date", "").replace("T", " ")[:19]
        has_photo = bool(m.get("photo"))

        if has_photo and WITH_PHOTOS:
            path = os.path.join(EXPORT_DIR, m["photo"])
            try:
                image = open(path, "rb").read()
            except OSError:
                image = None
            intent = (await ai_engine.prepare_image_intent([image], text, loop)
                      if image else await ai_engine.prepare_text_intent(text))
        elif text:
            # Captions drive the case; the image only adds trailer and BOL.
            intent = await ai_engine.prepare_text_intent(text)
        else:
            skipped += 1
            continue

        try:
            await commit_trip_leg(
                p=pool, did=int(sender), user_name=driver["name_eng"],
                group_title="replay", orig_chat_id=0, orig_msg_id=m.get("id", 0),
                msg_timestamp=stamp, intent=intent)
            processed += 1
        except Exception as e:
            print(f"    ! {stamp} {driver['name_eng']}: {e}")

        if processed % 25 == 0 and processed:
            print(f"    ... {processed} messages", flush=True)
    return processed, skipped


async def bot_legs(pool):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Include positioning legs: the dispatcher's sheet records yard
            # moves and same-site moves (ILPYO yard move, John Shim E2F->E2R),
            # so they are matchable truth even though they are not billable
            # trips. Clock In rows are filtered from the manual side instead.
            await cur.execute("""
                SELECT l.*, d.name_eng AS driver
                  FROM shuttle_legs l JOIN driver_profiles d ON d.user_id = l.user_id
              ORDER BY d.name_eng, l.departure_time;""")
            return list(await cur.fetchall())


def hhmm(value):
    return value.strftime("%H:%M") if isinstance(value, datetime) else None


def minutes(text):
    match = re.match(r"^(\d{1,2}):(\d{2})", text or "")
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def site(code, sites):
    return sites.get((code or "").upper(), (code or "").upper())


def _why_rejected(leg, row, sites, want, got, gap, best_gap):
    """One-line reason a candidate leg was not chosen for a manual row.
    Used by REPLAY_DEBUG to expose *why* a match did not happen, not just
    that it failed."""
    if site(leg["origin_location"], sites) != site(row["origin"], sites):
        return f"origin mismatch (bot {site(leg['origin_location'], sites)} vs manual {site(row['origin'], sites)})"
    if site(leg["destination_location"], sites) != site(row["destination"], sites):
        return f"destination mismatch (bot {site(leg['destination_location'], sites)} vs manual {site(row['destination'], sites)})"
    if want is None:
        return "manual depart time unparseable"
    if got is None:
        return "bot depart time unparseable"
    if gap > MATCH_WINDOW_MIN:
        return f"depart {row['depart']} vs bot {hhmm(leg['departure_time'])} gap {gap}min (>{MATCH_WINDOW_MIN})"
    if best_gap is not None and gap >= best_gap:
        return f"depart gap {gap}min >= a better candidate's {best_gap}min"
    return "unknown"


def compare(manual, bot, sites):
    """Greedy match on driver, both endpoints, and departure within the window.

    When REPLAY_DEBUG is set, also returns debug rows: for each unmatched
    manual row, the closest bot candidate and the reason it was rejected.
    """
    by_driver = defaultdict(list)
    for leg in bot:
        by_driver[leg["driver"].upper()].append(leg)

    matched, missing, debug = [], [], []
    used = set()
    for row in manual:
        candidates = by_driver.get(row["driver"], [])
        want = minutes(row["depart"])
        best, best_gap = None, None
        closest, closest_gap, closest_reason = None, None, None
        for leg in candidates:
            if id(leg) in used:
                continue
            got = minutes(hhmm(leg["departure_time"]))
            gap = abs(got - want) if (want is not None and got is not None) else None
            if gap is not None and (closest_gap is None or gap < closest_gap):
                closest, closest_gap = leg, gap
            if site(leg["origin_location"], sites) != site(row["origin"], sites):
                continue
            if site(leg["destination_location"], sites) != site(row["destination"], sites):
                continue
            if want is None or got is None:
                continue
            if gap <= MATCH_WINDOW_MIN and (best_gap is None or gap < best_gap):
                best, best_gap = leg, gap
        if best:
            used.add(id(best))
            matched.append((row, best, best_gap))
        else:
            missing.append(row)
            if REPLAY_DEBUG and candidates:
                # If no candidate even passed endpoint checks, show the one
                # that was nearest in time so the operator can see whether
                # a small endpoint typo or a long delay is the real cause.
                if closest is not None:
                    reason = _why_rejected(closest, row, sites, want,
                                            minutes(hhmm(closest["departure_time"])),
                                            closest_gap, best_gap)
                    debug.append((row, closest, reason))

    extra = [l for l in bot if id(l) not in used]
    return matched, missing, extra, debug


async def main():
    password = os.environ["TEST_MYSQL_PASSWORD"]
    print(f"  replaying {SHEET}  photos={'on' if WITH_PHOTOS else 'off'}"
          + (f"  driver={ONLY_DRIVER}" if ONLY_DRIVER else ""))
    pool = await build_scratch(password)

    processed, skipped = await replay(pool)
    print(f"\n  {processed} messages replayed, {skipped} skipped\n")

    manual = load_day(WORKBOOK, SHEET)
    # A "Clock In" row is a shift event, not a shuttle leg; the bot stores it in
    # driver_shifts. Everything else in the sheet (including yard moves) is
    # matched now that bot positioning legs are part of the comparison.
    manual = [r for r in manual if (r["transaction"] or "").upper() != "CLOCK IN"]
    if ONLY_DRIVER:
        manual = [r for r in manual if r["driver"] == ONLY_DRIVER]
    bot = await bot_legs(pool)
    sites = dict(ai_engine.LOCATION_CACHE.get("site_map") or {})

    matched, missing, extra, debug = compare(manual, bot, sites)

    print(f"  {'DRIVER':20} {'MANUAL':>6} {'BOT':>5} {'MATCHED':>8}   RATE")
    print("  " + "-" * 52)
    drivers = sorted({r["driver"] for r in manual} | {l["driver"].upper() for l in bot})
    for name in drivers:
        m = sum(1 for r in manual if r["driver"] == name)
        b = sum(1 for l in bot if l["driver"].upper() == name)
        k = sum(1 for r, _, _ in matched if r["driver"] == name)
        rate = f"{100*k/m:.0f}%" if m else "-"
        print(f"  {name[:20]:20} {m:6} {b:5} {k:8}   {rate:>5}")
    print("  " + "-" * 52)
    total = len(manual)
    print(f"  {'TOTAL':20} {total:6} {len(bot):5} {len(matched):8}   "
          f"{100*len(matched)/total:.0f}%" if total else "")

    if missing:
        print(f"\n  NOT PRODUCED BY THE BOT ({len(missing)})")
        for r in missing[:25]:
            print(f"    {r['driver'][:16]:16} {r['origin']:6} {r['depart']} -> "
                  f"{r['destination']:6}  {r['transaction'] or ''}")
        if len(missing) > 25:
            print(f"    ... and {len(missing)-25} more")

    if extra:
        print(f"\n  BOT PRODUCED, NOT IN THE LOG ({len(extra)})")
        for l in extra[:25]:
            print(f"    {l['driver'][:16]:16} {str(l['origin_location'])[:6]:6} "
                  f"{hhmm(l['departure_time'])} -> {str(l['destination_location'])[:6]:6}"
                  f"  round={l['round_number']} {l['route_code'] or ''}")
        if len(extra) > 25:
            print(f"    ... and {len(extra)-25} more")

    if debug:
        print(f"\n  MISMATCH DIAGNOSTICS (set REPLAY_DEBUG=1 for full detail)")
        for row, leg, reason in debug:
            print(f"    MANUAL:   {row['driver'][:16]:16} {row['origin']:6} {row['depart']} -> "
                  f"{row['destination']:6}  {row['transaction'] or ''}")
            print(f"    CANDIDATE:{leg['driver'][:16]:16} {str(leg['origin_location'])[:6]:6} "
                  f"{hhmm(leg['departure_time'])} -> {str(leg['destination_location'])[:6]:6} "
                  f"round={leg['round_number']} {leg['route_code'] or ''}")
            print(f"    REJECTED: {reason}")
            print()

    pool.close()
    await pool.wait_closed()

    print(f"\n  Legs left in `{DB}` on mysql_db for inspection "
          f"(dropped and rebuilt at the start of the next run):")
    print(f"    docker compose exec mysql_db mysql -uroot -p -D {DB}")
    print(f"    SELECT * FROM shuttle_legs ORDER BY user_id, departure_time;")


asyncio.run(main())
