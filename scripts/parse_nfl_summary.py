"""
Parser for NFL "Game Summary" PDFs (the official league box-score format).
Extracts offensive stats (rushing / passing / receiving) for both teams
from the "Final Individual Statistics" page into structured JSON.

Usage:
    python3 parse_nfl_summary.py <path_to_pdf>
"""

import re
import sys
import json
import pdfplumber


def find_stats_page(pdf_path):
    """Locate the 'Final Individual Statistics' page object."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Final Individual Statistics" in text:
                return page.extract_words(), page.extract_text(), page.width
    raise ValueError("Could not find 'Final Individual Statistics' page in PDF")


def split_into_columns(words, page_width, y_tolerance=2):
    """
    Group words into text lines using their y-position, then split each line
    into left-team / right-team halves using x-position (the report is laid
    out as two side-by-side columns). This preserves which side a row
    belongs to even when the two teams have different numbers of players
    (plain text extraction collapses this information).
    Returns (left_lines, right_lines) as lists of strings, line-aligned.
    """
    lines = {}
    for w in words:
        y = round(w["top"] / y_tolerance) * y_tolerance
        lines.setdefault(y, []).append(w)

    midpoint = page_width / 2
    left_lines, right_lines = [], []
    for y in sorted(lines.keys()):
        row = sorted(lines[y], key=lambda w: w["x0"])
        left = " ".join(w["text"] for w in row if w["x0"] < midpoint)
        right = " ".join(w["text"] for w in row if w["x0"] >= midpoint)
        left_lines.append(left)
        right_lines.append(right)
    return left_lines, right_lines


def get_team_names(full_text):
    """Pull the two team names from the header line, e.g. 'X vs Y'."""
    m = re.search(r"([A-Za-z0-9 .]+?) vs ([A-Za-z0-9 .]+)", full_text)
    if not m:
        raise ValueError("Could not find team names")
    return m.group(1).strip(), m.group(2).strip()


# Column layouts for each stat category (order matters, matches PDF headers)
RUSHING_COLS = ["ATT", "YDS", "AVG", "LG", "TD"]
PASSING_COLS = ["ATT", "CMP", "YDS", "SK_YD", "TD", "LG", "IN", "RT"]
RECEIVING_COLS = ["TAR", "REC", "YDS", "AVG", "LG", "TD"]

# Player name as printed in the stat table, e.g. "C.McCaffrey":
#   [A-Z]                first letter of the initial
#   [A-Za-z'.\-]*         any further initials/punctuation ("T.J."), consumed
#                         greedily since the trailing \. anchors the surname
#   \.                    the dot separating initial(s) from surname
#   [A-Za-z'\-]+          the surname (letters, apostrophes, hyphens)
NAME_RE = r"[A-Z][A-Za-z'.\-]*\.[A-Za-z'\-]+"
# A generic stat cell: an optional leading "-" (for e.g. sack yards),
# digits, and an optional decimal part (for AVG/RT columns like "134.2").
NUM_RE = r"-?\d+(?:\.\d+)?"
# The SK/YD column specifically, which is "sacks/yards" rather than a single
# number, e.g. "0/0" (no sacks) or "2/-14" (2 sacks for -14 net yards):
#   \d+       sack count (always non-negative)
#   /         literal separator
#   -?\d+     net yards lost to sacks (can be negative or, per compare_stats.py
#             notes, even positive when a same-play fumble return nets it out)
SACK_RE = r"\d+/-?\d+"

# Passing has one SK/YD field at index 3 (0-based) that needs SACK_RE instead of NUM_RE
COL_PATTERNS = {
    "SK_YD": SACK_RE,
}


def build_single_col_regex(n_cols, col_names=None):
    r"""Regex for ONE team's row: name followed by n_cols stat fields.

    Builds a pattern like (for RECEIVING_COLS, n_cols=6):
        ^(NAME_RE)(\s+NUM_RE){6}\s*$
    i.e. "^(C\.McCaffrey)\s+(6)\s+(97)\s+(16\.2)\s+(52)\s+(1)\s*$" for a
    line like "C.McCaffrey 6 97 16.2 52 1" -- one capture group per column, in
    the same left-to-right order as col_names, so zip(col_names, groups())
    downstream lines each captured number up with its header.
    Anchored with ^...$ (whole line, not search) so it only matches genuine
    stat rows, not headers or "Total" lines that happen to contain a name.
    """
    def field_pattern(i):
        if col_names and col_names[i] in COL_PATTERNS:
            return r"\s+(" + COL_PATTERNS[col_names[i]] + r")"
        return r"\s+(" + NUM_RE + r")"

    fields = "".join(field_pattern(i) for i in range(n_cols))
    return re.compile("^(" + NAME_RE + ")" + fields + r"\s*$")


def parse_section(left_lines, right_lines, start_idx, n_cols, col_names):
    """
    Parse consecutive player rows for one stat category, walking the left
    and right column line-lists independently (they're line-index aligned
    with each other and stop independently once a team runs out of rows).
    """
    row_re = build_single_col_regex(n_cols, col_names)
    left, right = [], []
    i = start_idx
    while i < len(left_lines):
        lline = left_lines[i].strip()
        rline = right_lines[i].strip()
        if (not lline or lline.startswith("Total")) and (not rline or rline.startswith("Total")):
            break
        ml = row_re.match(lline) if lline and not lline.startswith("Total") else None
        mr = row_re.match(rline) if rline and not rline.startswith("Total") else None
        if ml:
            left.append({"player": ml.group(1), **dict(zip(col_names, ml.groups()[1:]))})
        if mr:
            right.append({"player": mr.group(1), **dict(zip(col_names, mr.groups()[1:]))})
        i += 1
    return left, right, i


def parse_game_summary(pdf_path):
    words, full_text, page_width = find_stats_page(pdf_path)
    away, home = get_team_names(full_text)
    left_lines, right_lines = split_into_columns(words, page_width)

    result = {"away_team": away, "home_team": home,
              "rushing": {}, "passing": {}, "receiving": {}}

    for idx, lline in enumerate(left_lines):
        stripped = lline.strip()
        if stripped.startswith("RUSHING"):
            l, r, _ = parse_section(left_lines, right_lines, idx + 1, len(RUSHING_COLS), RUSHING_COLS)
            result["rushing"] = {away: l, home: r}
        elif stripped.startswith("PASSING"):
            l, r, _ = parse_section(left_lines, right_lines, idx + 1, len(PASSING_COLS), PASSING_COLS)
            result["passing"] = {away: l, home: r}
        elif stripped.startswith("PASS RECEIVING"):
            l, r, _ = parse_section(left_lines, right_lines, idx + 1, len(RECEIVING_COLS), RECEIVING_COLS)
            result["receiving"] = {away: l, home: r}

    return result


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Rams_49ers_Game_Summary.pdf"
    data = parse_game_summary(path)
    print(json.dumps(data, indent=2))
