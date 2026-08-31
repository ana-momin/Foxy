"""Detection tests built from real posts collected live.

Every string below was actually published on X or LinkedIn. The lookalikes are
the point: each one contains a textbook announcement phrase and would fool a
keyword search, which is exactly how a naive monitor starts crying wolf.

No network access - the text is inlined so this runs anywhere.
"""

import pytest

from app.classify import classify, extract_batch, extract_company, score_rules

# --- genuine founder announcements -----------------------------------------
REAL = [
    "big news: i got into Y Combinator. solo founder, on my 4th attempt.",
    "We got into YC S26. And for the first time in the history of YC startups",
    "I got into YC S26 as a solo founder! The last 15 months looked like this:",
    "We got into Y Combinator (P26) After scaling SimpleClaw to $40k MRR",
    "After 6 applications and 6 rejection emails, we finally got into Y Combinator.",
    "We got into Y Combinator! Agnost AI (YC S26) is the infra for self-improving agents.",
    "Nebula Security is now backed by Y Combinator.",
    "1/ Adalat AI is now backed by Y Combinator. We are the first nonprofit",
    "thrilled to share our startup Nimbus got into Speedrun, a16z accelerator",
]

# --- lookalikes that must never fire ---------------------------------------
NOISE = [
    # someone else's news
    "Congrats to my friend Sarah who just got into Y Combinator! So proud of her.",
    "8 startups I referred got into YC. Here is what the founders had in common:",
    "The guy behind the coin just got accepted into Y combinator for a new startup.",
    "Shoutout to the team at Beta who got into Y Combinator this batch",
    # advice and retrospectives
    "I got into YC on my 6th attempt. Let me tell a few things every founder needs to hear.",
    "How I, a 17 year old dropout, got into Y Combinator.",
    "How I finally got into Y Combinator after 4 rejections.",
    "As founder I raised money and got into Y Combinator. Here are my 9 biggest lessons",
    "Here is how to get into YC: a thread on the application process",
    "exactly one year ago today, we got into Y Combinator. obviously thrilled, but",
    # commercial / third party
    "We are hiring engineers at a YC-backed startup. Apply now!",
    "As a YC alum, I invested in 12 companies this year.",
    "I invested early into a startup that was recently accepted into Y Combinator.",
    "Logical, an AI startup founded by two engineers, has been accepted into Y Combinator",
]


@pytest.mark.parametrize("text", REAL)
def test_real_announcements_fire(text):
    assert classify(text).is_announcement, f"should have fired: {text!r}"


@pytest.mark.parametrize("text", NOISE)
def test_lookalikes_are_rejected(text):
    assert not classify(text).is_announcement, f"should NOT have fired: {text!r}"


def test_precision_is_total_on_the_labelled_set():
    """No false positive is acceptable. One missed post is."""
    fired = [t for t in NOISE if classify(t).is_announcement]
    assert fired == [], f"false positives: {fired}"


def test_recall_stays_high():
    caught = sum(1 for t in REAL if classify(t).is_announcement)
    assert caught / len(REAL) >= 0.85


# --- extraction -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Agnost AI (YC S26) is the infra for agents", "Agnost AI"),
        ("Nebula Security is now backed by Y Combinator.", "Nebula Security"),
        ("1/ Adalat AI is now backed by Y Combinator.", "Adalat AI"),
        ("After scaling SimpleClaw to $40k MRR", "SimpleClaw"),
    ],
)
def test_company_extraction(text, expected):
    """Single-word matching once produced 'Security' for 'Nebula Security',
    which matched nothing in the directory and caused a false early alert."""
    assert extract_company(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We got into YC F26!", "Fall 2026"),
        ("joining YC W27", "Winter 2027"),
        ("accepted into Y Combinator Summer 2026 batch", "Summer 2026"),
        ("no batch mentioned here", None),
    ],
)
def test_batch_normalisation(text, expected):
    assert extract_batch(text) == expected


def test_first_person_is_required():
    """A post with no first-person voice is someone reporting on another
    company - the single most reliable discriminator found in testing."""
    third = "Logical has been accepted into Y Combinator, the company announced."
    assert not classify(third).is_announcement


def test_announcement_openers_count_as_first_person():
    """'Excited to announce X is in YC' carries no pronoun but is still the
    founder speaking."""
    assert classify("Excited to announce Acme AI is now backed by Y Combinator").is_announcement


def test_hiring_mention_does_not_veto_a_real_announcement():
    """Founders routinely announce an acceptance and mention hiring in the same
    post. Hiring is a soft negative, never a veto."""
    text = "We got into YC F26! Building Acme AI. We are hiring soon."
    assert classify(text).is_announcement


def test_speedrun_program_resolved_before_early_returns():
    """Programme must be set before any early return, or a Speedrun company
    gets verified against YC's directory - where it will never appear - and is
    reported as early forever."""
    v = score_rules("Nativ (a16z Speedrun) is an AI localisation platform")
    assert v.program == "Speedrun"
