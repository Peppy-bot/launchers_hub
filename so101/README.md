# so101 launcher

Single-arm SO-101 teleop over the `nodes-hub` so101 family:

```text
commander_inst ──(joint or pose)+gripper──▶ backbone_inst ──joint+gripper──▶ follower_inst
```

The [launcher](so101.json5) runs one SO-101; the [robot fragment](fragments/so101.json5)
owns the follower, backbone, and the robot's own axes with their bindings.
XR and recorder deployments are shared with OpenArm through the
repository-level robot commander and recording directories. "Leader"/"follower"
name pairing roles here; the SO-101 leader arm is one commander option
among several (see nodes-hub/so101/README.md, Terminology). The fragment's
axes:

| Axis | Options | Provides |
|---|---|---|
| `robot_commander` (deploys `so101_leader`) | `so101_leader`, `xr_commander`, `no_commander` (actions only) | `commander_inst` with a commander |
| `recorder` (`zero_or_one`) | `lerobot_recorder` | `recorder_inst` |
| `camera_rig` (`zero_or_one`) | `cameras` | `front` |

```sh
peppy stack launch so101                                        # leader-arm teleop
peppy stack launch so101 --with xr_commander,lerobot_recorder   # headset + recording
peppy stack launch so101 --with xr_commander,lerobot_recorder,cameras
peppy stack launch so101 --with no_commander                    # actions only
peppy stack resolve so101 --with xr_commander                   # inspect the flattened stack
```

Recording requires the XR commander: episodes start via the recorder's
`record_episode` action, and only the headset carries that button.

## Host prerequisites

1. **udev rules**: install `rules/60-so101.rules` (fill in the board serials
   or hub-port paths first) so `/dev/so101_follower` and `/dev/so101_leader`
   stay stable across replugs; two identical adapters are otherwise
   indistinguishable. The `cameras` option additionally needs the
   `/dev/so101_front_cam` rule filled in.
2. **Calibration**: the nodes refuse to start uncalibrated. The follower is
   in every selection, so it always needs this:

   ```sh
   lerobot-calibrate --robot.type=so101_follower \
     --robot.port=/dev/so101_follower --robot.id=follower
   ```

   The leader is deployed only by the `so101_leader` option, so calibrate it
   when that is the commander:

   ```sh
   lerobot-calibrate --teleop.type=so101_leader \
     --teleop.port=/dev/so101_leader --teleop.id=leader
   ```

   Each writes `<id>.json` under lerobot's own calibration directory. Place
   or symlink both under `/var/lib/so101/calibration/`, which is the host
   path this fragment bind-mounts and where the nodes look for `<id>.json`.
3. **Postures**: `move_to_home` targets the collapsed park pose the arm
   rests in, and `move_to_ready` the calibration midpoint where work starts;
   both are `so101_description` constants, validated at startup against the
   kinematics URDF that same description embeds (no host URDF to fetch).

## Safety model

No collision governor exists on this single arm; the backbone is the one
motion authority, and what it limits depends on how the arm is being driven.

**Action goals** (`move_to_home`, `move_to_ready`, `move_arm_joints`,
`move_arm`) run minimum-jerk trajectories sized by the per-joint velocity
caps, and Cartesian moves additionally by the end-effector speed caps. A
goal outside the URDF joint limits is refused outright.

**A joints-led stream** (the SO-101 leader arm) passes through under the
end-effector speed caps alone, matching the reference teleop: streamed
targets are deliberately not joint-limit clamped, and the servo EPROM
position limits are the physical travel guard.

**A pose-led stream** (the headset) is clipped into the arm's fitted
reachable ball before the solver sees it, then limit-clamped and
rate-stepped per joint, then governed by the same end-effector speed caps.

After any stream gap the backbone re-anchors on the measured position, so a
leader parked far away walks the follower over rather than snapping it. The
follower applies no per-cycle clamp of its own: on silence it simply stops
writing goals and the servo PID holds the last one. Setpoint consumers
age-gate on the producer's wire timestamp, so a backlog replayed after a
stall is dropped rather than executed. The leader has no engage button;
staleness is the deadman, so unplugging it stops the leader's stream within
`stale_timeout_s` (0.25 s by default) and the arm settles at its last
governed target shortly after. Effort feedforward is rejected at both the
backbone and the follower: the STS3215 is position-only hardware.
