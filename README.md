# roodle

Moodle VLE client

This library borrows an already logged-in Firefox session, and uses it to access the Moodle site, which is easier than trying to figure out how to do SSO log-in.

## Usage: assigning groups

Example of updating Moodle groups to match those listed in a CSV file:

``` python
import pandas as pd
from roodle import connect_via_firefox

course = connect_via_firefox("https://moodle.bath.ac.uk", 12345)
course.fetch_users()

df = pd.read_csv("group_assignments.csv", index_col="Username")
assigned_groups = df["Group"].to_dict()
group_counts = df.groupby("Group")["Group"].count().to_dict()

SD_groups = [g['value'] for g in course.groups if g['value'].startswith("SD-")] + ["Solo groups"]

for k, group in assigned_groups.items():
    # print(k, group)
    new_groups = [group]
    if group_counts.get(group) == 1:
        new_groups += ["Solo groups"]
    course.modify_user_groups(k, remove=SD_groups, add=new_groups)
```

You might use groups for multiple purposes, so the example does not remove all groups - just those starting with a given prefix (`SD-101`, `SD-102`, etc).

## Usage: downloading assignment data

This example downloads the "marking spreadsheet" from a Moodle assignment in CSV format.  The result is the same as going to the website and downloading it manually, but more convenient.

``` python
from pathlib import Path
import pandas as pd
from roodle import connect_via_firefox


OUTPUT_FOLDER = Path("inputs")


if __name__ == "__main__":
    print("Connecting to Moodle...")
    course = connect_via_firefox("https://moodle.bath.ac.uk", 12345)

    new_text = course.get_assignment_data(1234567)
    new_filename = OUTPUT_FOLDER / "moodle_assignment.csv"
    with open(new_filename, "wt") as f:
        f.write(new_text)
    print(f"Assignment metadata written to {new_filename}")

    # Get final grades
    (OUTPUT_FOLDER / f"moodle_grades.csv").write_text(course.get_grades())
```

