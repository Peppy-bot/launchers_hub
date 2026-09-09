# OpenArm launchers and fragments

Each OpenArm is one fragment: `openarm_v1` and `openarm_v2` drive the
physical robot over CAN, `openarm_v1_sim` and `openarm_v2_sim` relay a
simulation's limbs. Every fragment selects the shared OpenArm control
([fragments/control_common.json5](fragments/control_common.json5)), the
robot's initializer and backbone, and declares the robot's own axes: the
robot commander, the recorder, and the camera rig. Three launchers compose
them:
[openarm_sim_fleet.json5](openarm_sim_fleet.json5) runs one simulation and
simulated robots as copies,
[openarm_real_fleet.json5](openarm_real_fleet.json5) runs physical robots as
copies, one machine each, and
[openarm_generic_fleet.json5](openarm_generic_fleet.json5) deploys nothing
and takes any mix from the command line. A copy's ids carry its name:
`alpha_backbone_inst`. See the [repository README](../Readme.md) for the
option table, limits, and configuration ownership.

## Real hardware

Bring up the CAN interfaces before launching a robot. The v1 fragment uses
`can0` and `can1`. The v2 fragment uses the `left_arm` and `right_arm`
names assigned by the host's `80-openarm-can.rules`:

```sh
sudo ip link set left_arm up type can bitrate 1000000 dbitrate 5000000 fd on
sudo ip link set right_arm up type can bitrate 1000000 dbitrate 5000000 fd on
```

For v1, use `can0` and `can1` in those commands. Check the actual adapters
and hardware generation before enabling the buses.

```sh
peppy repo refresh
peppy stack launch openarm_real_fleet                    # alpha, a v2 with the browser panel
peppy stack launch openarm_real_fleet --place alpha@jetson-1
peppy stack join openarm_v1 -i bravo --place bravo@jetson-2
peppy stack list
peppy stack remove alpha
peppy stack reset --federated
```

The browser panel defaults to `http://<robot-host>:8765`. Launch returns
after startup. `stack remove alpha` stops every instance owned by alpha;
`stack reset --federated` tears down the fleet.

The hardware generation applies to the arms, grippers, backbone and browser
panel. A mismatched gripper setting has these physical consequences:

| Rig | Incorrect setting | Consequence |
|---|---|---|
| v1 left | `v2` | drives backwards into the closed stop |
| v1 right | `v2` | drives 50% past the open stop |
| v2 left | `v1` | drives backwards into the closed stop |
| v2 right | `v1` | opens two thirds of the way and reports fully open |

The same setting selects MIT hold for v1 and POS_FORCE control for v2.
Select the option matching the physical robot.

## Commanders, recording, and cameras

These are the robot's own axes, declared by its fragments, and selected per
copy: with `with:` in the file, or `--with` on `stack join`:

```sh
peppy stack join openarm_v2 -i bravo --with xr_commander,lerobot_recorder,cameras
peppy stack join openarm_v1 -i charlie --with mcp_commander --place charlie@jetson-2
peppy stack join openarm_v2 -i delta --with web_commander,ai_brain
```

The default web commander streams joint setpoints. XR streams end-effector
poses and selects the backbone's pose mode. MCP exposes the backbone's discrete
motion actions through the built-in `openarm_v2:v1` exposure, for either
hardware generation.

The MCP endpoint defaults to
`http://127.0.0.1:8900/openarm_v2/v1/mcp`; `stack list` reports its endpoints.
Recording requires the web or XR commander.

A v2 copy's `brain` axis adds `ai_brain`, the environment aware action layer
serving `item_perception` and `item_manipulation` over the backbone's
`limb_motion`, with the MCP server built into peppy serving the `ai_brain:v1`
exposure on port 8901. It composes with any commander: the operator and the
brain send the same kind of goal to the same producer.

Install [the camera udev rules](rules/99-openarm-cameras.rules), following their
header, before selecting `cameras`. That option brings up both wrist cameras
and the chest camera and attaches them to the recorder or headset.

For XR, open the HTTPS URL printed in the commander's log and accept its
self-signed certificate. Over USB, enable developer mode and run
`adb reverse tcp:4443 tcp:4443`, then open `https://localhost:4443`.

Hold a grip button to move the matching arm; release it to hold position.
The trigger controls that hand's gripper while grip is held. A moves home,
B moves ready, and either grip cancels a posture move. With recording selected,
X starts or saves an episode; holding Y for one second finishes the session.
Set the demonstration task at `https://<host>:4443/task` before recording.

For two robots on one host, assign the second copy's CAN interfaces,
commander port, and dataset directory:

```sh
peppy stack join openarm_v2 -i bravo --with lerobot_recorder \
  --set-arguments 'left_arm_inst.can_interface="bravo_left"' \
  --set-arguments 'left_gripper_inst.can_interface="bravo_left"' \
  --set-arguments 'right_arm_inst.can_interface="bravo_right"' \
  --set-arguments 'right_gripper_inst.can_interface="bravo_right"' \
  --set-arguments 'commander_inst.http_port=8767' \
  --set-arguments 'recorder_inst.storage_root="/tmp/lerobot_bravo"'
```

A second copy with a brain also takes a distinct `brain_mcp_inst.port`.

Arguments name the instance as the fragment writes it, such as
`left_arm_inst.can_interface`; the copy's name prefixes the running instance.

## Who leads, and in which space

The backbone follows exactly one kind of upstream arm command, named by its required `upstream_mode` argument, and subscribes only that kind of arm slot (gripper and posture slots are read under either mode):

- `"joints"` - `openarm_web_commander` (the browser panel) streams joint setpoints on `joint_link`. The commander every robot fragment deploys.
- `"pose"` - `xr_commander` streams an end-effector pose per hand on `pose_link`, and the backbone solves it. The robot fragment selects the mode and re-vacates the slots as part of being selected.
- Nobody streams - `mcp_commander` drives the backbone through discrete actions only: the whole-robot posture moves and the per-limb arm and gripper moves it exposes as tools. `upstream_mode` stays `"joints"`, all six leader sockets are vacant with their reasons, and the governor keeps its launch-time band, enable, and EE-speed caps for the whole session, as under the headset.

One or the other, never both: a backbone reading two command authorities for one arm is not a state the mode can express. An arm slot of the kind the mode does *not* name would never be read, so linking one refuses the launch, naming every offending slot.

The `xr_commander` selection runs without `openarm_web_commander` entirely. `governor_control` is an optional backbone feature, since not every leader can produce it (`xr_commander` is robot-agnostic, so it never will): with no producer bound, the governor runs on the backbone's launch-time band, enable, and EE-speed cap for the whole session. To retune, edit the backbone arguments and relaunch, or use the panel.

Recording adds `lerobot_recorder` (see the recorder's README in nodes-hub for the dataset workflow). The `cameras` option adds the three cameras, whose device paths come from `rules/99-openarm-cameras.rules` (install it per the file's header); the headset retunes them for in-headset panels as part of its own selection.

## Simulation

The simulated fleet runs the simulation as the stack and the robot as a
copy. The file deploys Waldo and `alpha`, a v2 with the browser commander:

```sh
peppy stack launch openarm_sim_fleet                  # Waldo and alpha
peppy stack launch openarm_sim_fleet --with mujoco    # the same copy in MuJoCo
peppy stack launch openarm_sim_fleet --with isaac_sim
peppy stack remove alpha                              # the simulation keeps running
peppy stack join openarm_v2_sim -i bravo --with xr_commander
peppy stack remove bravo                              # one simulated robot at a time
peppy stack join openarm_v2_sim -i charlie --with web_commander,ai_brain
```

MuJoCo and Isaac Sim simulate v1 and v2; Waldo supplies the v2 world from
private-nodes-hub. For rendered wrist/chest streams and recording, the copy
selects them in the file:

```json5
{ robot: "openarm_v2_sim", instances: [
    { instance_id: "alpha",
      with: { robot_commander: "xr_commander", recorder: "lerobot_recorder", camera_rig: "cameras_sim" } },
] },
```

Rendered cameras require v2, so only `openarm_v2_sim` declares the rig,
[fragments/cameras_sim.json5](fragments/cameras_sim.json5); the camera
geometry lives in the simulation's OpenArm configuration and matches the v2
link layout. Real camera devices remain configured in
[fragments/cameras.json5](fragments/cameras.json5).

What a copy may change on the running simulation, and the simulation
limits, are in the [repository README](../Readme.md#what-a-copy-can-change).

Isaac Sim and Waldo declare a scene commander, selected at launch with
`--with web_scene_commander`. It drives the simulation's `scene_control`
contract. Waldo serves its viewer over HTTPS and WebSocket on `viewer_port`
(8080).
The OpenArm world also enables `hand_teleop`: webcam hand tracking in the
viewer takes command priority while a hand is tracked. Stopping tracking
returns control to the robot's pairing.

## Inspection and builds

```sh
peppy stack resolve openarm_sim_fleet --with mujoco
peppy stack resolve openarm_real_fleet --join openarm_v1 --join-name bravo --join-with xr_commander
peppy stack resolve openarm_generic_fleet --with isaac_sim,web_scene_commander --join openarm_v2_sim --join-name alpha --join-with xr_commander
peppy node add /path/to/ws/nodes-hub/openarm/initializer -sb
peppy node add /path/to/ws/nodes-hub/openarm/sim_arm -sb
peppy node add /path/to/ws/nodes-hub/openarm/sim_gripper -sb
```

Use `--node-build-idle-timeout-secs 18000` on launch or join for long first
builds. `stack list` reports health; join failures include the node's log
paths and machine names. See the [repository README](../Readme.md) for release
requirements and the full option table.

## Troubleshooting

**`deployment <id>: Dependencies missing from nodes cache (...): <name>:<tag>`**
The repo providing that node isn't registered with the daemon. Run `peppy repo add /path/to/<repo>` and `peppy repo refresh`, then launch again.

**The launch stalls on the simulation's build**
The first build pulls the sim base image and can outlive the daemon's idle timeout. Build it once beforehand with a longer timeout, then launch:

```sh
peppy node add /path/to/ws/nodes-hub/openarm/sim_isaac -sb --idle-timeout 18000
```

**Everything launches but the arms don't respond**
The simulation keeps loading after `Launch complete`, and Isaac can take a minute. Check instance health with `peppy stack list` and watch its log with `peppy node info openarm_sim_mujoco:v1`, `openarm_sim_isaac:v1` or `waldo:v1`.

**The Isaac stream is a black screen**
Stop the stack, clear the shader cache with `rm -rf ~/.cache/isaac-sim`, and launch again.

**The headset shows the page but "Enter VR" is missing**
WebXR needs a secure context, so the node self-generates a per-machine TLS certificate and always serves HTTPS; click through the browser's self-signed warning once. Over the network, open the https URL from the startup log. Over USB, `adb reverse tcp:4443 tcp:4443` and open `https://localhost:4443`.

**The headset is connected but neither arm moves**
Hold a grip button: it is the deadman, per hand, and with it released the node publishes nothing at all so the arms hold. If holding it does nothing, check the backbone's startup log line for which upstream mode it is following: a `"joints"` backbone reads only the panel's joint slots and a `"pose"` backbone only the headset's pose slots. A leader wired to the off-mode slots never reaches launch, so what remains is a leader that is publishing nothing: check the headset link and the grip in the node's status panel.
