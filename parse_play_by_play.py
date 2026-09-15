"""
Parses the PLAY-BY-PLAY section of an NFL "Game Summary" PDF and
reconstructs rushing / passing / receiving stats play-by-play, so they
can be compared against the box score's own "Final Individual Statistics"
(see parse_nfl_summary.py) as a check on how completely the play text
captures the box score.

Usage:
    python3 parse_play_by_play.py <path_to_pdf>
"""

import re
import sys
import json
from collections import defaultdict
import pdfplumber

# Player name as the gamebook prints it, e.g. "C.McCaffrey", "T.J.Watt":
#   [A-Z]           first letter of the initial, always capitalized
#   [\w.'-]*        zero or more chars for any middle initials ("T.J."),
#                   consumed greedily since the final \. anchors where the
#                   surname actually starts
#   \.              the literal dot that separates initial(s) from surname
#   [A-Za-z'-]+     the surname itself (letters plus apostrophes/hyphens for
#                   names like "O'Neal" or "Smith-Jones")
NAME = r"[A-Z][\w.'-]*\.[A-Za-z'-]+"


def for_yards(tag):
    """Matches the yardage clause at the end of a play, e.g. "for 6 yards",
    "for -3 yards", or "for no gain". `tag` namespaces the capture group
    (yds_rush / yds_pass) so RUSH_RE and COMPLETE_RE, which both embed this,
    don't collide on a duplicate group name.
      for\\s+                     literal "for" plus whitespace
      (?:...|no gain)             either a signed-yardage number+"yard(s)",
                                   or the literal phrase "no gain"
      (?P<yds_{tag}>-?\\d+)       the signed integer yardage, captured
      \\s*yards?                  "yard" or "yards", loosely spaced
    """
    return rf"for\s+(?:(?P<yds_{tag}>-?\d+)\s*yards?|no gain)"


def yards_to_int(match, tag):
    val = match.group(f"yds_{tag}")
    return 0 if val is None else int(val)


def extract_pbp_lines(pdf_path):
    """Collect every line from the play-by-play pages (spans several pages)."""
    lines = []
    collecting = False
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Play By Play" in text:
                collecting = True
            if "Miscellaneous Statistics Report" in text:
                collecting = False
            if collecting:
                lines.extend(text.split("\n"))
    return lines


# Matches a rushing play, e.g.:
#   "B.Corum right tackle to LA 26 for -1 yards (N.Bosa)."
#   "J.Williams up the middle to DAL 33 for no gain (A.Carter; K.Thibodeaux)."
#   "M.Stafford kneels to LA 20 for -1 yards."
#   "J.Dart scrambles left end to NYG 30 for 5 yards (B.Burns)."
#   (?P<name>{NAME})           the ball carrier, e.g. "B.Corum"
#   \s+                        whitespace before the run description
#   (?:                        the run-direction phrase -- one of:
#     (?:left|right)\s+(?:guard|tackle|end)   a direction + hole, "right tackle"
#     |up the middle                          straight up the gut
#     |kneels                                 a clock-killing kneel-down
#     |scrambles(?:\s+...)?    a broken-pocket scramble, optionally followed
#                              by the same direction+hole/middle phrasing
#   )
#   .*?                        lazily consume everything in between (the
#                              landing spot "to LA 26" and/or tackler credit
#                              "(N.Bosa)") up to the first yardage clause
#   {for_yards('rush')}        "for N yards" / "for no gain" (see for_yards())
#   (?P<td>,\s*TOUCHDOWN)?     optional trailing ", TOUCHDOWN"
RUSH_RE = re.compile(
    rf"(?P<name>{NAME})\s+"
    rf"(?:(?:left|right)\s+(?:guard|tackle|end)|up the middle|kneels|"
    rf"scrambles(?:\s+(?:left|right)\s+(?:end|guard|tackle)|\s+up the middle)?)"
    rf".*?{for_yards('rush')}(?P<td>,\s*TOUCHDOWN)?"
)

# Sacks: phrasing confirmed from a real game, e.g.
#     "J.Dart sacked at NYG 31 for 0 yards (sack split by D.Ezeiruaku and J.Bullard)."
#     "J.Dart sacked at DAL 36 for -8 yards (C.Downs). FUMBLES (C.Downs) [C.Downs],
#      recovered by NYG-J.Runyan at DAL 34. J.Runyan to DAL 29 for 5 yards (D.Winters)."
# Sack COUNT is tracked below and compared 1:1 against the box score's SK/YD
# column (sacks don't count toward passing ATT, so they're a separate stat).
# Sack YARDAGE is deliberately NOT reconstructed: the box score's SK/YD yards
# figure is the net team yardage lost, which on the second example above is
# -1, not -8 -- because the fumble was recovered by the passer's own team and
# returned 5 yards forward on the same broken-up play. Modeling that requires
# the same kind of multi-event-per-line parsing as the fumble-continuation
# case in classify_play()'s docstring, and one example isn't enough to trust
# a general rule for it.
# Matches just enough of a sack line to get the passer's name and confirm
# it's a sack, e.g.:
#   "J.Dart sacked at NYG 31 for 0 yards (sack split by ...)"
#   "D.Maye sacked ob at NE 43 for -2 yards (D.Thomas)."   (sacked out of bounds)
#   (?P<passer>{NAME})     the quarterback who was sacked
#   \s+sacked\s+           the literal "sacked"
#   (?:ob\s+)?             optional "ob" (out of bounds) before the spot
#   at\s+[A-Za-z0-9]+\s+\d+  "at" + the field spot ("NYG 31" / a bare number at
#                          midfield), consumed but not captured -- yardage
#                          isn't reconstructed
SACK_RE = re.compile(rf"(?P<passer>{NAME})\s+sacked\s+(?:ob\s+)?at\s+[A-Za-z0-9]+\s+\d+")

# The passer, from the start of any pass play, e.g. "M.Stafford pass short
# middle to T.Higbee...": name followed by the literal word "pass".
PASSER_RE = re.compile(rf"(?P<passer>{NAME})\s+pass\s+")

# An interception, e.g.:
#   "M.Stafford pass short right intended for D.Adams INTERCEPTED by F.Warner"
#   "D.Prescott pass deep right INTERCEPTED by J.Holland at NYG 45."  (no
#     named target -- a broken-up/uncatchable throw, e.g. a Hail Mary)
#   (?:intended for\s+(?P<target>{NAME})\s+)?   optional -- the receiver the
#                                                ball was thrown at, when named
#   INTERCEPTED by\s+(?P<defender>{NAME})       the defender who picked it off
INT_RE = re.compile(rf"(?:intended for\s+(?P<target>{NAME})\s+)?INTERCEPTED by\s+(?P<defender>{NAME})")

# A completed pass, e.g.:
#   "M.Stafford pass short right to T.Higbee to LA 39 for 8 yards (F.Warner)."
#   "J.Dart pass short left to I.Likely pushed ob at 50 for 19 yards (S.Revel)."
#   "J.Dart pass short right to I.Likely for 15 yards, TOUCHDOWN."
#   to\s+(?P<target>{NAME})     the receiver ("to T.Higbee")
#   .*?                         lazily skip whatever comes next -- the landing
#                               spot ("to LA 39"), an out-of-bounds note
#                               ("pushed ob at 50"/"ran ob at DAL 38"), and/or
#                               tackler credit -- up to the first yardage clause
#   {for_yards('pass')}         "for N yards" (see for_yards())
#   (?P<td>,\s*TOUCHDOWN)?      optional trailing ", TOUCHDOWN"
COMPLETE_RE = re.compile(
    rf"to\s+(?P<target>{NAME})"
    rf".*?{for_yards('pass')}(?P<td>,\s*TOUCHDOWN)?"
)

# An incomplete pass has no yardage clause to anchor on, so this just grabs
# whoever is named right after "to", e.g. "pass incomplete short left to
# M.Nabers." -- intentionally loose since it's only used once "incomplete"
# has already been confirmed elsewhere in the line.
INCOMPLETE_TARGET_RE = re.compile(rf"to\s+(?P<target>{NAME})")


def classify_play(line):
    """Return a dict describing the play, or None if this line isn't a
    rush/pass snap (kicks, punts, penalties, timeouts, etc. are ignored)."""
    # Fumbles on a rush/completion line are a known source of small yardage
    # mismatches against the box score, but there's no reliable flat
    # adjustment: across 4 observed fumble plays, official yardage was 1 less
    # than the "for N yards" text once, exactly matched twice, and (for a
    # player who recovered his own fumble and kept running past the "for N"
    # spot) needed MORE yards, not fewer. The direction seems to depend on
    # where the ball ended up relative to the tackle spot and who recovered
    # it -- not modeled here since that needs possession/direction tracking
    # this parser doesn't do. So: don't guess, just flag it. `fumble` is
    # carried on the returned play dict so compare_stats.py can call out
    # affected players' mismatches as expected noise rather than real bugs.
    fumble = "FUMBLES" in line

    if " sacked " in line:
        m_sack = SACK_RE.search(line)
        return {"type": "sack", "passer": m_sack.group("passer"), "fumble": fumble} if m_sack else None

    if " pass " in line:
        m_passer = PASSER_RE.search(line)
        if not m_passer:
            return None
        passer = m_passer.group("passer")

        m_int = INT_RE.search(line)
        if m_int:
            return {"type": "interception", "passer": passer,
                    "target": m_int.group("target")}

        if "incomplete" in line:
            m_t = INCOMPLETE_TARGET_RE.search(line)
            return {"type": "incomplete", "passer": passer,
                    "target": m_t.group("target") if m_t else None}

        m_comp = COMPLETE_RE.search(line)
        if m_comp:
            yards = yards_to_int(m_comp, "pass")
            return {"type": "complete", "passer": passer,
                    "target": m_comp.group("target"), "yards": yards,
                    "td": bool(m_comp.group("td")), "fumble": fumble}
        return None

    m_rush = RUSH_RE.search(line)
    if m_rush:
        yards = yards_to_int(m_rush, "rush")
        return {"type": "rush", "name": m_rush.group("name"),
                "yards": yards, "td": bool(m_rush.group("td")), "fumble": fumble}
    return None


# Marks the start of a genuine new snap, e.g. "2-17-NE 45 (2:16) ..." or
# "4-4-NE 48 (13:26) M.Wishnowsky punts...": down-distance-spot, then a
# parenthesized game clock. Continuation lines (a fumble's "RECOVERED by ...",
# an "extra point is GOOD") never start this way, so this reliably
# distinguishes "a new play just happened that we may not have classified"
# from "this line is just more detail about the play we already have".
# Kickoffs are a separate case (see KICKOFF_RE below): they have no down to
# stamp at all.
DOWN_STAMP_RE = re.compile(r"^\d+-\d+-.*?\(\d+:\d+\)")

# A kickoff, e.g. "E.Pineiro kicks 62 yards from SF 35 to LA 3. J.Whittington
# to LA 27 for 24 yards...". Like DOWN_STAMP_RE, this marks the start of a new
# (untracked) play, but kickoffs carry no down/distance/clock stamp to match
# on -- they follow a score, so possession and the down count reset instead.
KICKOFF_RE = re.compile(r"kicks\s+\d+\s+yards\s+from\b")

# A field-position spot as the gamebook prints it, e.g. "PHI 39" or a bare
# "50" right at midfield:
#   (?:([A-Z]{2,4})\s+)?   optional team abbreviation
#   (\d{1,2})              the yard-line number (0-50)
_SPOT = r"(?:([A-Z]{2,4})\s+)?(\d{1,2})"

# The line of scrimmage a play started from, off its own leading
# down-distance-spot-clock stamp, e.g. "1-10-PHI 39 (7:32) S.Barkley...".
LOS_RE = re.compile(rf"^\d+-\d+-{_SPOT}\s+\(")

# Where a rush/completion physically ended, e.g. "to PHI 44 for 5 yards" or
# "pushed ob at 50 for 19 yards". Requires a bare number right where the spot
# would be, so this can't accidentally match a receiver's name ("to
# T.Higbee") -- names never satisfy \d{1,2} -- and instead skips ahead to the
# real landing spot right before "for".
LANDING_RE = re.compile(rf"(?:to|pushed\s+ob\s+at|ran\s+ob\s+at)\s+{_SPOT}\s+for\s")

# Where an accepted penalty is enforced from, e.g. "enforced at PHI 40."
ENFORCED_RE = re.compile(rf"enforced\s+at\s+{_SPOT}")


def credited_yards_after_hold(los, landing, raw_gain, enforced):
    """An accepted "Offensive Holding" WITHOUT "- No Play" still counts the
    play as a real attempt, but only credits individual yardage up to the
    penalty's enforcement spot (measured from the previous line of
    scrimmage) rather than the play's full "for N yards" text -- confirmed
    exactly against 3 real plays across 2 different games (see the comment
    in reconstruct_stats() where this is used).

    `los`, `landing`, `enforced` are each (team_or_None, yard_number) as
    parsed by LOS_RE / LANDING_RE / ENFORCED_RE. Returns None (meaning:
    leave the play's recorded stats alone) whenever the spots can't be
    confidently compared as plain numbers -- e.g. the play crossed from one
    team's half to the other's between the LOS and the landing spot, which
    would need full field-position math this parser doesn't do.
    """
    los_team, los_num = los
    landing_team, landing_num = landing
    enf_team, enf_num = enforced
    if los_team != enf_team or (landing_team is not None and landing_team != los_team):
        return None
    diff = landing_num - los_num
    if diff == raw_gain:
        sign = 1
    elif diff == -raw_gain:
        sign = -1
    else:
        return None  # numbers don't line up the way we expect -- don't guess
    return sign * (enf_num - los_num)


def reconstruct_stats(lines):
    rushing = defaultdict(lambda: {"ATT": 0, "YDS": 0, "TD": 0})
    passing = defaultdict(lambda: {"ATT": 0, "CMP": 0, "YDS": 0, "TD": 0, "IN": 0})
    receiving = defaultdict(lambda: {"TAR": 0, "REC": 0, "YDS": 0, "TD": 0})
    sacks = defaultdict(lambda: {"CNT": 0})
    plays = []
    last_undo = None  # callable that reverses the most recently applied play
    last_hold_adjust = None  # callable(enforced_spot) -- see "Offensive Holding" below

    for line in lines:
        # A "PENALTY ... No Play" line with NO leading down/distance/time
        # stamp negates the play immediately before it (offsetting penalty
        # redoes the down). One WITH a leading stamp (e.g. a false start on
        # the next snap) does not negate the prior, already-completed play.
        if line.strip().startswith("PENALTY") and "No Play" in line:
            if last_undo:
                last_undo()
            last_undo = None
            last_hold_adjust = None
            continue

        # An accepted "Offensive Holding" WITHOUT "- No Play" does NOT negate
        # the preceding play's individual stats the way a "No Play" penalty
        # does (verified: undoing it wrongly dropped a real rushing attempt
        # from the box-score-matching count) -- but it does trim its credited
        # yardage down to the enforcement spot. See credited_yards_after_hold().
        if line.strip().startswith("PENALTY") and "Offensive Holding" in line:
            if last_hold_adjust:
                m_enf = ENFORCED_RE.search(line)
                if m_enf:
                    last_hold_adjust((m_enf.group(1), int(m_enf.group(2))))
            last_undo = None
            last_hold_adjust = None
            continue

        # A replay reversal ("The Replay Official reviewed ... REVERSED.")
        # negates the just-recorded play; the corrected result is stated on
        # the very next line and gets counted normally as its own play.
        if "Replay Official" in line and "REVERSED" in line:
            if last_undo:
                last_undo()
            last_undo = None
            last_hold_adjust = None
            continue

        play = classify_play(line)
        if not play:
            # An unclassified new snap (punt, field goal, spike, kickoff...)
            # supersedes whatever play last_undo was still pointing at: a
            # later penalty now belongs to THIS play, which we don't know how
            # to undo, so drop the stale reference rather than wrongly undo
            # an older tracked play (e.g. an "Offensive Holding" on a kickoff
            # return incorrectly voiding the last tracked play from the
            # previous drive).
            if DOWN_STAMP_RE.match(line.strip()) or KICKOFF_RE.search(line):
                last_undo = None
                last_hold_adjust = None
            continue
        plays.append(play)
        last_hold_adjust = None

        # Recovered here (once) for use by the "Offensive Holding" yardage
        # trim below, if this turns out to be a rush or completion.
        m_los = LOS_RE.match(line.strip())
        m_landing = LANDING_RE.search(line)
        los = (m_los.group(1), int(m_los.group(2))) if m_los else None
        landing = (m_landing.group(1), int(m_landing.group(2))) if m_landing else None

        if play["type"] == "rush":
            r = rushing[play["name"]]
            r["ATT"] += 1
            r["YDS"] += play["yards"]
            r["TD"] += int(play["td"])
            name, yards, td = play["name"], play["yards"], int(play["td"])
            last_undo = lambda r=r, yards=yards, td=td: (
                r.__setitem__("ATT", r["ATT"] - 1),
                r.__setitem__("YDS", r["YDS"] - yards),
                r.__setitem__("TD", r["TD"] - td),
            )
            if los and landing:
                def _adjust(enforced, r=r, los=los, landing=landing, raw_gain=yards):
                    credited = credited_yards_after_hold(los, landing, raw_gain, enforced)
                    if credited is not None:
                        r["YDS"] -= (raw_gain - credited)
                last_hold_adjust = _adjust

        elif play["type"] == "complete":
            p = passing[play["passer"]]
            p["ATT"] += 1
            p["CMP"] += 1
            p["YDS"] += play["yards"]
            p["TD"] += int(play["td"])
            rec = receiving[play["target"]]
            rec["TAR"] += 1
            rec["REC"] += 1
            rec["YDS"] += play["yards"]
            rec["TD"] += int(play["td"])
            yards, td = play["yards"], int(play["td"])
            last_undo = lambda p=p, rec=rec, yards=yards, td=td: (
                p.__setitem__("ATT", p["ATT"] - 1), p.__setitem__("CMP", p["CMP"] - 1),
                p.__setitem__("YDS", p["YDS"] - yards), p.__setitem__("TD", p["TD"] - td),
                rec.__setitem__("TAR", rec["TAR"] - 1), rec.__setitem__("REC", rec["REC"] - 1),
                rec.__setitem__("YDS", rec["YDS"] - yards), rec.__setitem__("TD", rec["TD"] - td),
            )
            if los and landing:
                def _adjust(enforced, p=p, rec=rec, los=los, landing=landing, raw_gain=yards):
                    credited = credited_yards_after_hold(los, landing, raw_gain, enforced)
                    if credited is not None:
                        delta = raw_gain - credited
                        p["YDS"] -= delta
                        rec["YDS"] -= delta
                last_hold_adjust = _adjust

        elif play["type"] == "incomplete":
            p = passing[play["passer"]]
            p["ATT"] += 1
            rec = receiving[play["target"]] if play["target"] else None
            if rec:
                rec["TAR"] += 1
            last_undo = lambda p=p, rec=rec: (
                p.__setitem__("ATT", p["ATT"] - 1),
                rec.__setitem__("TAR", rec["TAR"] - 1) if rec else None,
            )

        elif play["type"] == "interception":
            p = passing[play["passer"]]
            p["ATT"] += 1
            p["IN"] += 1
            rec = receiving[play["target"]] if play["target"] else None
            if rec:
                rec["TAR"] += 1
            last_undo = lambda p=p, rec=rec: (
                p.__setitem__("ATT", p["ATT"] - 1), p.__setitem__("IN", p["IN"] - 1),
                rec.__setitem__("TAR", rec["TAR"] - 1) if rec else None,
            )

        elif play["type"] == "sack":
            s = sacks[play["passer"]]
            s["CNT"] += 1
            last_undo = lambda s=s: s.__setitem__("CNT", s["CNT"] - 1)

        else:
            last_undo = None

    return dict(rushing), dict(passing), dict(receiving), dict(sacks), plays


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Rams_49ers_Game_Summary.pdf"
    lines = extract_pbp_lines(path)
    rushing, passing, receiving, sacks, plays = reconstruct_stats(lines)
    print(json.dumps({"rushing": rushing, "passing": passing, "receiving": receiving, "sacks": sacks,
                       "plays_parsed": len(plays), "lines_scanned": len(lines)}, indent=2))
