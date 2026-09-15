# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Parses official NFL "Game Summary" PDFs (league "Game Books" — the same PDF
downloadable from a game's Game Center recap page on NFL.com via the
"Download Game Book (PDF)" link) and cross-checks a from-scratch
reconstruction of offensive stats against the box score's own numbers, as a
way to validate how completely/correctly the play-by-play text is being
parsed.

## Setup and commands

```bash
source .venv/bin/activate   # venv already has pdfplumber installed
python3 compare_stats.py <path_to_pdf>       # main entry point — full comparison report
python3 parse_nfl_summary.py <path_to_pdf>   # box score only, prints JSON
python3 parse_play_by_play.py <path_to_pdf>  # play-by-play reconstruction only, prints JSON
```

There is no test suite. Correctness is validated empirically by running
`compare_stats.py` against real Game Book PDFs and checking the reported
field-level accuracy and per-player mismatches — treat a drop in accuracy
on an existing PDF as a regression. Checked-in fixtures for this, all
2026 REG1: `game_summary.pdf` (SF @ LA), `cowboys_giants_2026_wk1.pdf`,
`pats_seahawks_2026_wk1.pdf`, `commanders_eagles_2026_wk1.pdf`,
`bears_panthers_2026_wk1.pdf`. All five should stay at 100% accuracy
excluding known fumble-noise fields (see below) after any parser change —
run `compare_stats.py` on all of them, not just one, since each game has
turned up its own new phrasing/edge case so far. To get another test
fixture, find a game's Game Center page on nfl.com
(`nfl.com/games/<away>-at-<home>-<year>-<REG/POST>-<week>`) and pull the
"Download Game Book (PDF)" link from it.

## Architecture

Three scripts form a pipeline, each independently runnable:

- **`parse_nfl_summary.py`** — ground truth. Finds the "Final Individual
  Statistics" page and parses it into structured rushing/passing/receiving
  rows per team. The page is laid out as two side-by-side team columns of
  plain text with no delimiters, so extraction works off raw word
  positions (`page.extract_words()`): words are grouped into lines by
  y-position, then each line is split into left-team/right-team halves by
  x-position relative to the page midpoint. This preserves which team a row
  belongs to even when the teams have different numbers of players (something
  plain `extract_text()` would collapse). Each stat category has a
  fixed, known column order (`RUSHING_COLS`, `PASSING_COLS`,
  `RECEIVING_COLS`) that a row regex is built against per-category
  (`build_single_col_regex`).

- **`parse_play_by_play.py`** — reconstructs the same three stat categories
  (plus sack counts) from the free-text play-by-play section, play by play.
  `classify_play()` is the core: it regex-matches one PDF text line at a
  time against rush/pass/sack phrasings and returns a dict describing the
  play, or `None` for anything else (kicks, punts, timeouts, etc). Every
  regex used there has an explanatory comment breaking down what it matches,
  with real example lines — read those before changing or extending them.
  `reconstruct_stats()` folds the play stream into running per-player
  totals, with a `last_undo` closure mechanism that reverses the
  most-recently-applied play when the next line indicates it didn't
  actually count (an offsetting "PENALTY ... No Play", or a replay
  reversal). This undo mechanism is the pattern to extend for any other
  "the previous play didn't count" situation. A parallel `last_hold_adjust`
  closure handles the different case of an accepted "Offensive Holding"
  *without* "- No Play" printed — that doesn't void the play, it just trims
  its credited yardage down to the penalty's enforcement spot (see
  `credited_yards_after_hold()`). Both closures get cleared (not fired) by
  an intervening, unclassified new play (a punt, kickoff, field goal, ...)
  so a later penalty can't misfire against a stale, unrelated older play —
  this staleness bug is worth remembering if `reconstruct_stats()` grows
  another closure like these two.

- **`compare_stats.py`** — ties the two together. Flattens the box score's
  per-team lists into a single `{player: stats}` map and diffs it
  field-by-field against the reconstruction, producing a per-player,
  per-field match/mismatch report and an overall accuracy percentage.

## Known, deliberately-unmodeled edge cases

These came up parsing real games and are documented in code comments where
relevant — don't try to "fix" them without new evidence, since prior attempts
at flat heuristics didn't generalize:

- **Fumble yardage noise**: when a rush/completion line contains `FUMBLES`,
  the box score's credited yardage can differ from the play text's "for N
  yards" in either direction (recovered-forward vs. recovered-backward, or a
  player recovering his own fumble and continuing the run), and by amounts
  other than 1 yard. There's no reliable flat adjustment given the examples
  seen so far, so these plays are just flagged (`fumble: True` on the play
  dict) and `compare_stats.py` reports affected players' mismatches as
  expected noise rather than parser bugs.
- **Sack yardage**: sack *count* is reconstructed and compared exactly
  against the box score's `SK/YD` column, but sack *yardage* is not — the
  box score's figure is net team yardage lost, which can differ from the sum
  of "sacked ... for -N yards" lines whenever the same play's fumble is
  recovered and returned (observed: two sacks of `0` and `-8` yards netted to
  `2/1` because of a same-play fumble-recovery return). This needs the same
  kind of multi-event-per-line handling as the fumble case above.
