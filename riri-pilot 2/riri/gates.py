"""Assignment Mode stage machine. Server-side — the client never decides the stage.

The pedagogical claim in the grant: students cannot feed an assignment in and
pull a draft out. Enforcement is two-layer:
  1. Stage prompts constrain what the model produces at each phase.
  2. Advancement requires a logged student submission (gate_submissions table)
     that clears a minimum-substance bar.

Deliberately NOT built: an NLP quality classifier on gate submissions. At pilot
scale the instructor reviews gate logs; a word-count floor filters keyboard
mashing, humans judge substance. Build the classifier only if Fall logs show
gaming at a rate the instructor can't absorb.

v2:
- Repetition detector rewritten. v1 used non-overlapping 6-gram windows with a
  strict '<' at the 50% boundary; an exact 2x paste passed (landed on the
  boundary) and any repeat whose block length wasn't a multiple of 6 misaligned
  the windows and passed entirely. Now: sliding-window grams, threshold 0.6.
  An exact 2x paste sits near 0.52 unique-ratio; natural prose sits near 1.0.
- assistant_overlap(): share of a submission's 6-grams that appear in RiRi's
  own replies this session. app.py uses it to FLAG (never auto-reject) gate
  submissions that are mostly pasted model output — the flag lands in the gate
  log the instructor already reviews. Rejection stays a human call.
"""
import os, re

STAGES = ["invention", "development", "polishing"]
MIN_GATE_WORDS = int(os.environ.get("RIRI_MIN_GATE_WORDS", "100"))

NGRAM = 6
DUP_UNIQUE_RATIO = 0.6      # below this share of unique grams => repeated text
OVERLAP_FLAG_RATIO = float(os.environ.get("RIRI_GATE_OVERLAP_FLAG", "0.30"))


def next_stage(current: str) -> str | None:
    i = STAGES.index(current)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'-]+", text.lower())


def _grams(words: list[str], n: int = NGRAM) -> list[tuple]:
    """Sliding-window n-grams — overlap makes the check alignment-proof."""
    return [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]


def word_count(text: str) -> int:
    return len(_words(text))


def gate_check(submission: str) -> tuple[bool, str]:
    wc = word_count(submission)
    if wc < MIN_GATE_WORDS:
        return False, (
            f"Gate submissions need at least {MIN_GATE_WORDS} words of your own "
            f"thinking to advance (you wrote {wc}). This is the documented work "
            "the next stage builds on."
        )
    grams = _grams(_words(submission))
    if grams and len(set(grams)) < len(grams) * DUP_UNIQUE_RATIO:
        return False, "This reads as repeated text. Write your response in your own words."
    return True, ""


def assistant_overlap(submission: str, assistant_texts: list[str]) -> float:
    """Fraction of the submission's n-grams present in assistant replies.

    0.0 = fully the student's own phrasing; 1.0 = entirely pasted model output.
    Used for flagging only — quoted phrases and echoed terms will produce small
    nonzero values by design, which is why the threshold lives above 0.
    """
    sub = set(_grams(_words(submission)))
    if not sub:
        return 0.0
    seen: set = set()
    for t in assistant_texts:
        seen.update(_grams(_words(t)))
    return len(sub & seen) / len(sub)
