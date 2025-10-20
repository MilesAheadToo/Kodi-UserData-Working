# -*- coding: utf-8 -*-
# Drop-in replacement for service.channel_vpn_cc/service.py with detailed logging

import glob
import io
import json
import os
import time
from urllib.parse import quote_plus

import xbmc
import xbmcaddon

try:
    import xbmcvfs  # type: ignore
except ImportError:
    xbmcvfs = None

# ---------- SETTINGS ----------
LOG_ENABLED = True  # set to False to turn off file logging
CONNECT_TIMEOUT_SEC = 20
COOLDOWN_SEC = 20  # avoid rapid re-switching

ADDON_ID = "service.channel_vpn_cc"
ADDON = xbmcaddon.Addon(id=ADDON_ID)


def resolve_path(path):
    if not path:
        return ""
    if path.startswith("special://"):
        try:
            if xbmcvfs:
                return xbmcvfs.translatePath(path)
        except Exception:
            pass
        try:
            return xbmc.translatePath(path)
        except Exception:
            return path
    return path


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


# Source data
MAPFILE = "/storage/downloads/channel_cc_map.json"  # legacy fallback path
cfg_candidate = resolve_path(ADDON.getAddonInfo("profile")).rstrip("\\/")
if not cfg_candidate:
    cfg_candidate = os.path.join(resolve_path("special://profile/addon_data"), ADDON_ID)
CFG_DIR = cfg_candidate
OVERRIDE = os.path.join(CFG_DIR, "cc_to_profile.json")   # {"GB":"my_expressvpn_uk_-_docklands_udp (UDP)", ...}
STATE = os.path.join(CFG_DIR, "state.json")           # {"last_profile":"...", "ts": 1699999999}

# Default fallback mapping in case override file is missing
DEFAULT_CC2PROFILE = {
    "GB": "my_expressvpn_uk_-_docklands_udp (UDP)",
    "DE": "my_expressvpn_germany_-_frankfurt_-_1_udp (UDP)",
    "US": "my_expressvpn_usa_-_new_york_udp (UDP)",
    "CA": "my_expressvpn_canada_-_toronto_udp (UDP)"
}

# Logging
LOGFILE = os.path.join(CFG_DIR, "service_cc.log")
VPNMGR_LOG = resolve_path("special://profile/addon_data/service.vpn.manager/service.log")

# ---------- HELPERS ----------


def klog(msg):
    xbmc.log("[{}] {}".format(ADDON_ID, msg), xbmc.LOGINFO)
    if LOG_ENABLED:
        try:
            ensure_dir(CFG_DIR)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with io.open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(u"{} {}\n".format(ts, msg))
        except Exception as e:
            xbmc.log("[{}] log-write-failed: {}".format(ADDON_ID, e), xbmc.LOGWARNING)


def load_json(path, default=None):
    try:
        with io.open(resolve_path(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, obj):
    try:
        full = resolve_path(path)
        base = os.path.dirname(full)
        ensure_dir(base)
        with io.open(full, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        klog("WARN cannot write {}: {}".format(path, e))
        return False


def load_cc_map():
    candidates = []
    try:
        getter = getattr(ADDON, "getSettingString", None) or getattr(ADDON, "getSetting", None)
        if getter:
            configured = (getter("channel_map_path") or "").strip()
            if configured:
                candidates.append(configured)
    except Exception:
        pass
    env_override = os.environ.get("CHANNEL_CC_MAP", "").strip()
    if env_override:
        candidates.append(env_override)
    candidates.append(os.path.join(CFG_DIR, "channel_cc_map.json"))
    candidates.append(MAPFILE)

    for raw_path in candidates:
        if not raw_path:
            continue
        full = resolve_path(raw_path)
        try:
            with io.open(full, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data, full
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            klog("WARN cannot parse {}: {}".format(raw_path, e))
        except Exception as e:
            klog("WARN cannot read {}: {}".format(raw_path, e))
    return {}, ""


def playing_is_pvr():
    # True for Live TV / PVR playback only
    fp = xbmc.getInfoLabel('Player.FilenameAndPath') or ""
    if fp.startswith("pvr://"):
        return True
    if xbmc.getCondVisibility("PVR.IsPlayingTV") or xbmc.getCondVisibility("PVR.IsPlayingRadio"):
        return True
    return False


def get_channel_name():
    name = (xbmc.getInfoLabel('PVR.ChannelName') or
            xbmc.getInfoLabel('VideoPlayer.ChannelName') or
            xbmc.getInfoLabel('Player.Title') or "")
    return name.strip()


def run_vpn_switch(profile_name):
    # Uses VPN Manager's plugin API
    encoded = quote_plus(profile_name)
    cmd = 'RunPlugin(plugin://service.vpn.manager/?action=SwitchVPN&name={})'.format(encoded)
    xbmc.executebuiltin(cmd)


def get_vpn_iface():
    # Detect a tun*/wg* interface if present
    cand = []
    try:
        for path in glob.glob("/sys/class/net/*"):
            base = os.path.basename(path)
            if base.startswith("tun") or base.startswith("wg"):
                cand.append(base)
    except Exception:
        pass
    return cand[0] if cand else ""


def read_rx_bytes(iface):
    try:
        p = "/sys/class/net/{}/statistics/rx_bytes".format(iface)
        with io.open(p, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return -1


def wait_for_connected(target_profile, start_iface):
    # Try two signals of success:
    # 1) VPN Manager service.log shows "... Connected"
    # 2) a tun*/wg* interface appears and rx_bytes increases
    t0 = time.time()
    last_line = ""
    have_iface = start_iface or get_vpn_iface()
    rx0 = read_rx_bytes(have_iface) if have_iface else -1

    while time.time() - t0 < CONNECT_TIMEOUT_SEC:
        # signal #1: look for "Connected" in VPN Manager log
        try:
            if os.path.exists(VPNMGR_LOG):
                with io.open(VPNMGR_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-80:]
                for ln in reversed(lines):
                    last_line = ln.strip()
                    if "Connected" in ln and target_profile in ln:
                        return True, "VPN Manager reports Connected"
        except Exception:
            pass

        # signal #2: interface activity
        iface_now = get_vpn_iface()
        if iface_now:
            rx_now = read_rx_bytes(iface_now)
            if rx0 >= 0 and rx_now > rx0 + 4096:  # some traffic flowed
                return True, "Tunnel {} active (rx_bytes grew)".format(iface_now)
            # initialize baseline if we just saw the iface appear
            if rx0 < 0:
                rx0 = rx_now

        xbmc.sleep(800)

    return False, "Timeout waiting for VPN connect. Last hint: {}".format(last_line)


# ---------- MAIN SERVICE ----------


class Player(xbmc.Player):
    def __init__(self):
        super(Player, self).__init__()
        self.cc_map, self.cc_map_path = load_cc_map()
        self.cc2profile = DEFAULT_CC2PROFILE.copy()
        self.cc2profile.update(load_json(OVERRIDE, {}))
        self.state = load_json(STATE, {})
        if self.cc_map_path:
            klog("Loaded cc_map {} entries from {}; cc_to_profile={}".format(
                len(self.cc_map), self.cc_map_path, self.cc2profile))
        else:
            klog("No cc_map found; cc_to_profile={}".format(self.cc2profile))

    def onAVStarted(self):
        # small delay for labels to populate
        xbmc.sleep(700)

        if not playing_is_pvr():
            klog("Playback detected but not PVR; leaving to VPN Manager add-on rules.")
            return

        channel = get_channel_name()
        if not channel:
            klog("PVR playback but channel name is empty; no action.")
            return

        # find country
        cc = (self.cc_map.get(channel) or
              self.cc_map.get(channel.replace(" HD", "").strip()) or
              "")
        if not cc:
            klog("No country mapping for channel='{}'; no VPN switch".format(channel))
            return

        # profile to use
        target = self.cc2profile.get(cc)
        if not target:
            klog("No profile mapped for cc='{}' (channel='{}')".format(cc, channel))
            return

        # cooldown / dedupe
        now = int(time.time())
        last = self.state.get("last_profile") or ""
        last_ts = int(self.state.get("ts") or 0)
        if last == target and (now - last_ts) < COOLDOWN_SEC:
            klog("Skip switch: already on '{}' ({}s ago)".format(target, now - last_ts))
            return

        # snapshot current VPN state
        iface_before = get_vpn_iface()
        rx_before = read_rx_bytes(iface_before) if iface_before else -1
        klog("PVR start: channel='{}', cc='{}', target_profile='{}', iface_before='{}', rx_before={}".format(
            channel, cc, target, iface_before, rx_before))

        # perform switch
        run_vpn_switch(target)

        ok, reason = wait_for_connected(target, iface_before)
        klog("VPN switch result: success={} ({})".format(ok, reason))

        self.state = {"last_profile": target, "ts": now}
        save_json(STATE, self.state)


class Monitor(xbmc.Monitor):
    pass


if __name__ == "__main__":
    try:
        ensure_dir(CFG_DIR)
        # touch the log at startup
        if LOG_ENABLED:
            with io.open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(u"\n==== service.channel_vpn_cc start ====\n")
        klog("Service ready (PVR-only; add-ons handled by VPN Manager)")
        mon = Monitor()
        player = Player()
        while not mon.abortRequested():
            if mon.waitForAbort(1):
                break
    except Exception as e:
        klog("FATAL service error: {}".format(e))
