"""Time periods and sample sizes for hypotheses.

Two things live here, both pure functions over dates and numbers, so they can
be reasoned about and tested without a warehouse connection.

**Periods.** A hypothesis names a window of `claim_init_date`. Every window is
half-open - `[from, to)` - and both bounds fall on date boundaries. This is not
a stylistic choice: `claim_init_date` is a timestamp, and comparing a timestamp
to a date literal truncates the literal to midnight, so an inclusive upper
bound drops the whole of the final day without any error. Measured on the
warehouse:

    claim_init_date BETWEEN '2026-08-26' AND '2026-08-26'  ->      0 claims
    claim_init_date >= '2026-08-26' AND < '2026-08-27'     -> 46,684 claims

Relative periods run from `today - P` up to the end of today, so "3M" on
2026-08-27 means `[2026-05-27, 2026-08-28)` - the whole of today included, the
whole of the start day included.

**Sample size.** Cochran's formula with the finite population correction. The
useful property is that it flattens out: 385 claims at 95%/5% whether the
population is half a million or eighteen million, so a hypothesis over 4.5M
claims still asks for a few hundred reviews rather than an impossible number.
"""
import math
from datetime import date, timedelta

# The periods offered on the form, in the order they are shown. The label is
# what a reviewer picks; the offset is how far back the window starts.
PERIOD_CHOICES = [
    ("1Y", "Last 1 year"),
    ("6M", "Last 6 months"),
    ("3M", "Last 3 months"),
    ("1M", "Last 1 month"),
    ("1W", "Last 1 week"),
    ("YESTERDAY", "Yesterday"),
    ("TODAY", "Today"),
    ("CUSTOM", "Custom date range"),
]
PERIOD_KINDS = {kind for kind, _ in PERIOD_CHOICES}
PERIOD_LABELS = dict(PERIOD_CHOICES)
DEFAULT_PERIOD = "3M"

# Months back for the calendar-month periods; days back for the rest.
_MONTHS_BACK = {"1Y": 12, "6M": 6, "3M": 3, "1M": 1}
_DAYS_BACK = {"1W": 7}

CONFIDENCE_LEVELS = {90: 1.6449, 95: 1.9600, 99: 2.5758}
DEFAULT_CONFIDENCE = 95
DEFAULT_MARGIN = 0.05
# Worst-case (most conservative) response distribution: p=0.5 maximises p(1-p)
# and so the required sample. Anything else would need a prior nobody has.
_WORST_CASE_P = 0.5


class PeriodError(ValueError):
    """A period selection that cannot be turned into a window."""


def _minus_months(day, months):
    """`day` shifted back whole calendar months, clamped to a real date.

    Going back a month from the 31st has to land somewhere; the last day of the
    shorter month is the reading that keeps the window contiguous with the
    previous one.
    """
    month_index = (day.month - 1) - months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last_day))


def resolve_period(kind, custom_from=None, custom_to=None, today=None):
    """The half-open window `(from_date, to_date)` for a period selection.

    `today` is injectable so this can be tested, and so the caller can pass the
    *warehouse's* current_date rather than the web server's - the two machines
    need not agree, and the window has to mean what the warehouse thinks it
    means.

    Both returned values are `date`s and the window is always `[from, to)`, so
    `to_date` is the day *after* the last day included.
    """
    kind = (kind or DEFAULT_PERIOD).strip().upper()
    if kind not in PERIOD_KINDS:
        raise PeriodError(f"Unknown period {kind!r}; expected one of "
                          + ", ".join(k for k, _ in PERIOD_CHOICES))
    today = today or date.today()
    tomorrow = today + timedelta(days=1)

    if kind == "TODAY":
        return today, tomorrow
    if kind == "YESTERDAY":
        # Yesterday alone: the window ends where today begins.
        return today - timedelta(days=1), today
    if kind == "CUSTOM":
        if not custom_from or not custom_to:
            raise PeriodError("A custom period needs both a start and an end date.")
        start, end = _as_date(custom_from), _as_date(custom_to)
        if end < start:
            raise PeriodError("The custom period's end date is before its start date.")
        # The end date the user typed is inclusive, so the exclusive bound is
        # the following day - otherwise picking the same day twice would select
        # nothing at all.
        return start, end + timedelta(days=1)
    if kind in _MONTHS_BACK:
        return _minus_months(today, _MONTHS_BACK[kind]), tomorrow
    return today - timedelta(days=_DAYS_BACK[kind]), tomorrow


def _as_date(value):
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise PeriodError("Missing date.")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise PeriodError(f"{text!r} is not a date in YYYY-MM-DD form.") from exc


def describe_period(kind, custom_from=None, custom_to=None, today=None):
    """The period as a reviewer should read it, with the resolved dates spelled
    out - the whole point of showing it is that "3M" alone does not say which
    days are in and which are out."""
    kind = (kind or DEFAULT_PERIOD).strip().upper()
    try:
        start, end = resolve_period(kind, custom_from, custom_to, today)
    except PeriodError as exc:
        return str(exc)
    last_day = end - timedelta(days=1)
    label = PERIOD_LABELS.get(kind, kind)
    if start == last_day:
        return f"{label}: {start.isoformat()}"
    return f"{label}: {start.isoformat()} to {last_day.isoformat()} inclusive"


def sample_size(population, confidence_level=DEFAULT_CONFIDENCE, margin_of_error=DEFAULT_MARGIN,
                proportion=_WORST_CASE_P):
    """Reviews needed to describe `population` at the given confidence.

    Cochran's n0 with the finite population correction applied, rounded up:

        n0 = z^2 * p(1-p) / e^2
        n  = n0 / (1 + (n0 - 1) / N)

    Returns 0 for an empty population - there is nothing to sample - and never
    more than the population itself.
    """
    population = int(population or 0)
    if population <= 0:
        return 0
    z = CONFIDENCE_LEVELS.get(int(confidence_level))
    if z is None:
        raise ValueError(f"Unsupported confidence level {confidence_level!r}; expected one of "
                         + ", ".join(str(c) for c in sorted(CONFIDENCE_LEVELS)))
    margin = float(margin_of_error)
    if not 0 < margin < 1:
        raise ValueError(f"Margin of error must be between 0 and 1, got {margin_of_error!r}")
    n0 = (z * z * proportion * (1 - proportion)) / (margin * margin)
    corrected = n0 / (1 + (n0 - 1) / population)
    return min(population, math.ceil(corrected))
