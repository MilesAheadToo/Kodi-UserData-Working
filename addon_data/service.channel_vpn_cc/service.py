# -*- coding: utf-8 -*-
# Drop-in replacement for service.channel_vpn_cc/service.py with detailed logging

import glob
import io
import json
import os
import re
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
CHANNEL_LABELS = (
    "PVR.ChannelName",
    "VideoPlayer.ChannelName",
    "ListItem.ChannelName",
    "ListItem.Label",
    "ListItem.Title",
    "VideoPlayer.Title",
    "VideoPlayer.TVShowTitle",
    "VideoPlayer.OriginalTitle",
    "Player.Title",
)
CHANNEL_PLACEHOLDERS = {
    "",
    "UNKNOWN",
    "Unknown",
    "Unknown Title",
    "Unknown Title (Unmatched)",
    "Live TV",
} | set(CHANNEL_LABELS)

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
DEFAULT_M3U = "/storage/downloads/pruned_tv.m3u"
cfg_candidate = resolve_path(ADDON.getAddonInfo("profile")).rstrip("\\/")
if not cfg_candidate:
    cfg_candidate = os.path.join(resolve_path("special://profile/addon_data"), ADDON_ID)
CFG_DIR = cfg_candidate
OVERRIDE = os.path.join(CFG_DIR, "cc_to_profile.json")   # {"GB":"UK_Docklands (UDP)", ...}
STATE = os.path.join(CFG_DIR, "state.json")           # {"last_profile":"...", "ts": 1699999999}

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
    def parse_m3u(full_path):
        mapping = {}
        pattern_country = re.compile(r'tvg-country="([^"]+)"', re.IGNORECASE)
        pattern_tvgid = re.compile(r'tvg-id="([^"]+)"', re.IGNORECASE)
        try:
            with io.open(full_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line.startswith("#EXTINF"):
                        continue

                    parts = line.split(",", 1)
                    if len(parts) != 2:
                        continue
                    channel_name = parts[1].strip()
                    if not channel_name:
                        continue

                    country = ""
                    m_country = pattern_country.search(parts[0])
                    if m_country:
                        country = m_country.group(1).strip().upper()
                    if not country:
                        m_tvgid = pattern_tvgid.search(parts[0])
                        if m_tvgid:
                            tvg_id = m_tvgid.group(1)
                            match = re.search(r'\.([A-Za-z]{2})(?:[^A-Za-z]|$)', tvg_id)
                            if match:
                                country = match.group(1).upper()
                    if country:
                        mapping[channel_name] = country
        except FileNotFoundError:
            raise
        except Exception as e:
            klog("WARN cannot parse m3u {}: {}".format(full_path, e))
        return mapping

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

    candidates.extend([
        os.path.join(CFG_DIR, "pruned_tv.m3u"),
        DEFAULT_M3U
    ])

    for raw_path in candidates:
        if not raw_path:
            continue
        full = resolve_path(raw_path)
        if not os.path.isfile(full):
            continue
        try:
            mapping = parse_m3u(full)
            if mapping:
                return mapping, full
        except FileNotFoundError:
            continue
    return {}, ""


def load_cc_profile_map():
    data = load_json(OVERRIDE, {})
    if not isinstance(data, dict):
        data = {}

    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}

    def resolve_profile_name(profile_key):
        if not isinstance(profile_key, str):
            return ""
        key = profile_key.strip()
        if not key:
            return ""
        profile_data = profiles.get(key)
        if isinstance(profile_data, dict):
            label = profile_data.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
        return key

    default_profile_key = str(data.get("default_profile") or "").strip()
    default_profile = resolve_profile_name(default_profile_key) if default_profile_key else ""
    default_when_unknown = str(data.get("default_when_unknown", "leave")).strip().lower()

    cc_map = {}
    by_country = data.get("mappings", {}).get("by_country_code")
    if isinstance(by_country, dict):
        for cc, profile_key in by_country.items():
            if not isinstance(cc, str):
                continue
            profile_name = resolve_profile_name(profile_key)
            if profile_name:
                cc_map[cc.strip().upper()] = profile_name

    # Support legacy top-level structures that map directly
    for cc, profile in data.items():
        if not isinstance(cc, str) or not isinstance(profile, str):
            continue
        cc_clean = cc.strip()
        profile_clean = profile.strip()
        if len(cc_clean) in (2, 3) and cc_clean.isalpha():
            cc_map.setdefault(cc_clean.upper(), profile_clean)

    return {
        "map": cc_map,
        "default_profile": default_profile,
        "default_when_unknown": default_when_unknown
    }


def playing_is_pvr():
    # True for Live TV / PVR playback only
    fp = xbmc.getInfoLabel('Player.FilenameAndPath') or ""
    if fp.startswith("pvr://"):
        return True
    if xbmc.getCondVisibility("PVR.IsPlayingTV") or xbmc.getCondVisibility("PVR.IsPlayingRadio"):
        return True
    return False


def _jsonrpc_channel_name():
    try:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "Player.GetItem",
            "params": {
                "playerid": 1,
                "properties": [
                    "channel",
                    "channeltype",
                    "title",
                    "showtitle",
                    "label",
                ],
            },
            "id": 1,
        })
        raw = xbmc.executeJSONRPC(payload)
        data = json.loads(raw)
        item = (data.get("result") or {}).get("item") or {}
        for key in ("channel", "label", "title", "showtitle"):
            val = item.get(key)
            if isinstance(val, str):
                val = val.strip()
                if val and val not in CHANNEL_PLACEHOLDERS:
                    return val
    except Exception as exc:
        klog("WARN JSONRPC channel lookup failed: {}".format(exc))
    return ""


def get_channel_name(max_wait_ms=5000):
    """Resolve a stable channel name, waiting briefly until info labels populate."""
    deadline = time.time() + (max_wait_ms / 1000.0)
    last_seen = {}
    while True:
        for label in CHANNEL_LABELS:
            value = (xbmc.getInfoLabel(label) or "").strip()
            if value:
                last_seen[label] = value
            if value and value not in CHANNEL_PLACEHOLDERS:
                return value
        if time.time() >= deadline:
            break
        xbmc.sleep(200)

    # JSON-RPC fallback (may have fresher metadata)
    via_jsonrpc = _jsonrpc_channel_name()
    if via_jsonrpc:
        return via_jsonrpc

    if last_seen:
        pairs = ["{}='{}'".format(k, v) for k, v in sorted(last_seen.items())]
        klog("Channel labels unresolved; last seen: {}".format("; ".join(pairs)))
    return ""


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
        profile_cfg = load_cc_profile_map()
        self.cc2profile = profile_cfg["map"]
        self.default_profile = profile_cfg["default_profile"]
        self.default_when_unknown = profile_cfg["default_when_unknown"]
        self.state = load_json(STATE, {})
        if self.cc_map_path:
            klog("Loaded {} channel country mappings from {}".format(
                len(self.cc_map), self.cc_map_path))
        else:
            klog("No channel country map found; VPN switching disabled until data is available.")
        if self.cc2profile:
            klog("Loaded {} country-to-profile mappings from cc_to_profile.json".format(len(self.cc2profile)))
        else:
            klog("cc_to_profile.json is missing or has no mappings; VPN switching disabled.")
        klog("Fallback behaviour='{}', default_profile='{}'".format(
            self.default_when_unknown, self.default_profile or ""))

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
        if cc:
            cc = cc.upper()

        target = None
        cc_for_log = cc or "UNKNOWN"

        if not cc:
            if self.default_profile and self.default_when_unknown == "default":
                target = self.default_profile
                cc_for_log = "DEFAULT"
                klog("No country mapping for channel='{}'; using default VPN profile '{}'".format(
                    channel, target))
            else:
                klog("No country mapping for channel='{}'; no VPN switch".format(channel))
                return
        else:
            target = self.cc2profile.get(cc)
            if not target:
                if self.default_profile and self.default_when_unknown == "default":
                    target = self.default_profile
                    cc_for_log = "DEFAULT"
                    klog("No profile mapped for cc='{}' (channel='{}'); falling back to default profile '{}'".format(
                        cc, channel, target))
                else:
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
            channel, cc_for_log, target, iface_before, rx_before))

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
        klog("Running service script from {}".format(__file__))
        klog("Service ready (PVR-only; add-ons handled by VPN Manager)")
        mon = Monitor()
        player = Player()
        while not mon.abortRequested():
            if mon.waitForAbort(1):
                break
    except Exception as e:
        klog("FATAL service error: {}".format(e))
