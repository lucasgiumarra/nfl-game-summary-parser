"""
Ties parse_nfl_summary.py (ground truth from the box score) and
parse_play_by_play.py (reconstructed from play text) together, and
reports how well the reconstruction matches, player by player and
field by field.

Usage:
    python3 compare_stats.py <path_to_pdf>
"""

import sys
import json
from parse_nfl_summary import parse_game_summary
from parse_play_by_play import extract_pbp_lines, reconstruct_stats

# map reconstructed field names -> official field names
FIELD_MAP = {
    "rushing": {"ATT": "ATT", "YDS": "YDS", "TD": "TD"},
    "passing": {"ATT": "ATT", "CMP": "CMP", "YDS": "YDS", "TD": "TD", "IN": "IN"},
    "receiving": {"TAR": "TAR", "REC": "REC", "YDS": "YDS", "TD": "TD"},
    # sack YARDAGE is not reconstructed (see the comment above SACK_RE in
    # parse_play_by_play.py -- it's net-of-return yardage, not just the sum of
    # "for -N yards" sack lines), only the sack COUNT is compared here.
    "sacks": {"CNT": "CNT"},
}


def official_lookup(official_category):
    """Flatten {team: [ {player, ...}, ... ]} into {player: {field: value}}."""
    flat = {}
    for team, rows in official_category.items():
        for row in rows:
            flat[row["player"]] = row
    return flat


def official_sacks(official):
    """Pull sack COUNT (not yardage -- see FIELD_MAP) out of the passing
    section's SK/YD column, shaped like the other official_category dicts."""
    return {team: [{"player": row["player"], "CNT": int(row["SK_YD"].split("/")[0])}
                   for row in rows]
            for team, rows in official["passing"].items()}


def fumble_touched_players(plays):
    """Which players (by category) had at least one play where FUMBLES showed
    up on the same line -- their yardage is expected-noise territory, not a
    confirmed parser bug (see the comment in classify_play())."""
    touched = {"rushing": set(), "passing": set(), "receiving": set()}
    for play in plays:
        if not play.get("fumble"):
            continue
        if play["type"] == "rush":
            touched["rushing"].add(play["name"])
        elif play["type"] == "complete":
            touched["passing"].add(play["passer"])
            touched["receiving"].add(play["target"])
    return touched


def compare(pdf_path):
    official = parse_game_summary(pdf_path)
    lines = extract_pbp_lines(pdf_path)
    rushing, passing, receiving, sacks, plays = reconstruct_stats(lines)
    official = {**official, "sacks": official_sacks(official)}
    # A passer with zero sacks never gets a "sacked at ..." line, so nothing
    # creates their entry in `sacks` -- seed it so they compare as a real 0
    # instead of falsely showing up as "missing from play-by-play".
    for rows in official["sacks"].values():
        for row in rows:
            sacks.setdefault(row["player"], {"CNT": 0})
    fumble_plays = [p for p in plays if p.get("fumble")]
    fumble_touched = fumble_touched_players(plays)

    reconstructed = {"rushing": rushing, "passing": passing, "receiving": receiving, "sacks": sacks}
    report = {}
    total_fields = 0
    total_matched = 0
    total_matched_excl_fumble = 0

    for category, recon_players in reconstructed.items():
        official_flat = official_lookup(official[category])
        cat_report = []
        for player, recon_stats in recon_players.items():
            official_stats = official_flat.get(player)
            row = {"player": player, "reconstructed": recon_stats,
                   "fumble_touched": player in fumble_touched.get(category, ())}
            if official_stats is None:
                # A play can be fully negated (an offsetting "No Play" penalty,
                # a replay reversal) after already creating this player's
                # defaultdict entry, leaving it at all zeros. The box score
                # correctly omits a player with zero real attempts entirely,
                # so this isn't a parser miss -- don't flag it as one.
                if all(v == 0 for v in recon_stats.values()):
                    row["official"] = None
                    row["status"] = "match"
                else:
                    row["official"] = None
                    row["status"] = "player not found in box score"
            else:
                mismatches = {}
                for recon_field, official_field in FIELD_MAP[category].items():
                    total_fields += 1
                    recon_val = recon_stats[recon_field]
                    official_val = int(official_stats[official_field])
                    if recon_val == official_val:
                        total_matched += 1
                        total_matched_excl_fumble += 1
                    else:
                        mismatches[recon_field] = {"reconstructed": recon_val, "official": official_val}
                        if row["fumble_touched"]:
                            total_matched_excl_fumble += 1  # don't penalize known fumble noise
                row["official"] = {k: official_stats[v] for k, v in FIELD_MAP[category].items()}
                row["mismatches"] = mismatches
                row["status"] = "match" if not mismatches else "mismatch"
            cat_report.append(row)

        # also flag players present in the official box score but never
        # reconstructed from play-by-play at all (a real parser miss)
        recon_names = set(recon_players.keys())
        for player in official_flat:
            if player not in recon_names:
                cat_report.append({"player": player, "reconstructed": None,
                                    "official": official_flat[player], "status": "missing from play-by-play"})

        report[category] = cat_report

    accuracy = total_matched / total_fields if total_fields else 0
    accuracy_excl_fumble = total_matched_excl_fumble / total_fields if total_fields else 0
    return report, accuracy, accuracy_excl_fumble, len(plays), len(lines), fumble_plays


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Rams_49ers_Game_Summary.pdf"
    report, accuracy, accuracy_excl_fumble, n_plays, n_lines, fumble_plays = compare(path)

    print(f"Play-by-play lines scanned: {n_lines}")
    print(f"Offensive plays reconstructed: {n_plays}")
    print(f"Overall field-level accuracy: {accuracy:.1%}")
    if fumble_plays:
        print(f"  (excluding known fumble-noise fields: {accuracy_excl_fumble:.1%} "
              f"-- {len(fumble_plays)} play(s) had a same-line FUMBLES; direction/size of "
              f"the resulting yardage discrepancy isn't modeled, see classify_play())")
    print()

    for category, rows in report.items():
        print(f"=== {category.upper()} ===")
        for row in rows:
            status = row["status"]
            marker = "OK" if status == "match" else "!!"
            note = " (fumble on a play -- may be expected noise)" if row.get("fumble_touched") and status == "mismatch" else ""
            print(f"  [{marker}] {row['player']:15s} {status}{note}")
            if status == "mismatch":
                for field, vals in row["mismatches"].items():
                    print(f"        {field}: reconstructed={vals['reconstructed']} official={vals['official']}")
        print()
