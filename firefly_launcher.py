# -*- coding: utf-8 -*-
# PyInstaller entry. Run app/server.py via runpy so that server.py's __file__
# resolves to _MEIPASS/app/server.py under frozen mode, keeping BASE_DIR /
# STATIC_DIR / ASSETS_DIR and the memory/ knowledge/ relative paths valid.
import os
import sys
import runpy


def _run():
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    app_dir = os.path.join(base, "app")
    server_py = os.path.join(app_dir, "server.py")
    sys.stderr.write("[launcher] frozen=%s\n" % getattr(sys, "frozen", False))
    sys.stderr.write("[launcher] _MEIPASS/base=%s\n" % base)
    sys.stderr.write("[launcher] app_dir=%s exists=%s\n" % (app_dir, os.path.isdir(app_dir)))
    sys.stderr.write("[launcher] server.py=%s exists=%s\n" % (server_py, os.path.isfile(server_py)))
    sys.stderr.write("[launcher] static/index.html exists=%s\n"
                     % os.path.isfile(os.path.join(app_dir, "static", "index.html")))
    sys.stderr.flush()
    sys.path.insert(0, base)
    sys.path.insert(0, app_dir)
    os.chdir(app_dir)
    runpy.run_path(server_py, run_name="__main__")


if __name__ == "__main__":
    _run()
