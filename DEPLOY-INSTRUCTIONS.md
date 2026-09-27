# Deploy instructions

This repository owns the WFB-NG, adapter, driver, service, and physical
diagnostic part of the ALFA hardware test. The confirmed first-test camera
traffic is HQ-only ordinary UDP through the WFB IP tunnel:

```text
Drone application destination: ground tunnel peer 10.5.0.1:5000
Ground application listener: UDP 5000
```

The deployment has one drone Pi and one ground-station Pi. A second shell used
to inspect the ground Pi is not a second ground station.

The camera/receiver process commands are documented in the sibling `system`
repository. This guide keeps the WFB deployment and evidence collection
complete when this repository is checked out on its own.

## Before the test

On the drone Pi:

```bash
cd "$HOME/consys"
sudo python3 drone.py --autostart
```

On the ground Pi:

```bash
cd "$HOME/consys"
sudo python3 gs.py --autostart
```

These commands install or update the role-specific WFB deployment and arrange
for it to survive reboot. They do not start the camera application; that
belongs to the `system` repository.

Confirm the authorized 2.4 GHz channel, region, bandwidth, antenna
configuration, and active WFB FEC/MCS values. Do not treat the driver power
index as a dBm/EIRP measurement.

Verify the drone tunnel route:

```bash
ip route get 10.5.0.1
```

This confirms that the drone can route application UDP traffic to the ground
tunnel peer before the camera starts sending HQ packets.

## During the test

Use the same `RUN_ID` as the camera/receiver process:

```bash
export RUN_ID="baseline-001"
export RUN_DIR="$HOME/wfbng-evidence/$RUN_ID"
mkdir -p "$RUN_DIR"

sudo python3 physical_test_diagnostics.py \
  --run-id "$RUN_ID" --role drone --output-dir "$RUN_DIR/wfb" \
  --sample-count 60 --sample-interval 1 \
  > "$RUN_DIR/wfb-collector.log" 2>&1 &
export WFB_COLLECTOR_PID=$!
```

This background process is read-only. It records 60 timestamped local WFB API
samples and service/adapter evidence in `$RUN_DIR/wfb`; it does not change
radio configuration or interrupt the running link.

Use `--role gs` on the ground station. This collector is read-only: it does
not alter WFB settings, services, radio state, or adapter configuration.

## After the test

```bash
wait "$WFB_COLLECTOR_PID"
find "$RUN_DIR/wfb" -maxdepth 1 -type f -print
```

`wait` prevents the shell from exiting before the collector writes its final
JSON, CSV, and JSONL files. The `find` command verifies that those evidence
files exist before the run directory is copied.

Keep the JSON, CSV, and JSONL evidence intact. API fields are recorded only
when the installed WFB service supplies them. Missing values remain explicitly
unavailable; they are never fabricated.

For the combined camera/WFB procedure, use the workspace-level
[`DEPLOY-INSTRUCTIONS.md`](../DEPLOY-INSTRUCTIONS.md) when both repositories
are present.
