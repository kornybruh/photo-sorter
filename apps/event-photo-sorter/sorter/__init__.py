"""
Event Photo Sorter -- package entry point.

Install once, anywhere -- the app itself doesn't need to live inside your
photo folder. On first launch it asks you to pick which folder to sort; it
remembers that choice for next time, and creates <folder>/sorted/
automatically.

Multiplayer: the server listens on your whole WiFi network (not just this
PC), so other people on the same network can open the LAN URL printed at
startup on their own phone/laptop and sort in parallel. The host sets "how
many people are sorting" once; every other device that opens the link gets
auto-assigned the next free player number, no picking required. No
login/auth -- anyone on your WiFi with the URL can sort or delete, same as
you.
"""
import threading
import webbrowser

from . import config
from .app import app  # noqa: F401  (re-exported for web_sorter.py / WSGI use)
from .network import get_lan_ip

config.restore_last_folder()

from . import routes  # noqa: E402,F401  (registers all routes on `app`)


def main():
    lan_ip = get_lan_ip()
    local_url = f"http://127.0.0.1:{config.PORT}"
    lan_url = f"http://{lan_ip}:{config.PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(lan_url)).start()
    print("Event Photo Sorter (web) running.")
    print(f"  On this PC:         {local_url}")
    print(f"  On your WiFi (LAN): {lan_url}   <- share this for multiplayer sorting")
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
