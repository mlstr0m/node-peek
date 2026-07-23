# Node Peek — submission checklist (extensions.blender.org)

Step-by-step to get Node Peek into Blender's **Get Extensions** catalogue.

## 0. Prerequisites (done ✅)

- [x] Valid `blender_manifest.toml` — passes `blender --command extension validate`
- [x] GPL-3.0-or-later license + SPDX headers
- [x] `copyright` and `[build]` fields set
- [x] Public source repo: https://github.com/mlstr0m/node-peek

## 1. Build the upload zip

```sh
BL=/Applications/Blender.app/Contents/MacOS/Blender
"$BL" --command extension build --source-dir node_peek --output-dir .
# -> node_peek-0.5.2.zip  (manifest at the ZIP ROOT — required by the platform)
```

Note: this is **not** the same as an "Install from Disk" zip that has a
`node_peek/` subfolder. The platform wants the manifest at the root, which is
exactly what `extension build` produces.

## 2. Screenshots / preview media (do this — it sells the add-on)

Capture 2–4 images (PNG, landscape, ≥ 1280px wide). Suggested shots:

1. A shader graph with thumbnails above every node (the money shot).
2. A close-up of one node's preview updating after a value tweak.
3. Inside a node group, showing interior previews.
4. Optional: a short GIF/MP4 of a live edit refreshing.

Keep a copy under `screenshots/` in the repo (excluded from the built zip).

## 3. Submit

1. Log in to https://extensions.blender.org with your **Blender ID**.
2. Dashboard → **Add Extension** → upload `node_peek-0.5.2.zip`.
3. Paste the **Description** from `docs/EXTENSION_LISTING.md`.
4. Add the screenshots from step 2.
5. Paste `docs/REVIEWER_NOTES.md` into the note-to-reviewers field.
6. Submit for review.

## 4. Review loop

- A moderator reads the code (mandatory for all extensions). Expect questions
  about the background subprocess — the reviewer notes pre-empt them.
- Respond to comments, push fixes if asked, re-upload.
- On approval it's live and appears in Blender's *Get Extensions*.

## 5. Publishing updates later

1. Bump `version` in `blender_manifest.toml` (and `bl_info` for legacy installs).
2. Rebuild the zip, upload a new version on the extension's page.
3. Tag the release on GitHub to keep them in sync:
   `git tag vX.Y.Z && git push --tags` + `gh release create ...`.
