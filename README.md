# Opp Detector
*A background app that keeps you safe from the opps*

## The Problem
Whenever my boss sees me scrolling reels, he gets really mad.

## Solution
~~Stop scrolling reels during work~~ Have my computer have my back; when my boss is approaching, mute and switch off the reels for me

Hands-free boss evasion :)

Opp Detector watches your webcam, recognises the people you've registered, and the moment someone you've ticked as an *opp* walks into frame, it mutes your Mac and flips to another desktop (or hides the front app).
Once they've been gone for a few seconds, it unmutes and switches back.

In *privacy mode*, a stranger standing close behind you (w/ at least two people in frame) counts as a threat too.

macOS only.

## How it works

- The **Detector** sits in its own process. That's the whole recognition pipeline: video capture, preprocessing, and the facial recognition model. That lets it keep running efficiently in the background, while letting the frontend stay quick.
- We analyze twice per second to keep processing minimal. It needs to be running all the time, so preserving battery is a must
  - Analysis only happens on a 480p scaled version of the image to keep processing even more minimal
- Recognition matches via InsightFace's `PersonAnalysis` face match first, with body re-ID as a fallback. Someone counts as "in frame" at ≥ 0.5 similarity and ≥ 1% of the frame.
- Only rising edges trigger: We keep the opp count for the last 5 analysed frames and fires when the newest count jumps above the average from a couple of frames earlier. Combined with a 5s debounce period, that means an opp just standing there will never your computer out.
- **Evasion** depends on your permissions. Every evasion mutes first (via `osascript`), then uses the best option it has:
  1. Accessibility granted -> presses ctrl-right arrow (or left if you're on the last desktop) and checks that the desktop actually changed. If it didn't, hide the front app.
  2. No Accessibility, or the shortcut is disabled -> hide the frontmost app (like CMD+H).
  3. No PyObjC -> put the display to sleep.
- After your opp has gone for a few seconds, it automatically restores you (unmutes, switches back)
- Most importantly, **everything is on-device**. Not a single frame leaves your computer; the only network access is the one-time model download. People are stored in `./data/` (a `people.json` + one JPEG each; `OPP_DETECTOR_DATA` overrides the location).

## Setup & run

Needs macOS, a webcam and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run app
```

Everything else happens automatically...
- The model downloads on first start (once you hit *Start detector*)
- MacOS asks for Camera & Accessibility permissions once you need them
  - You caaaan pre-move Accessibility: Go Settings > Privacy > Accessibility then turn on terminal, restart the terminal, and continue

## Usage

1. **Add someone:** *Add person…* takes a photo (needs exactly one face). Or start the detector and hit *Capture from camera*: whoever the camera sees right now gets added, **already marked as an opp** ("you're my boss now").
2. Tick *Opp* next to anyone who should trigger an evasion. Changes apply live, no restart.
3. **Start detector.** The preview shows boxes: red = opp, green = known, grey = unknown. The banner says CLEAR or OPP IN FRAME.
   Optional: tick *Privacy mode* so close strangers behind you count too.
4. Go back to your reels.

Tip: when capturing or picking a photo, have them **look at the camera**. Recognition is face-first.

## Credits


- The whole detector loop, multi-process plumbing, rising edge trigger, evasion strategies/fallbacks/etc. was me (some help from  Claude for debugging, some frontend, and some of the evasion strategy impls).
- Facial recognition models: [InsightFace](https://github.com/deepinsight/insightface) 2.1 (MIT)
- Inference for said models: [ONNX Runtime](https://onnxruntime.ai)
- Webcam access, pre-/post-processing: [OpenCV](https://github.com/opencv/opencv-python)
- Image logistics (for frontend = load, save, preview, etc.): [Pillow](https://python-pillow.org), [PyObjC](https://github.com/ronaldoussoren/pyobjc) (for `Quartz`, `AppKit`, `Foundation`, `objc`)
- Packaging + running: [uv](https://github.com/astral-sh/uv)