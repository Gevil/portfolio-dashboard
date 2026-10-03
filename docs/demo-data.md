# Demo data and media

[← Documentation index](README.md)

Everything under `docs/media/` was captured from a **separate, throw-away demo container** – never from a real
deployment – so the media contain no real positions, amounts, credentials, e-mail addresses or network
details.

## What is fictional and what is real

| Fictional (seeded by `tools/demo/seed.py`) | Real (fetched live by the demo container) |
|---|---|
| Share counts and cost bases (ASML 2.5 sh / €2,500, NVDA 10 sh / €1,200, ETF 3 sh / cost unknown) | Prices, charts, FX, benchmark, yield curve, CFTC data, earnings dates, market light |
| Alert history, notifications, approvals, triage items and their headlines ("Demo Wire") | |
| Advice history and the three sample analysis reports | The scoreboard's *grading* of that fictional advice against real prices |
| Credentials (`demo` / `demo-pass`), tokens, contact strings | |

The demo instance has no API keys, no ntfy and no reachable LLM: pushes stay in the outbox, FRED is off and the
primary GPU lane shows as down.

## Regenerate

Prerequisites: the `portfolio-dashboard:latest` image (`bash ops/deploy.sh` or `podman build`), `podman`,
`ffmpeg`, ImageMagick, and a Python with `playwright` + Chromium installed.

```bash
export PLAYWRIGHT_PY=/path/to/venv/bin/python        # python with playwright installed
bash tools/demo/run.sh up         # seed /tmp/pd-demo, start the container on 127.0.0.1:8699
bash tools/demo/run.sh capture    # PNG screenshots + a webm walkthrough in /tmp/pd-media/raw
bash tools/demo/run.sh down       # remove the container and /tmp/pd-demo
```

Then convert and shrink (keeps the repository small – about 3 MB for all media):

```bash
# screenshots -> WebP, metadata stripped
magick raw/overview-dark.png -strip -quality 80 -define webp:method=6 docs/media/screenshots/overview-dark.webp
# walkthrough -> mp4 (inline-able on GitHub via link) and a small GIF for the README
ffmpeg -ss 1 -i raw/video/*.webm -vf "fps=15,scale=1100:-2" -c:v libx264 -crf 30 -preset slow -pix_fmt yuv420p -movflags +faststart -an docs/media/video/walkthrough.mp4
ffmpeg -ss 1 -t 26 -i raw/video/*.webm -vf "fps=5,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" docs/media/video/walkthrough.gif
```

Set `LANES_DIR` to a directory with a `lanes.conf` to populate the *GPU lane* card (the published screenshots
were taken that way; without it the card reports no lanes configured).

## Privacy checklist before committing new media

1. Capture only from the demo instance (port 8699), never from the real pod.
2. Open every image at native resolution (crop, don't judge thumbnails) and look for amounts, e-mail
   addresses, IPs, home-directory paths, broker names and tokens.
3. `magick … -strip` removes metadata; keep the WebP/GIF/MP4 only, not the raw PNG/WebM.
4. Byte-scan the new files and run `gitleaks` before pushing (see
   [`skills/secrets-handling`](../skills/secrets-handling/SKILL.md)).
