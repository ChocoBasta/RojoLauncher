"""Small checks the Mac build runs against the app it just built (see .github/workflows/build.yml).
Talks to the running app the same way its window does, through ~/.rojo-launcher/server.json.

  check.py wait-server            wait until a copy of the app is up
  check.py wait-new-server        wait until a different copy is up (after Move / Update)
  check.py state key=value ...    wait until the app's state has these values
  check.py wait-update            wait until the app has found an update
  check.py post <action>          press a button (install, apply_update, ...)
  check.py connected              the window's page is connected to the app
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SERVER = Path.home() / ".rojo-launcher" / "server.json"
LAST = Path("/tmp/rojo-launcher-last-server.json")


def info():
    try:
        return json.loads(SERVER.read_text())
    except (OSError, ValueError):
        return None


def call(path, data=None, timeout=5):
    i = info()
    if not i:
        raise OSError("no server.json")
    url = "http://127.0.0.1:%d%s" % (i["port"], path)
    if data is None:
        req = urllib.request.Request(url + ("&" if "?" in path else "?") + "t=" + i["token"])
    else:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"X-Token": i["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def wait(what, test, secs=60):
    end, last = time.time() + secs, None
    while time.time() < end:
        try:
            last = test()
            if last:
                return last
        except Exception as e:
            last = e
        time.sleep(0.5)
    sys.exit("FAILED waiting for %s (last: %r)" % (what, last))


def main(cmd, *args):
    if cmd == "wait-server":
        wait("the app", lambda: call("/api/ping").get("ok"), 90)
        print("app is up on port", info()["port"])
    elif cmd == "wait-new-server":
        old = json.loads(LAST.read_text())["token"]
        wait("a new copy of the app", lambda: info() and info()["token"] != old and call("/api/ping").get("ok"), 90)
        print("new copy is up on port", info()["port"])
    elif cmd == "state":
        want = dict(a.split("=", 1) for a in args)

        def ok():
            s = call("/api/state")
            bad = {k: s.get(k) for k, v in want.items() if str(s.get(k)) != v}
            if bad:
                raise ValueError("got %r" % bad)
            return True
        wait("state %s" % want, ok, 45)
        s = call("/api/state")
        print("state ok:", {k: s.get(k) for k in ("version", "mac_app", "installed", "banner", "rojo", "git", "platform")})
    elif cmd == "wait-update":
        wait("an update", lambda: call("/api/state").get("update"), 45)
        print("update found:", call("/api/state")["update"])
    elif cmd == "post":
        LAST.write_text(json.dumps(info()))
        r = call("/api/" + args[0], {})
        print(args[0], "->", r)
        if not r.get("ok"):
            sys.exit("FAILED: %s" % r)
    elif cmd == "connected":
        port = info()["port"]

        def ok():
            out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:ESTABLISHED"],
                                 capture_output=True, text=True).stdout
            lines = [l for l in out.splitlines() if "->127.0.0.1:%d" % port in l]
            return "\n".join(lines) if lines else None
        print("window page connected:\n" + wait("the window to connect", ok, 30))
    else:
        sys.exit("unknown command " + cmd)


if __name__ == "__main__":
    main(*sys.argv[1:])
