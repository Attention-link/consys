# ALFA test branch: risks, fixes, and retained constraints

## Current 2.4 GHz profile

The current hardware/simulation baseline is 2.4 GHz. The source defaults are
channel 13 and region `PL`, but those are not authorisation for any test site.
The available AM-V2G-Ti ground antenna is also 2.4 GHz only. Retain the
existing deployment process and compare its AWUS-dipole baseline with the
sector antenna before creating a separate, fully equipped 5 GHz profile.

- **Embedded WFB keys are not secrets.** The matching base64 default keys in
  `gs.py` and `drone.py` permit a newly installed pair to communicate, but
  anyone with the scripts can obtain the material. Generate and distribute a
  unique paired key before operating a private link; protect the resulting key
  files.
- **The configured transmit-power value is a driver index, not dBm.** The
  88XXau WFB driver uses a 0--63 scale and this project caps it at 56. Do not
  interpret the index as radiated power or use it to demonstrate regulatory
  compliance. Record measured output for each adapter and antenna combination.
- **The drone's two-adapter layout is intentionally asymmetric.** `drone_TX`
  is the only transmitting adapter and `drone_RX` is receive-only. This
  preserves the tested directional adapter policy. Do not change that role
  policy without reviewing the RF wiring.
- **Statistics parsing assumes the local WFB-NG API contract.** The diagnostics
  expect a length-prefixed msgpack TCP response and tolerate common byte/string
  key differences, but API schema or endpoint changes can still make metrics
  unavailable or misleading. Validate counters after every WFB-NG upgrade and
  keep an integration fixture from the deployed version.
- **Physical-test evidence is opt-in and read-only.**
  `physical_test_diagnostics.py` writes JSON, CSV, and JSONL evidence only
  when an operator supplies a run ID, role, and output directory. It records
  no configuration or key contents, does not alter deployment behavior, and
  marks unavailable data instead of fabricating radio metrics.
- **The ALFA HQ application path uses the WFB IP tunnel.** The sibling
  `system` branch sends HQ as ordinary UDP to `10.5.0.1:5000`; the ground
  application receives it on UDP `5000`. The separate `5602/5600`
  direct-video service is not the camera path. Use
  [`DEPLOY-INSTRUCTIONS.md`](DEPLOY-INSTRUCTIONS.md) for the exact per-run
  evidence commands; LQ/COMBINED are not selected for the first test.
