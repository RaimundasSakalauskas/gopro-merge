# gopro-merge

Losslessly merge the chapter files a GoPro splits long recordings into, without re-encoding:

- **HERO** `.MP4` chapters (`GX010042.MP4`, `GX020042.MP4`, …)
- **MAX / MAX2** `.360` chapters (`GS010113.360`, `GS020113.360`, …). The merged file still opens in GoPro Player as a 360° video, with stabilization and horizon leveling intact.

It works from the command line or from a right-click Quick Action in Finder.

## Why not ffmpeg?

A plain `ffmpeg -f concat -c copy` loses data GoPro's own tools rely on:

- the concat demuxer fails on the GPMF telemetry track (`gpmd`: GPS, gyro, accelerometer) because of how its edit lists are timed;
- the muxer drops the `moov/udta` boxes (`GPMF`, `FIRM`, `SETT`, …) that describe the camera, lens and settings;
- for `.360` files it rewrites the second video track's display matrix, which GoPro uses to hide that track from regular players.

This tool works on the MP4 box tree directly. It keeps the first chapter's metadata byte-for-byte and rebuilds only each track's sample tables, durations and edit lists. Every chapter keeps its own edit-list entry, so the short pre-roll GoPro hides at the start of each chapter stays hidden, and audio/video sync is the same as in the original files.

## Requirements

- macOS for the Quick Action. The script itself is plain Python and runs anywhere.
- Python 3.9+ (standard library only). On a fresh Mac, `/usr/bin/python3` becomes available after installing the Command Line Tools (`xcode-select --install`).
- [GoPro Player](https://gopro.com/en/us/info/gopro-player), only for the Quick Action to appear on `.360` files: GoPro Player registers that file type. `.MP4` files don't need it.
- Free disk space roughly equal to the total size of the chapters being merged.

## Install

```sh
git clone <this repo>
cd gopro-merge
./install.sh
```

This copies the script to `~/Library/Application Support/gopro-merge/` and creates the Quick Action `~/Library/Services/Merge GoPro Chapters.workflow`.

Then finish the setup. macOS doesn't show a newly installed Quick Action until both steps are done:

1. Restart Finder so it picks up the new Quick Action:
   ```sh
   killall Finder
   ```
2. In Finder, right-click any file → **Quick Actions → Customize…**, and tick **Merge GoPro Chapters**. Also check it's ticked in **System Settings → Keyboard → Keyboard Shortcuts → Services → Files and Folders**.

To update after pulling changes, run `./install.sh` again.

## Uninstall

```sh
./uninstall.sh
```

This removes the installed script copy and the Quick Action. The cloned folder and your videos are left untouched; delete the folder yourself if you no longer need it.

## Usage

### Finder

Select GoPro `.MP4` or `.360` files, right-click, then choose **Quick Actions → Merge GoPro Chapters**.

- **One file selected:** every chapter of that recording in the same folder is found and merged. Any chapter of the recording works as the starting point.
- **Several files selected:** exactly those are merged, sorted by chapter number regardless of selection order. Use this to merge only some chapters.

A notification appears when merging starts and when it finishes. Errors are shown in an alert.

### Command line

```sh
./gopro-merge.py GX010042.MP4             # find all chapters, choose which to merge
./gopro-merge.py a.mp4 b.mp4 c.mp4        # merge exactly these, sorted by chapter number
./gopro-merge.py -y GS010113.360          # merge every chapter found without asking
./gopro-merge.py GX010042.MP4 -o ride.MP4
```

When given a single file in a terminal, it lists the chapters it found and asks which to include (`Enter` for all, or e.g. `1-3`, `1,2,4`).

### Output

By default the result is written next to the first chapter as `<first chapter>_merged.<ext>`, for example `GX010042_merged.MP4` or `GS010113_merged.360`. Existing files are never overwritten, and the source files are never modified.

## Supported file names

Only `.MP4` and `.360` files are considered (case doesn't matter). Low-resolution proxies (`.LRV`) and thumbnails (`.THM`) are ignored. Chapters must be in the same folder and have the same extension.

### GoPro camera names

GoPro names files `G` + type letter + 2-digit chapter + 4-digit recording number:

| Pattern | Cameras | Example recording | Chapter order |
| --- | --- | --- | --- |
| `GX` `cc` `nnnn` `.MP4` | HERO6 and later, HEVC | `GX010042.MP4`, `GX020042.MP4`, `GX030042.MP4` | by `cc` (01, 02, 03, …) |
| `GH` `cc` `nnnn` `.MP4` | HERO6 and later, H.264 | `GH010042.MP4`, `GH020042.MP4` | by `cc` |
| `GS` `cc` `nnnn` `.360` | MAX, MAX2 | `GS010113.360`, `GS020113.360` | by `cc` |
| `GOPR` `nnnn` `.MP4`, then `GP` `cc` `nnnn` `.MP4` | HERO5 and earlier | `GOPR0042.MP4`, `GP010042.MP4`, `GP020042.MP4` | `GOPR` first, then by `cc` |

Any other `G?ccnnnn` prefix is treated the same way. Chapters of one recording share the prefix letters, the recording number `nnnn` and the extension. The name must be exactly the camera's (`GX010042.MP4`); renamed files such as `GX010042_edit.MP4` are not picked up automatically, but can still be merged by selecting them explicitly.

### Part suffix

For files you renamed yourself:

| Pattern | Example recording | Chapter order |
| --- | --- | --- |
| `<name>.<ext>`, `<name>.part<N>.<ext>` | `ride.360`, `ride.part2.360`, `ride.part3.360` | the file without a suffix is part 1, then by `N` numerically (`part10` after `part9`) |

`<name>` can be anything, including a GoPro name (`GX010042.MP4`, `GX010042.part2.MP4`). If `.part<N>` siblings exist, they take precedence over the GoPro naming.

## Limitations

- All chapters must come from the same recording: same tracks, codecs and settings. Mismatched files are rejected instead of merged into a broken file.
- Tested with real GoPro MAX2 `.360` footage (firmware H24.02) and with synthetic `.MP4` chapters that mimic HERO files (H.264 with B-frames and AAC audio), where the merged video is frame-for-frame identical to the chapters. Real HERO footage and the original MAX haven't been verified yet.
- Very long recordings are fine (64-bit offsets are used), but the merged file needs as much free space as the chapters it's built from.

## License

[MIT](LICENSE)
