"""
Figuring out this machine's own network addresses -- used both to print the
shareable LAN URL and to tell "is this request from the host machine itself"
apart from "someone else on the WiFi", for host-only actions like Clear All
and the folder picker.
"""
import socket


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_local_addresses():
    # every address this machine could be reached at, including its own
    # LAN-facing IP -- the host's browser gets auto-opened to that address,
    # not 127.0.0.1, so "am I the host" has to recognize both
    addrs = {"127.0.0.1", "::1", get_lan_ip()}
    try:
        addrs.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    return addrs


LOCAL_ADDRESSES = get_local_addresses()
