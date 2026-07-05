# Phase-0 Spike

This spike proves the closed loop on macOS: build a SwiftUI app, boot an iOS Simulator, install the app, launch it, capture a screenshot, and record a short video.

## Loop

1. Generate the SampleApp project with XcodeGen, or clone a repo if `--repo` is provided.
2. Resolve the project scheme automatically unless `--scheme` is passed.
3. Find or create the requested simulator device/runtime.
4. Build with `xcodebuild` for the simulator destination.
5. Install and launch the app in the booted simulator.
6. Capture a screenshot and record video.

## Flags

| Flag | Default | Description |
|---|---:|---|
| `--project` | SampleApp `.xcodeproj` | Path to the Xcode project or workspace |
| `--scheme` | auto-resolved | Xcode scheme to build |
| `--device` | `iPhone 15` | Simulator device name |
| `--os` | `latest` | iOS runtime version or `latest` |
| `--repo` | unset | Git URL to clone instead of using the SampleApp |
| `--record-seconds` | `8` | Video duration before interrupting the recorder |
| `--out` | `./artifacts` | Output directory for logs and artifacts |

## Artifacts

- `screenshot.png`
- `demo.mp4`
- `DerivedData/`
