# deltaTime.py
#
# From pyparsing examples but much simplified
#

from datetime import timedelta
import pyparsing as pp

__all__ = ["time_expression"]


# basic grammar definitions
CK = pp.CaselessKeyword
CL = pp.CaselessLiteral


def plural(s):
    return CK(s) | CK(s + "s").addParseAction(pp.replaceWith(s))


year, week, day, hour, full_minute, full_second = map(
    plural, "year week day hour minute second".split()
)
short_minute = CK("min").addParseAction(pp.replaceWith("minute")) | CK(
    "mins"
).addParseAction(pp.replaceWith("minute"))
short_second = CK("sec").addParseAction(pp.replaceWith("second")) | CK(
    "secs"
).addParseAction(pp.replaceWith("second"))
minute = short_minute | full_minute
second = short_second | full_second
time_units = year | week | day | hour | minute | second


late = CK("late").setParseAction(pp.replaceWith(1))


UNIT_SECONDS = {
    "year": 365 * 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "day": 24 * 60 * 60,
    "hour": 60 * 60,
    "minute": 60,
    "second": 1,
}

qty_and_units = pp.pyparsing_common.integer("qty") + time_units("units")


def compute_time_delta(t):
    delta_seconds = UNIT_SECONDS[t.units] * t.qty
    t["delta_seconds"] = delta_seconds


qty_and_units.addParseAction(compute_time_delta)

#
# Relative time reference with multiple pairs of qty / units
#

relative_time_reference = (
    pp.OneOrMore(pp.Group(qty_and_units))("items") + late("dir")
).setName("relative time")


def compute_relative_time(t):
    delta_seconds = sum(x["delta_seconds"] for x in t["items"])
    t["time_delta"] = timedelta(seconds=t.dir * delta_seconds)
    t["total_seconds"] = t["time_delta"].total_seconds()


relative_time_reference.addParseAction(compute_relative_time)


# parse actions for total time_and_day expression
def save_original_string(s, _, t):
    # save original input string and reference time
    t["original"] = " ".join(s.strip().split())


def remove_temp_keys(t):
    # strip out keys that are just used internally
    all_keys = list(t.keys())
    for k in all_keys:
        if k not in ("original", "time_delta", "total_seconds"):
            del t[k]


relative_time_reference.addParseAction(save_original_string, remove_temp_keys)


time_expression = relative_time_reference


def main():
    # test grammar
    tests = """\
        10 secs late
        100 secs late
        10 mins late
        1 hour late
        2 hours late
        1 day late
        2 days late
        2 days 3 hours late
    """

    expected = {
        "10 secs late": timedelta(seconds=10),
        "100 secs late": timedelta(seconds=100),
        "10 mins late": timedelta(minutes=10),
        "1 hour late": timedelta(hours=1),
        "2 hours late": timedelta(hours=2),
        "1 day late": timedelta(days=1),
        "2 days late": timedelta(days=2),
        "2 days 3 hours late": timedelta(days=2, hours=3),
    }

    def verify_offset(instring, parsed):
        time_epsilon = timedelta(seconds=1)
        if instring in expected:
            # allow up to a second time discrepancy due to test processing time
            if (parsed.time_delta - expected[instring]) <= time_epsilon:
                parsed["verify_offset"] = "PASS"
            else:
                parsed["verify_offset"] = "FAIL"

    time_expression.runTests(tests, postParse=verify_offset)


if __name__ == "__main__":
    main()
