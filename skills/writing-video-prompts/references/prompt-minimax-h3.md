# Blaze API notes — MiniMax H3 (`minimax-h3`)

How a clip on this model is sent to the Blaze Video API. This file covers the API only; write the
prompt itself with the general guidance in `SKILL.md`.

## Media

- **At least one reference is required:** a picture, a video or an audio clip. A text-only request
  is refused.
- Up to 9 pictures (start frame, end frame and reference images together), 3 reference videos and
  3 reference audios.
- `start_frame_image`, `end_frame_image` and `reference_*` can all be passed in the same call.
- Local files and https URLs both work. Local files are sent inline.

## Referring to media in the prompt

References can be named in the prompt by tag: `<Picture 1>`, `<Picture 2>` … for pictures in the
order they are sent (start frame first, then reference images, then end frame), `<Video N>` for
videos and `<Audio N>` for audio.

## Parameters

- `duration`: 4 to 15 seconds.
- `aspect_ratio`: `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`.
- Audio is always generated with the picture; `generate_audio` has no effect.
- Prompt enhancement (Blaze rewriting the prompt before generation) is on by default; set
  `BLAZE_ENHANCE_PROMPT=false` in `~/.supercmo/.env` to send the prompt as written.
