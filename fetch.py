from ast import literal_eval
import dataclasses
import datetime
import json
from html.parser import HTMLParser
import re
import sys
from typing import Any, List, Mapping, Optional

import requests
from slimit import ast
from slimit.parser import Parser
from slimit.visitors import nodevisitor


# The Reach, Charlton/Woolwich - https://www.thereach.org.uk/
URL = "https://portal.rockgympro.com/portal/public/be94788ef672908b57b32977c18452dc/occupancy?&iframeid=occupancyCounter&fId="


class ScriptExtractorParser(HTMLParser):
    """Extract the contents of <script> tags"""

    scripts: List[str]
    scrape_data: bool

    def __init__(self):
        super().__init__()

        self.scripts = []
        self.scrape_data = False

    def handle_starttag(self, tag: str, attrs: Mapping[str, str]):
        self.scrape_data = tag.lower() == 'script'
        # Create a "bucket" to store the <script> tag contents; reference it later via scripts[-1]
        if self.scrape_data:
            self.scripts.append("")

    def handle_endtag(self, tag: str):
        # If the script contents was empty (as it would be for include-only scripts); delete it
        if self.scrape_data:
            if len(self.scripts[-1].strip()) == 0:
                del self.scripts[-1]

        self.scrape_data = False

    def handle_data(self, data: str):
        # Append the script contents
        if self.scrape_data:
            self.scripts[-1] += data.strip()


@dataclasses.dataclass
class OccupancySnapshot:
    capacity: int
    occupancy: int
    updated_at: datetime.datetime
    label: Optional[str]


def fetch_rockgympro_occupancy(url: str) -> Mapping[str, OccupancySnapshot]:
    res = requests.get(url)

    # Parse out <script> tags
    html_parser = ScriptExtractorParser()
    html_parser.feed(res.text)

    for script in html_parser.scripts:
        # A bit hacky (we could check in the ast.VarDecl bit for example), but it saves parsing JS unncessarily
        if 'var data = ' in script:
            # Parse JS
            js_parser = Parser()
            tree = js_parser.parse(script)

            def extract_object(node: ast.Object, base: Optional[Mapping] = None) -> Mapping:
                """
                Recursively extract JS assignments and objects to Python

                TODO: Could probably be generalised to to_python(node: ast.Node); but it works for now.

                """

                if base is None:
                    base = {}

                # Iterate each assignment ({"key": "value"}) in the js object
                for assign in node.children():
                    # Keys should always be strings
                    # TODO: Does this work with object properties that aren't in string literals? e.g.:
                    #    {"ok": ..., notsure: ..., [dynamic]: ...}
                    assert isinstance(assign.left, ast.String)
                    left = assign.left.value.strip('"\'')

                    # Cast different assignment values to Python equivalents
                    if isinstance(assign.right, ast.Object):
                        right = extract_object(assign.right)
                    elif isinstance(assign.right, ast.String):
                        right = assign.right.value.strip('"\'')
                    elif isinstance(assign.right, ast.Number):
                        right = literal_eval(assign.right.value)
                    elif isinstance(assign.right, ast.Array):
                        right = [extract_object(each) for each in assign.right.children()]
                    elif isinstance(assign.right, ast.Null):
                        right = None
                    else:
                        raise ValueError(assign.right)

                    base[left] = right

                return base

            # Extract the var data = { ... }; declaration
            data = {}
            for node in nodevisitor.visit(tree):
                if isinstance(node, ast.VarDecl) and node.identifier.value == "data":
                    data = extract_object(node.initializer)

            # Emit each OccupancySnapshot
            occupancies = {}
            for name, counter in data.items():
                # Convert "Last updated: now (3:13 PM)" to a datetime
                match = re.search('\((?P<hours>\d+):(?P<mins>\d+) (?P<tod>(AM|PM))\)$', counter['lastUpdate'])
                time_str = "%02d:%02d %s" % (int(match.group("hours")), int(match.group("mins")), match.group("tod"))
                updated_at = datetime.datetime.strptime(time_str, "%I:%M %p").time()

                occupancies[name] = {
                    "capacity": counter['capacity'],
                    "occupancy": counter['count'],
                    "updated_at": datetime.datetime.combine(datetime.date.today(), updated_at).isoformat(),
                    "label": counter['subLabel'],
                    }

            return occupancies


if __name__ == "__main__":
    occupancies = fetch_rockgympro_occupancy(URL)

    # json.dump(occupancies, sys.stdout)

    # Emit three StatsD metrics: occupancy (number of people in gym), capacity and an occupancy percentage
    # I'm using Gauges, as these are snapshots of figures. See https://statsd.readthedocs.io/en/v0.5.0/types.html#gauges
    for name, occupancy in occupancies.items():
        occupancy_pc = (occupancy['occupancy'] / occupancy['capacity']) * 100

        # Name format: gym.{name}.{metric} - name will be whatever key was in the var data declaration (in this case, AAA)
        # The name can be referenced from graphite via: statsd.gauges.gym.{name}.{metric}, e.g.:
        # statsd.gauges.gym.AAA.occupancy_pc
        print(f"gym.{name}.occupancy:{occupancy['occupancy']}|g")
        print(f"gym.{name}.capacity:{occupancy['capacity']}|g")
        print(f"gym.{name}.occupancy_pc:{occupancy_pc}|g")

