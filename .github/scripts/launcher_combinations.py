#!/usr/bin/env python3
"""Enumerate and plan the repository's launcher combinations.

enumerate prints launcher paths and fragment references for diff scoping,
followed by six-column combo records: kind, launcher, launch words, joined
option, join words, placement. It covers every state of every axis the
launcher and its fragments declare, every copy a `zero_or_more` axis can add
with `stack join`, and every state of the axes of each copy the file
deploys, written as the `NAME.axis=option` launch words that select them. A
declared core_nodes list requests local placement in CI.

plan previews each combination through peppy stack resolve, its join
included. Constraints classify refused combinations. The skip file
classifies unavailable hardware and rollout dependencies. Successful plans
produce a matrix with the complete launch and join.

The JSON5 subset reader reports unsupported syntax with its file and line.
"""

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field

# peppy's own cross-combination check refuses to enumerate a selection space
# larger than this (daemon-config-internal, COMBINATION_CEILING); the CI holds
# itself to the same ceiling so a launcher peppy's check escalates is escalated
# here too rather than launching combinations for hours.
COMBINATION_CEILING = 2048

# Both constraint refusals (`... requires ..., which this selection (...) does
# not satisfy` and `... forbids ..., which this selection (...) has`) carry this
# phrase; no other resolution error does. It is what separates a combination
# refused by design from a launcher that is broken.
CONSTRAINT_REFUSAL_MARK = "which this selection"

# A join refused because the copy writes a stack instance differently from
# how it runs (`joining NAME would change INSTANCE, which already runs`)
# carries this phrase. Such a copy runs only when the file deploys it at
# launch, which the repository check covers; it is not a broken launcher.
JOIN_CHANGE_MARK = "would change"
#: The simulations pair one robot: a copy joining where the file's copy already pairs is launch-only too.
PAIRING_MARK = "is already paired"

# The name every previewed and launched copy joins under in CI. A copy name is
# unique on the stack, and the fleets deploy `alpha`.
COPY_NAME = "bravo"

CARDINALITIES = ("one", "zero_or_one", "zero_or_more")


# ---------------------------------------------------------------------------
# A JSON5-subset reader: objects, arrays, strings, numbers, the three literals,
# // and /* */ comments, unquoted keys, trailing commas. Nothing else.
# ---------------------------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class Json5Error(ValueError):
    """The file is not JSON5 this reader understands, with file and line."""


class _Parser:
    def __init__(self, text, label):
        self.text = text
        self.label = label
        self.pos = 0

    def fail(self, message):
        line = self.text.count("\n", 0, self.pos) + 1
        raise Json5Error(f"{self.label}:{line}: {message}")

    def peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char):
        if self.peek() != char:
            self.fail(f"expected {char!r}, found {self.peek()!r}")
        self.pos += 1

    def skip_ws(self):
        while True:
            while self.peek() in (" ", "\t", "\r", "\n"):
                self.pos += 1
            if self.text.startswith("//", self.pos):
                newline = self.text.find("\n", self.pos)
                self.pos = len(self.text) if newline < 0 else newline + 1
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    self.fail("unterminated block comment")
                self.pos = end + 2
            else:
                return

    def parse_document(self):
        self.skip_ws()
        value = self.parse_value()
        self.skip_ws()
        if self.pos != len(self.text):
            self.fail(f"trailing content after the document: {self.peek()!r}")
        return value

    def parse_value(self):
        self.skip_ws()
        char = self.peek()
        if char == "{":
            return self.parse_object()
        if char == "[":
            return self.parse_array()
        if char in ('"', "'"):
            return self.parse_string()
        match = _IDENT.match(self.text, self.pos)
        if match:
            word = match.group(0)
            self.pos = match.end()
            if word in ("true", "false", "null"):
                return {"true": True, "false": False, "null": None}[word]
            self.fail(f"bare word {word!r} is not a value")
        match = _NUMBER.match(self.text, self.pos)
        if match and char:
            self.pos = match.end()
            number = match.group(0)
            return float(number) if any(c in number for c in ".eE") else int(number)
        self.fail(f"expected a value, found {char!r}")

    def parse_object(self):
        self.expect("{")
        obj = {}
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.pos += 1
                return obj
            match = _IDENT.match(self.text, self.pos)
            if self.peek() in ('"', "'"):
                key = self.parse_string()
            elif match:
                key = match.group(0)
                self.pos = match.end()
            else:
                self.fail(f"expected a key, found {self.peek()!r}")
            self.skip_ws()
            self.expect(":")
            obj[key] = self.parse_value()
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "}":
                self.fail(f"expected ',' or '}}', found {self.peek()!r}")

    def parse_array(self):
        self.expect("[")
        arr = []
        while True:
            self.skip_ws()
            if self.peek() == "]":
                self.pos += 1
                return arr
            arr.append(self.parse_value())
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "]":
                self.fail(f"expected ',' or ']', found {self.peek()!r}")

    def parse_string(self):
        quote = self.peek()
        self.pos += 1
        out = []
        while True:
            if self.pos >= len(self.text):
                self.fail("unterminated string")
            char = self.text[self.pos]
            if char == quote:
                self.pos += 1
                return "".join(out)
            if char == "\n":
                self.fail("unterminated string (newline before the closing quote)")
            if char == "\\":
                self.pos += 1
                escape = self.peek()
                if escape == "u":
                    hex_digits = self.text[self.pos + 1 : self.pos + 5]
                    if len(hex_digits) != 4 or not all(
                        c in "0123456789abcdefABCDEF" for c in hex_digits
                    ):
                        self.fail("malformed \\u escape")
                    out.append(chr(int(hex_digits, 16)))
                    self.pos += 5
                elif escape in _ESCAPES:
                    out.append(_ESCAPES[escape])
                    self.pos += 1
                else:
                    self.fail(f"unknown escape \\{escape}")
            else:
                out.append(char)
                self.pos += 1


def load_json5(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise Json5Error(f"{label}: cannot read: {error}") from error
    return _Parser(text, label).parse_document()


# ---------------------------------------------------------------------------
# The launcher model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Axis:
    """One component axis, with the axes each of its options' fragments
    declare in turn."""

    name: str
    cardinality: str
    options: list[str]
    #: The option the document deploys on this axis, if it deploys one.
    deployed: str | None = None
    #: Per option, the axes its fragments declare.
    nested: dict[str, list["Axis"]] = field(default_factory=dict)

    @property
    def allows_unfilled(self):
        return self.cardinality != "one"

    @property
    def repeatable(self):
        """Runs as named copies: the file's `deployments`, then `stack join`."""
        return self.cardinality == "zero_or_more"


@dataclass(frozen=True)
class Copy:
    """A copy the file deploys: its name, the axis and option it runs, and
    the options its `with` selects on that option's own axes."""

    name: str
    axis: str
    option: str
    selected: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Launcher:
    """One launcher: its axes, the fragment files its options compose, the
    copies its file deploys, and its declared core-node placement."""

    axes: list[Axis]
    references: list[str]
    copies: list[Copy]
    declares_core_nodes: bool


def deployed_option(entry, label):
    """The `{ <axis>: "<option>" }` pair one `deployments` entry deploys, or
    None where the entry deploys a node under `source`."""
    if not isinstance(entry, dict):
        raise Json5Error(f"{label}: a `deployments` entry is an object")
    if "source" in entry:
        return None
    keys = [key for key in entry if key not in ("instances", "with", "arguments", "adjustments")]
    if len(keys) != 1 or not isinstance(entry[keys[0]], str):
        raise Json5Error(
            f"{label}: a `deployments` entry deploys a node under `source` or one "
            "component's option, `{ <component>: \"<option>\" }`"
        )
    return keys[0], entry[keys[0]]


def option_entries(document, label):
    """The `{ <axis>: "<option>" }` entries of a document's `deployments`,
    as an axis-to-option map, beside the node entries."""
    pairs = (deployed_option(entry, label) for entry in document.get("deployments", []))
    return dict(pair for pair in pairs if pair)


def deployed_copies(document, axes, label):
    """The copies a launcher's `deployments` run: one per `instances` entry
    of a repeatable axis, each carrying what its `with` selects on the axes
    of the option it runs, the entry's `with` under the copy's own."""
    repeatable = {axis.name for axis in axes if axis.repeatable}
    copies = []
    for entry in document.get("deployments", []):
        pair = deployed_option(entry, label)
        if pair is None or pair[0] not in repeatable:
            continue
        axis, option = pair
        instances = entry.get("instances")
        if not isinstance(instances, list) or not instances:
            raise Json5Error(
                f"{label}: axis `{axis}` runs as named copies: "
                f'`{{ {axis}: "{option}", instances: [{{ instance_id: "alpha" }}] }}`'
            )
        for instance in instances:
            name = instance.get("instance_id") if isinstance(instance, dict) else None
            if not isinstance(name, str):
                raise Json5Error(f"{label}: a copy of `{axis}` has no `instance_id`")
            selected = {**entry.get("with", {}), **instance.get("with", {})}
            copies.append(Copy(name, axis, option, selected))
    return copies


def read_axes(document, label, scope, read_option):
    """The axes a document declares, `read_option(axis, option, spec)`
    returning the axes the option's fragments declare."""
    components = document.get("components", [])
    if not isinstance(components, list):
        raise Json5Error(f"{label}: `components` is a list")
    deployed = option_entries(document, label)
    axes = []
    for component in components:
        if not isinstance(component, dict) or "name" not in component:
            raise Json5Error(f"{label}: a component of `components` has no `name`")
        name = component["name"]
        for refused in ("default", "optional", "components"):
            if refused in component:
                raise Json5Error(
                    f"{label}: component `{name}` declares `{refused}`; the option a document "
                    "starts with is a `deployments` entry, cardinality says how many may run, "
                    "and an option's fragment declares its own components"
                )
        options = component.get("options", {})
        if not isinstance(options, dict) or not options:
            raise Json5Error(f"{label}: component `{name}` declares no options")
        cardinality = component.get("cardinality", "one")
        if cardinality not in CARDINALITIES:
            raise Json5Error(f"{label}: use cardinality: one, zero_or_one, or zero_or_more")
        if scope == "fragment" and cardinality == "zero_or_more":
            raise Json5Error(
                f"{label}: component `{name}` declares zero_or_more inside a fragment; copies "
                "are the launcher's to deploy"
            )
        nested = {option: read_option(name, option, spec) for option, spec in options.items()}
        axes.append(Axis(name, cardinality, list(options), deployed.get(name), nested))
    for axis_name in deployed:
        if not any(axis.name == axis_name for axis in axes):
            raise Json5Error(f"{label}: `deployments` deploys `{axis_name}`, which is not a component")
    return axes


def fragment_parts(spec):
    """An option's fragment parts in order: paths and inline bodies."""
    parts = spec if isinstance(spec, list) else [spec]
    for part in parts:
        if not isinstance(part, (str, dict)):
            raise Json5Error("an option is a fragment path, an inline fragment, or a list of both")
    return parts


class Reader:
    """Reads a launcher and every fragment it composes, collecting the
    repository-relative path of each fragment file."""

    def __init__(self, root):
        self.root = root
        self.references = set()

    def launcher(self, path):
        document = load_json5(os.path.join(self.root, path), path)
        if not isinstance(document, dict):
            raise Json5Error(f"{path}: a launcher document is an object")
        directory = posixpath.dirname(path)

        def read_option(axis, option, spec):
            return self.option_axes(spec, directory, f"{path} option {axis}.{option}", depth=1)

        axes = read_axes(document, path, "launcher", read_option)
        return Launcher(
            axes,
            sorted(self.references),
            deployed_copies(document, axes, path),
            bool(document.get("core_nodes")),
        )

    def option_axes(self, spec, directory, label, depth):
        """The axes an option's fragments declare, their own options read
        one level down; a fragment two levels down declares none."""
        axes = []
        for part in fragment_parts(spec):
            if isinstance(part, str):
                reference = posixpath.normpath(posixpath.join(directory, part))
                self.references.add(reference)
                body = load_json5(os.path.join(self.root, reference), reference)
                body_directory = posixpath.dirname(reference)
                body_label = reference
            else:
                body, body_directory, body_label = part, directory, f"inline fragment of {label}"
            if not isinstance(body, dict):
                raise Json5Error(f"{body_label}: a fragment document is an object")
            if depth > 1:
                if body.get("components"):
                    raise Json5Error(
                        f"{body_label}: a fragment two levels below the launcher declares no components"
                    )
                continue

            def read_option(axis, option, nested_spec):
                return self.option_axes(nested_spec, body_directory, f"{body_label} option {axis}.{option}", depth + 1)

            axes.extend(read_axes(body, body_label, "fragment", read_option))
        return axes


def read_launcher(root, path):
    """One launcher and every fragment it composes."""
    return Reader(root).launcher(path)


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


def axis_states(axis):
    """Every state of one axis, peppy's order: options in declaration
    order, unfilled last where the cardinality allows."""
    states = list(axis.options)
    if axis.allows_unfilled:
        states.append(None)
    return states


def is_fixed(axis):
    """A `one` axis with a single option, deployed, holds that option in
    every launch and is no choice to write out; its option's axes stay in
    reach."""
    return axis.deployed is not None and axis.cardinality == "one" and len(axis.options) == 1


def selections_of(axes, head=()):
    """Every combination of the states of `axes` and of the axes their
    selected options bring in reach, as lists of `(axis, option)`."""
    selections = [list(head)]
    for axis in axes:
        extended = []
        for selection in selections:
            for option in axis_states(axis):
                entry = selection + ([] if is_fixed(axis) else [(axis.name, option)])
                nested = axis.nested.get(option, []) if option else []
                extended.extend(selections_of(nested, entry))
        selections = extended
    return selections


@dataclass(frozen=True)
class Combination:
    """One launch: the words over the stack's axes and, joined afterwards,
    at most one copy."""

    words: list
    join_option: str | None = None
    join_words: list = field(default_factory=list)


def copy_selections(axes, copy):
    """Every selection of a deployed copy's own axes other than the one it
    already runs, as the `NAME.axis=option` launch words that select it."""
    own = next((axis.nested.get(copy.option, []) for axis in axes if axis.name == copy.axis), [])
    runs = {axis.name: copy.selected.get(axis.name, axis.deployed) for axis in own}
    return [
        [(f"{copy.name}.{axis}", option) for axis, option in selection]
        for selection in selections_of(own)
        if any(option != runs.get(axis) for axis, option in selection)
    ]


def launcher_selections(axes, copies=()):
    """Every launch of the launcher, peppy's order: each stack selection
    bare, then with each deployed copy's own axes selected by launch word,
    then with every copy a repeatable axis can add joined onto it."""
    stack = [axis for axis in axes if not axis.repeatable]
    deployed = [words for copy in copies for words in copy_selections(axes, copy)]
    joined = [
        (option, selection)
        for axis in axes
        if axis.repeatable
        for option in axis.options
        for selection in selections_of(axis.nested.get(option, []))
    ]
    combinations = []
    for selection in selections_of(stack):
        combinations.append(Combination(selection))
        for words in deployed:
            combinations.append(Combination(selection + words))
        for option, own in joined:
            combinations.append(Combination(selection, option, own))
    return combinations


def render_words(selection):
    return ",".join(f"{axis}={option}" for axis, option in selection if option)


def command_enumerate(root):
    index = load_json5(
        os.path.join(root, "peppy_repository.json5"), "peppy_repository.json5"
    )
    launchers = index.get("launchers") if isinstance(index, dict) else None
    if not isinstance(launchers, dict) or not launchers:
        raise Json5Error("peppy_repository.json5: lists no `launchers`")
    for name, entry in launchers.items():
        if not isinstance(entry, dict) or "path" not in entry:
            raise Json5Error(f"peppy_repository.json5: launcher `{name}` has no `path`")
        path = entry["path"]
        launcher = read_launcher(root, path)
        combinations = launcher_selections(launcher.axes, launcher.copies)
        if len(combinations) > COMBINATION_CEILING:
            raise Json5Error(
                f"{path}: the selection space has {len(combinations)} combinations, "
                f"more than the {COMBINATION_CEILING} this check enumerates"
            )
        print(f"launcher\t{name}\t{path}\t{','.join(launcher.references)}")
        placement = "local" if launcher.declares_core_nodes else "-"
        for combination in combinations:
            print(
                "combo\t{}\t{}\t{}\t{}\t{}".format(
                    name,
                    render_words(combination.words),
                    combination.join_option or "",
                    render_words(combination.join_words),
                    placement,
                )
            )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def sanitize(text, limit=300):
    """One line, no tabs, no pipes (it lands in a markdown table), bounded."""
    line = re.sub(r"^(\[ERROR\] )?Error: ", "", text.strip())
    line = re.sub(r"[\t\r\n]+", " ", line).strip()
    if len(line) > limit:
        line = line[: limit - 3] + "..."
    return line.replace("|", "\\|")


def deployed_nodes(resolved):
    """The node names a flattened launcher deploys."""
    nodes = set()
    for deployment in resolved.get("deployments", []):
        source = deployment.get("source", {}) if isinstance(deployment, dict) else {}
        if isinstance(source, dict) and "name" in source:
            nodes.add(source["name"])
    return nodes


def combination_label(name, words, join_option, join_words, placement):
    """The launch job's display name: the launcher, its selection, its join,
    its placement."""
    label = f"{name} ({words})" if words else name
    if join_option:
        label += f" + join {join_option}"
        if join_words:
            label += f" ({join_words})"
    if placement == "local":
        label += " [--local]"
    return label


def disk_key(name, words, join_option, join_words, placement):
    """A slug naming this combination: the per-combination sticky-disk key.

    Keyed on the combination itself rather than its position in the list, so
    adding a launcher shifts nobody's disk and a warm run stays warm.
    """
    return re.sub(
        r"[^A-Za-z0-9]+", "-", f"{name} {words} {join_option} {join_words} {placement}"
    ).strip("-")


def resolve_command(path, words, join_option, join_words):
    """The preview of one combination: the launch, its join included."""
    argv = ["peppy", "stack", "resolve", path]
    if words:
        argv += ["--with", words]
    if join_option:
        argv += ["--join", join_option, "--join-name", COPY_NAME]
        if join_words:
            argv += ["--join-with", join_words]
    return argv


def command_plan(root, combos_path, skips_path, matrix_path):
    skips = {}
    for entry in load_json5(skips_path, skips_path) or []:
        if not isinstance(entry, dict) or "node" not in entry:
            raise Json5Error(f"{skips_path}: an entry has no `node`")
        skips[entry["node"]] = entry.get("reason", "no reason given")

    inventory = load_json5(os.path.join(root, "peppy_repository.json5"),
                           "peppy_repository.json5")
    paths = {
        name: entry["path"]
        for name, entry in inventory["launchers"].items()
    }

    planned = []
    matrix = []
    with open(combos_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 6 or fields[0] != "combo" or fields[1] not in paths:
                raise SystemExit(f"{combos_path}: not a combo line of this repository: {line!r}")
            _, name, words, join_option, join_words, placement = fields
            argv = resolve_command(paths[name], words, join_option, join_words)
            # peppy's own logging is held to errors, so stdout carries the
            # resolved plan alone and the JSON5 reader below takes it whole.
            resolve = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=root,
                env={**os.environ, "RUST_LOG": "error"},
            )
            record = (name, words, join_option, join_words, placement)
            if resolve.returncode == 0:
                nodes = deployed_nodes(_Parser(resolve.stdout, "resolved").parse_document())
                hits = [(node, skips[node]) for node in sorted(nodes) if node in skips]
                if hits:
                    detail = "; ".join(f"deploys {node}: {reason}" for node, reason in hits)
                    planned.append((*record, "skipped", detail))
                else:
                    planned.append((*record, "launch", "-"))
                    matrix.append(
                        {
                            "label": combination_label(*record),
                            "launcher": name,
                            "words": words,
                            "join_option": join_option,
                            "join_name": COPY_NAME if join_option else "",
                            "join_words": join_words,
                            "local": placement == "local",
                            "key": disk_key(*record),
                        }
                    )
            else:
                output = (resolve.stderr + resolve.stdout).strip()
                if CONSTRAINT_REFUSAL_MARK in output:
                    planned.append((*record, "refused", sanitize(output)))
                elif join_option and (JOIN_CHANGE_MARK in output or PAIRING_MARK in output):
                    planned.append((*record, "launch-only", sanitize(output)))
                else:
                    print(output, file=sys.stderr)
                    raise SystemExit(
                        f"{paths[name]} with `{words or 'defaults'}`"
                        f"{f' joining {join_option}' if join_option else ''} does not resolve "
                        "and the launcher's constraints do not refuse it either"
                    )

    with open(matrix_path, "w", encoding="utf-8") as handle:
        json.dump(matrix, handle, separators=(",", ":"))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The summary says, before anything launches, exactly which jobs the
        # run is about to fan out to. A `diff` block is the one construct a
        # step summary renders in color, so the launch list is green `+`
        # lines; the refused and skipped tables follow with their reasons.
        # The launch list names the launch jobs by their own labels, so what
        # the summary promises is what the checks list shows, verbatim.
        refused = [
            (combination_label(name, words, join_option, join_words, "-"), detail)
            for name, words, join_option, join_words, _, verdict, detail in planned
            if verdict == "refused"
        ]
        skipped = [
            (combination_label(name, words, join_option, join_words, "-"), detail)
            for name, words, join_option, join_words, _, verdict, detail in planned
            if verdict == "skipped"
        ]
        launch_only = [
            (combination_label(name, words, join_option, join_words, "-"), detail)
            for name, words, join_option, join_words, _, verdict, detail in planned
            if verdict == "launch-only"
        ]
        with open(summary, "a", encoding="utf-8") as handle:
            if matrix:
                handle.write(
                    f"\n### 🚀 Launching start to end, one job each "
                    f"({len(matrix)} of {len(planned)} combinations)\n\n"
                )
                handle.write("```diff\n")
                for entry in matrix:
                    handle.write(f"+ {entry['label']}\n")
                handle.write("```\n")
            else:
                handle.write(
                    "\n### 🚀 Launching nothing: every combination this change "
                    "reaches is refused or skipped\n\n"
                )
            if refused:
                handle.write(
                    f"\n### ❌ Refused by the launcher's own constraints "
                    f"({len(refused)})\n\n"
                )
                handle.write("| combination | refusal |\n")
                handle.write("| --- | --- |\n")
                for label, detail in refused:
                    handle.write(f"| {label} | {detail} |\n")
            if skipped:
                handle.write(
                    f"\n### ⏭️ Skipped: hardware or rollout dependencies ({len(skipped)})\n\n"
                )
                handle.write("| combination | deploys |\n")
                handle.write("| --- | --- |\n")
                for label, detail in skipped:
                    handle.write(f"| {label} | {sanitize(detail)} |\n")
            if launch_only:
                handle.write(
                    f"\n### 🧷 Copies the file deploys at launch, refused as a join "
                    f"({len(launch_only)})\n\n"
                )
                handle.write("| combination | join refusal |\n")
                handle.write("| --- | --- |\n")
                for label, detail in launch_only:
                    handle.write(f"| {label} | {detail} |\n")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            launchable = any(verdict == "launch" for *_, verdict, _ in planned)
            handle.write(f"launchable={'true' if launchable else 'false'}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)

    enumerate_parser = subcommands.add_parser(
        "enumerate", help="print the launcher and combination inventory"
    )
    enumerate_parser.add_argument("--root", default=".")

    plan_parser = subcommands.add_parser(
        "plan", help="classify selected combinations for this machine"
    )
    plan_parser.add_argument("--root", default=".")
    plan_parser.add_argument("--combos", required=True)
    plan_parser.add_argument("--skips", required=True)
    plan_parser.add_argument("--matrix", required=True)

    args = parser.parse_args()
    try:
        if args.command == "enumerate":
            command_enumerate(args.root)
        else:
            command_plan(args.root, args.combos, args.skips, args.matrix)
    except Json5Error as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
