import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launcher_combinations as combinations


def fleet_axes():
    """A simulation axis the file deploys, a scene commander it leaves off,
    and a robot axis running as copies whose fragment declares a robot
    commander and a recorder."""
    commander = combinations.Axis("robot_commander", "one", ["web_commander", "xr_commander"], deployed="web_commander")
    recorder = combinations.Axis("recorder", "zero_or_one", ["lerobot_recorder"])
    return [
        combinations.Axis("simulation", "one", ["mujoco", "isaac_sim"], deployed="mujoco"),
        combinations.Axis("scene_commander", "zero_or_one", ["web_scene_commander"]),
        combinations.Axis(
            "robot", "zero_or_more", ["openarm_v2"], nested={"openarm_v2": [commander, recorder]}
        ),
    ]


@contextlib.contextmanager
def planned(combo_line):
    """One combo line planned against a one-launcher repository, with
    `stack resolve` answering an empty plan: the recorded subprocess call
    and the matrix the plan wrote."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "peppy_repository.json5").write_text(json.dumps({
            "launchers": {"fleet": {"path": "fleet.json5"}},
        }))
        combos = root / "combos.tsv"
        combos.write_text(combo_line)
        skips = root / "skips.json5"
        skips.write_text("[]")
        matrix = root / "matrix.json"
        response = combinations.subprocess.CompletedProcess([], 0, '{"deployments": []}', "")
        with patch.object(combinations.subprocess, "run", return_value=response) as run:
            combinations.command_plan(root, combos, skips, matrix)
        yield run, json.loads(matrix.read_text())


class CombinationsTests(unittest.TestCase):
    def test_every_state_of_every_axis_in_reach_is_enumerated(self):
        combinations_found = combinations.launcher_selections(fleet_axes())
        # Stack: simulation (2) times scene commander (1 + unfilled) = 4, each
        # bare and with every copy: robot commander (2) times recorder (1 +
        # unfilled) = 4.
        self.assertEqual(len(combinations_found), 4 * (1 + 4))
        bare = [c for c in combinations_found if c.join_option is None]
        self.assertIn([("simulation", "mujoco"), ("scene_commander", None)], [c.words for c in bare])
        joined = [c for c in combinations_found if c.join_option == "openarm_v2"]
        self.assertIn(
            [("robot_commander", "xr_commander"), ("recorder", None)], [c.join_words for c in joined]
        )
        self.assertEqual(
            combinations.render_words([("simulation", "mujoco"), ("scene_commander", None)]), "simulation=mujoco"
        )

    def test_a_one_axis_with_a_single_option_is_no_choice(self):
        control = combinations.Axis("control", "one", ["control_common"], deployed="control_common")
        commander = combinations.Axis("robot_commander", "one", ["web_commander", "xr_commander"], deployed="web_commander")
        found = combinations.selections_of([control, commander])
        self.assertEqual(
            found,
            [[("robot_commander", "web_commander")], [("robot_commander", "xr_commander")]],
        )
        # A single option nothing deploys is a choice the launch has to
        # write out.
        undeployed = combinations.Axis("control", "one", ["control_common"])
        self.assertEqual(
            combinations.selections_of([undeployed]), [[("control", "control_common")]]
        )

    def test_a_one_axis_selected_by_the_file_brings_its_options_axes_in_reach(self):
        commander = combinations.Axis("robot_commander", "one", ["web_commander", "xr_commander"], deployed="web_commander")
        axes = [combinations.Axis("robot", "one", ["openarm_v2"], deployed="openarm_v2", nested={"openarm_v2": [commander]})]
        found = combinations.launcher_selections(axes)
        # The robot is the launcher's single deployed option, so the words
        # are its commander's alone.
        self.assertEqual(
            [c.words for c in found],
            [[("robot_commander", "web_commander")], [("robot_commander", "xr_commander")]],
        )

    def test_hub_inventory_covers_every_launcher_and_its_copies(self):
        root = Path(__file__).resolve().parents[2]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            combinations.command_enumerate(root)
        lines = output.getvalue().splitlines()

        def combos(launcher):
            return [line.split("\t") for line in lines if line.startswith(f"combo\t{launcher}\t")]

        # so101: one robot, a single option the file deploys and no choice to
        # write out, times its fragment's commander (3), recorder
        # (1 + unfilled) and camera_rig (1 + unfilled).
        self.assertEqual(len(combos("so101")), 3 * 2 * 2)
        self.assertIn(
            ["combo", "so101", "robot_commander=xr_commander,recorder=lerobot_recorder,camera_rig=cameras", "", "", "-"],
            combos("so101"),
        )
        # The real fleet: one bare launch of its deployed copies, then a join
        # of each robot option under every selection of its own axes:
        # robot_commander (3) times recorder (2) times camera_rig (2), the
        # v2 also times brain (2).
        real = combos("openarm_real_fleet")
        self.assertEqual(len(real), 1 + 3 * 2 * 2 + 3 * 2 * 2 * 2)
        self.assertIn(["combo", "openarm_real_fleet", "", "", "", "-"], real)
        self.assertIn(
            ["combo", "openarm_real_fleet", "", "openarm_v1", "robot_commander=xr_commander,recorder=lerobot_recorder,camera_rig=cameras", "-"],
            real,
        )
        # The simulated fleet: one stack selection per simulation, Isaac Sim
        # and Waldo each with and without their scene commander (5), each
        # bare and joined by either simulated robot: the v1 has no camera
        # rig and no brain (3 times 2), the v2 has both (3 times 2 times 2
        # times 2).
        sim = combos("openarm_sim_fleet")
        self.assertEqual(len(sim), 5 * (1 + 3 * 2 + 3 * 2 * 2 * 2))
        self.assertIn(["combo", "openarm_sim_fleet", "simulation=waldo,scene_commander=web_scene_commander", "", "", "-"], sim)
        self.assertIn(
            ["combo", "openarm_sim_fleet", "simulation=mujoco", "openarm_v2_sim", "robot_commander=xr_commander,recorder=lerobot_recorder,camera_rig=cameras_sim", "-"],
            sim,
        )
        # The generic fleet: the simulation off or any of the five stack
        # selections above, each bare and joined by any of the four robots.
        generic = combos("openarm_generic_fleet")
        self.assertEqual(
            len(generic), 6 * (1 + 3 * 2 * 2 + 3 * 2 * 2 * 2 + 3 * 2 + 3 * 2 * 2 * 2)
        )
        self.assertIn(["combo", "openarm_generic_fleet", "", "openarm_v2_sim", "robot_commander=web_commander", "-"], generic)
        references = next(line for line in lines if line.startswith("launcher\topenarm_sim_fleet\t"))
        for reference in [
            "openarm/fragments/openarm_v1_sim.json5",
            "openarm/fragments/openarm_v2_sim.json5",
            "openarm/fragments/cameras_sim.json5",
            "robot_commanders/fragments/xr_commander.json5",
            "recording/fragments/lerobot_recorder.json5",
            "simulation/fragments/waldo.json5",
            "simulation/fragments/web_scene_commander.json5",
        ]:
            self.assertIn(reference, references)

    def test_fragment_files_are_named_for_their_option(self):
        root = Path(__file__).resolve().parents[2]
        for document in sorted(root.rglob("*.json5")):
            if document.name == "peppy_repository.json5" or "examples" in document.parts or ".github" in document.parts:
                continue
            for axis in combinations.load_json5(document, str(document)).get("components", []):
                for option, path in axis["options"].items():
                    self.assertEqual(Path(path).stem, option, f"{document}: {axis['name']}={option} selects {path}")

    def test_fragment_paths_follow_domain_layout_and_are_all_referenced(self):
        root = Path(__file__).resolve().parents[2]
        index = combinations.load_json5(root / "peppy_repository.json5", "index")
        references = set()
        for entry in index["launchers"].values():
            _, paths, _ = combinations.read_launcher(root, entry["path"])
            references.update(paths)
        fragments = set()
        for path in root.rglob("*.json5"):
            document = combinations.load_json5(path, str(path))
            if not isinstance(document, dict) or document.get("peppy_schema") != "launcher_fragment/v1":
                continue
            relative = path.relative_to(root)
            self.assertEqual(relative.parts[1], "fragments", str(relative))
            self.assertEqual(len(relative.parts), 3, str(relative))
            fragments.add(relative.as_posix())
        self.assertEqual(references, fragments)

    def test_both_robots_share_capabilities_and_own_their_tuning(self):
        root = Path(__file__).resolve().parents[2]
        for robot, launcher, robot_path, tuning_path, command_rate, fps in [
            ("openarm", "openarm/openarm_sim_fleet.json5", "openarm/fragments/openarm_v2_sim.json5",
             "openarm/fragments/control_common.json5", 100, 15),
            ("so101", "so101/so101.json5", "so101/fragments/so101.json5", "so101/fragments/so101.json5", 60, 30),
        ]:
            with self.subTest(robot=robot):
                _, references, _ = combinations.read_launcher(root, launcher)
                self.assertIn("robot_commanders/fragments/xr_commander.json5", references)
                self.assertIn("recording/fragments/lerobot_recorder.json5", references)
                fragment = combinations.load_json5(root / robot_path, robot)
                axes = {axis["name"] for axis in fragment["components"]}
                self.assertEqual({"robot_commander", "recorder"} - axes, set())
                self.assertIn("robot_commander", combinations.option_entries(fragment, robot))
                control = combinations.load_json5(root / tuning_path, robot)
                commander = next(adjustment for adjustment in control["adjustments"]
                                 if adjustment["target"] == "commander_inst"
                                 and adjustment.get("when") == {"robot_commander": "xr_commander"})
                self.assertEqual(commander["set_arguments"]["command_rate_hz"], command_rate)
                self.assertEqual(commander["set_arguments"]["gripper_open_fraction"], 0.5)
                recorder = next(adjustment for adjustment in control["adjustments"]
                                if adjustment["target"] == "recorder_inst")
                self.assertEqual(recorder["set_arguments"]["fps"], fps)
        for path, robot_arguments in [
            ("robot_commanders/fragments/xr_commander.json5", {"command_rate_hz", "gripper_open_fraction"}),
            ("recording/fragments/lerobot_recorder.json5", {"fps", "robot_type", "storage_root"}),
        ]:
            document = combinations.load_json5(root / path, path)
            for deployment in document["deployments"]:
                for instance in deployment["instances"]:
                    self.assertTrue(robot_arguments.isdisjoint(instance.get("arguments", {})), path)

    def test_plan_previews_the_launch_and_its_join_and_launches_the_same(self):
        line = "combo\tfleet\tsimulation=mujoco\topenarm_v2\trobot_commander=xr_commander\t-\n"
        with planned(line) as (run, matrix):
            self.assertEqual(run.call_args.args[0], [
                "peppy", "stack", "resolve", "fleet.json5", "--with", "simulation=mujoco",
                "--join", "openarm_v2", "--join-name", combinations.COPY_NAME, "--join-with", "robot_commander=xr_commander",
            ])
            # The preview reads the plan as JSON5, so peppy logs errors only.
            self.assertEqual(run.call_args.kwargs["env"]["RUST_LOG"], "error")
            entry, = matrix
            self.assertEqual(entry["words"], "simulation=mujoco")
            self.assertEqual(entry["join_option"], "openarm_v2")
            self.assertEqual(entry["join_name"], combinations.COPY_NAME)
            self.assertEqual(entry["join_words"], "robot_commander=xr_commander")
            self.assertEqual(entry["label"], "fleet (simulation=mujoco) + join openarm_v2 (robot_commander=xr_commander)")

    def test_plan_previews_a_launch_with_no_join(self):
        words = "simulation=mujoco,scene_commander=web_scene_commander"
        with planned(f"combo\tfleet\t{words}\t\t\t-\n") as (run, matrix):
            self.assertEqual(run.call_args.args[0], [
                "peppy", "stack", "resolve", "fleet.json5", "--with", words,
            ])
            entry, = matrix
            self.assertEqual(entry["words"], words)
            self.assertEqual(entry["join_option"], "")
            self.assertEqual(entry["join_name"], "")
            self.assertEqual(entry["label"], f"fleet ({words})")

    def test_waldo_scene_commander_links_the_simulation(self):
        root = Path(__file__).resolve().parents[2]
        for launcher in ["openarm/openarm_sim_fleet.json5"]:
            document = combinations.load_json5(root / launcher, launcher)
            plugins = [adjustment["set_arguments"]["plugins"]
                       for adjustment in document["adjustments"]
                       if "plugins" in adjustment.get("set_arguments", {})]
            self.assertEqual(plugins, ["hand_teleop"], launcher)
        scene = combinations.load_json5(
            root / "simulation/fragments/web_scene_commander.json5", "web_scene_commander")
        instance = scene["deployments"][0]["instances"][0]
        self.assertEqual(instance["links"]["simulation"], "simulation_inst")

    def test_an_option_entry_may_carry_settings_shared_by_its_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fleet.json5").write_text(json.dumps({"components": [
                {"name": "robot", "cardinality": "zero_or_more", "options": {"openarm_v2": {
                    "components": [{"name": "robot_commander", "options": {"web_commander": {}, "xr_commander": {}}}],
                    "deployments": [{"robot_commander": "web_commander"}],
                }}},
            ], "deployments": [
                {"robot": "openarm_v2", "with": {"robot_commander": "xr_commander"},
                 "arguments": {"commander_inst": {"https_port": 4444}},
                 "instances": [
                     {"instance_id": "alpha"},
                     {"instance_id": "bravo"},
                 ]},
            ]}))
            axes, _, _ = combinations.read_launcher(directory, "fleet.json5")
            self.assertEqual([axis.name for axis in axes], ["robot"])

    def test_refused_keys_and_cardinalities_are_rejected(self):
        for extra in [{"optional": True}, {"cardinality": "many"},
                      {"default": "openarm_v2"}, {"components": []}]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                Path(directory, "fleet.json5").write_text(json.dumps({"components": [
                    {"name": "robot", "options": {"openarm_v2": {}}, **extra},
                ]}))
                with self.assertRaises(combinations.Json5Error):
                    combinations.read_launcher(directory, "fleet.json5")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fleet.json5").write_text(json.dumps({"components": [
                {"name": "robot", "options": {"openarm_v2": {
                    "components": [{"name": "cameras", "cardinality": "zero_or_more", "options": {"a": {}}}],
                }}},
            ]}))
            with self.assertRaises(combinations.Json5Error):
                combinations.read_launcher(directory, "fleet.json5")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fleet.json5").write_text(json.dumps({"components": [
                {"name": "robot", "options": {"openarm_v2": {
                    "components": [{"name": "robot_commander", "options": {"web_commander": {
                        "components": [{"name": "deep", "options": {"a": {}}}],
                    }}}],
                }}},
            ]}))
            with self.assertRaises(combinations.Json5Error):
                combinations.read_launcher(directory, "fleet.json5")


if __name__ == "__main__":
    unittest.main()
