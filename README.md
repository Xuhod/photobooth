# Photo booth Drive backend

The desktop app uses a Windows camera for live preview and still capture. It saves the finished JPEG locally, uploads it to Drive, grants link-only viewing, and displays a QR code. Guests do not need a separate account.

New photos go into **My Drive → Photo Booth Uploads**. The booth creates and reuses its own folder; it shares each photo by link, not the folder. Existing uploads are not moved.

## Run the camera app

After authorizing Drive (see below), double-click **Start Photo Booth.cmd**, or run from this folder:

```powershell
.\.venv\Scripts\python.exe camera_app.py
```

Select **Canva Page 2** or **Canva Page 3** from **Template** (or choose **No template** for a single photo). Choose a countdown and press **Take Photo** once for each shot. The button counts from **1 of 3** to **3 of 3**, and the preview stays live between shots. After shot three, the booth saves the strip as `jobs/<job-id>/photo.jpg` and the individual shots as `shot-1.jpg` through `shot-3.jpg` alongside it. Only the strip is uploaded and shared through the QR.

Scan the QR with a signed-out phone. If upload fails, **Retry Upload** reuses the saved strip. After restarting, recover queued uploads with `booth.py resume`. If the camera is unavailable, close other camera apps and check **Windows Settings → Privacy & security → Camera → Let desktop apps access your camera**.

Use **History** in the booth to reopen the QR or copy the Drive link for one of the 20 most recent successful uploads, even after restarting. History is stored on the booth PC and is visible to anyone using the app.

Use the **Camera** dropdown to select **Windows default** or a specific camera. The list updates on device-change notifications. If the selected camera is disconnected, the booth waits for it to reconnect or lets you choose a different device. Switching cameras cancels any countdown but does not interrupt an upload already in progress.

## Prepared Canva templates

The default **Fast sharing** mode builds a 2480 × 3508 strip. Choose **Full resolution** for the original 4961 × 7016 size if you need it for printing. The three original captures are saved locally at camera resolution in either mode.

`Template/Ready/page-2` and `Template/Ready/page-3` contain the artwork, three slot masks, and their coordinates. `photo_template.render_strip()` accepts three Pillow images in top-to-bottom order and returns a full-size composite.

Test a template with any three existing photos (use `3` instead of `2` to try the other page):

```powershell
.\.venv\Scripts\python.exe photo_template.py render 2 strip.jpg first.jpg second.jpg third.jpg
```

Pages 4 and 5 are not prepared: their changed slot regions touch or overlap when all frames are changed at once. To use those pages with three distinct shots, export one variant per slot with just that slot white and the other two black.

## First live test (Windows PowerShell)

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a project and enable the **Google Drive API**. Configure Google Auth Platform, then create an OAuth client with application type **Desktop app**. If the app is External and in Testing, add your operator Google account under **Audience → Test users**.
2. Download the desktop OAuth client JSON and save it next to `booth.py` as `credentials.json`. Keep it and the generated `token.json` out of source control and do not share them.
3. Run from this folder:

   ```powershell
   .\.venv\Scripts\python.exe booth.py auth
   .\.venv\Scripts\python.exe booth.py send "C:\path\to\sample.jpg"
   ```

   If setting up on a different PC, first run `python -m venv .venv` and `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.

4. Open the printed `qr.png` and scan it with a signed-out phone. The Drive file is viewable by anyone who receives the link. Use a non-sensitive sample photo. If sharing fails, check whether the operator's Google Workspace policy allows anyone-with-link sharing.

The local copy, upload progress, and QR are stored under `jobs/<job-id>/`. If a job fails, run `.\.venv\Scripts\python.exe booth.py resume` to retry pending jobs, or add a printed job ID to retry just that one. Don't run multiple publishing processes against the same queue. The local job ID is for operator retries; guests use the QR or the Drive link itself.

For the future UI, call `Queue.enqueue()` with the edited JPEG and `Queue.publish(job_id, drive)` from a background worker using `Drive.connect()`. Publish returns the URL and QR path. Drive links still work after the PC shuts down, provided sharing stays enabled and the files aren't removed. There is no automatic deletion; keep uploads for at least a week after the event ends. For External apps in Testing, the OAuth refresh token can expire after seven days; sign in again before the event if needed. Token expiry does not affect already-shared links.
