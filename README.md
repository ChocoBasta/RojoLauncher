# Rojo Launcher

Pick a repo, press **Pull** to get the latest from GitHub, then press **Start** to run `rojo serve`. In Studio, press **Connect** in the Rojo plugin.

![Rojo Launcher](screenshot.png)

## How it works

- **Pull** (the big button) gets the latest from GitHub. The first time, it downloads the repo instead. It shows how many new changes are waiting before you press it.
- **Start / Stop** runs `rojo serve` for that repo. If you press Start while a pull is running, Rojo starts as soon as the pull finishes. Switching to another repo stops the old server first, so the port is never left busy.
- Your unsaved changes are never overwritten. If a pull can't go ahead, the app tells you and leaves your files alone.
- **Favorites:** press the ★ next to a repo's name to keep it at the top.
- **Add any repo** with the **+** button: type `owner/repo` or paste the GitHub link. It can be anyone's repo, and it goes into your Favorites. Repos you added have a **Remove** link (this only takes them off the list; the folder on your computer stays).
- **Updates:** the app checks for a new version when it opens and every few hours. When there is one, an **Update** button appears at the bottom left. Press it and the app updates itself and reopens, and Rojo keeps running for the repo you had open. You can also check by hand in Settings.
- Keys: Enter = Pull, Ctrl+Enter = Start/Stop, ↑/↓ = switch repo, Ctrl+F = search, Ctrl+N = add a repo, F5 = refresh.
- Closing the window stops Rojo.

## Install

**Easiest (once it's built):** download `RojoLauncher-Windows.zip` or `RojoLauncher-Mac.zip` from the repo's Releases page, unzip, and open **Rojo Launcher**. No Python needed.
- Windows may say "Windows protected your PC" because the app isn't signed. Click **More info → Run anyway**.
- Mac: the first time, right-click the app and choose **Open**. On Mac the Update button opens the download page instead of updating in place.

**From the Python file:** install [Python 3](https://www.python.org/downloads/) (on Windows tick "Add Python to PATH"), double-click `rojo_launcher.py`, then press **Add to Desktop** in the app. After that it opens from your Desktop/Start menu (or Applications on Mac) like any other program, with its own icon.

You also need:
- **Git**: [git-scm.com](https://git-scm.com/downloads)
- **Rojo**: via Aftman/Rokit or the [Rojo releases](https://github.com/rojo-rbx/rojo/releases).

## Private repos

Open **Settings** and paste a token: [make one here](https://github.com/settings/tokens/new?scopes=repo&description=Rojo%20Launcher) (a classic token with "repo" ticked). If you use the GitHub CLI (`gh auth login`), or Git already has your GitHub login saved, it works without a token.

Settings are stored in `~/.rojo-launcher.json`.

## Publishing a new version (for whoever maintains this)

The app looks for updates in the `ChocoBasta/RojoLauncher` repo (`UPDATE_REPO` near the top of `rojo_launcher.py`).

1. Raise `APP_VERSION` in `rojo_launcher.py` and push it to `main`. Copies running from the Python file update from that file.
2. Push a tag with the same number, such as `v3.1`. The GitHub Action in `.github/workflows/build.yml` builds the Windows .exe and the Mac .app with PyInstaller and attaches them to a Release. The .exe updates from the latest Release.
