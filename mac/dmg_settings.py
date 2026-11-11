# dmgbuild settings for RojoLauncher-Mac.dmg: the app on the left, Applications on the right,
# so installing is one drag. Used by .github/workflows/build.yml.
import os.path

app = defines.get("app", "dist/Rojo Launcher.app")  # noqa: F821  (dmgbuild provides `defines`)
appname = os.path.basename(app)

format = "UDZO"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = defines.get("icon") or None  # noqa: F821  the disk's own icon
icon_locations = {appname: (150, 165), "Applications": (410, 165)}
window_rect = ((240, 200), (560, 340))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_size = 112
text_size = 13
