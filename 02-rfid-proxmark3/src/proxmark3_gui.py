#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxmark3 Control - kuttu edition
=================================
A simple Windows GUI front-end for the Proxmark3 Easy (512 KB) running the
Iceman / RRG firmware (RfidResearchGroup/proxmark3).

What it does
------------
- Lists every card/tag family the device supports (LF 125 kHz + HF 13.56 MHz).
- Auto-detects a card you place on the antenna.
- After detection it shows what is possible for THAT card:
    Read / Details  -> always available
    Clone / Edit / Format -> only enabled if the card allows it, otherwise grey.
- A raw console tab lets you type any Iceman command yourself.

How it works under the hood
---------------------------
Every button simply runs the official client once:
    proxmark3.exe  COMx  -c "<command>"
captures the text output, and prints it to the console at the bottom.
So this GUI is only a friendly layer on top of the real client - nothing more,
nothing hidden.

Requirements
------------
- Python 3.8+  (ships with tkinter, already included on standard Windows Python)
- The Iceman Proxmark3 client  (proxmark3.exe) - see the README.
- pyserial is OPTIONAL. If installed, COM ports are auto-listed; if not, you
  type the port yourself (e.g. COM5).

Legal note: use this only on cards you own or are authorised to test.
"""

import os
import sys
import time
import threading
import subprocess
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import glob

# pyserial is optional - used only to auto-list COM ports
try:
    from serial.tools import list_ports as _list_ports
    HAVE_PYSERIAL = True
except Exception:
    HAVE_PYSERIAL = False

# winreg is built into Python on Windows - lets us read the COM ports and the
# Proxmark's own port straight from the registry (same source as Device
# Manager), so no pyserial needed.
try:
    import winreg
    HAVE_WINREG = True
except Exception:
    HAVE_WINREG = False


# Proxmark3 USB id (Iceman/RRG CDC device) - used to pick its COM port.
PM3_VID = "9AC4"


def list_com_ports():
    """All active COM ports, read from the registry (no pyserial needed)."""
    ports = []
    if HAVE_WINREG:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DEVICEMAP\SERIALCOMM")
            i = 0
            while True:
                try:
                    _, val, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                ports.append(val)
        except OSError:
            pass
    if not ports and HAVE_PYSERIAL:
        try:
            ports = [p.device for p in _list_ports.comports()]
        except Exception:
            ports = []
    # natural-ish sort (COM3 before COM10)
    def _n(p):
        d = "".join(ch for ch in p if ch.isdigit())
        return int(d) if d else 0
    return sorted(set(ports), key=_n)


def find_proxmark_port():
    """Return the COM port of the Proxmark3 itself, by scanning the USB enum
    for its vendor id - exactly how Device Manager knows which port it is."""
    if not HAVE_WINREG:
        return None
    try:
        usb = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Enum\USB")
    except OSError:
        return None
    i = 0
    while True:
        try:
            dev = winreg.EnumKey(usb, i)
        except OSError:
            break
        i += 1
        if PM3_VID not in dev.upper() and "PROXMARK" not in dev.upper():
            continue
        try:
            devkey = winreg.OpenKey(usb, dev)
        except OSError:
            continue
        j = 0
        while True:
            try:
                inst = winreg.EnumKey(devkey, j)
            except OSError:
                break
            j += 1
            try:
                dp = winreg.OpenKey(devkey, inst + r"\Device Parameters")
                port, _ = winreg.QueryValueEx(dp, "PortName")
                if port:
                    return port
            except OSError:
                pass
    return None


def find_client_exe():
    """Locate proxmark3.exe automatically: known paths first, then a glob of
    the user's Downloads (any rrg_other.../client folder)."""
    up = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    patterns = [
        os.path.join(up, "Downloads", "*", "client", "proxmark3.exe"),
        os.path.join(up, "Downloads", "proxmark3", "client", "proxmark3.exe"),
        os.path.join(up, "Downloads", "*", "proxmark3.exe"),
        r"C:\ProxSpace\pm3\proxmark3.exe",
        r"C:\proxmark3\client\proxmark3.exe",
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return "proxmark3.exe"


# ----------------------------------------------------------------------------
# CARD DATABASE
# ----------------------------------------------------------------------------
# Each card defines:
#   key      - internal id
#   name     - shown in the list
#   freq     - "LF" or "HF"
#   detect   - substrings that identify it in `lf search` / `hf search` output
#   what     - one-line plain description (shown in the detail panel)
#   caps     - which of the 5 actions are allowed
#   cmds     - the Iceman command for each allowed action
#
# {id} / {uid} placeholders are filled from the last read/detect.
# "DIALOG:xxx" means the action opens a small input dialog (see run_action).
# ----------------------------------------------------------------------------

CARD_DB = [
    # ---------------- LF 125 / 134 kHz ----------------
    dict(key="em410x", name="EM4100 / EM410x", freq="LF",
         detect=["EM410x", "EM 410x", "EM410"],
         what="Most common 125 kHz blue fob / white card. Fully read, clone and emulate.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf em 410x reader",
                   details="lf search",
                   clone="lf em 410x clone --id {id}")),

    dict(key="hid", name="HID Prox (H10301 ...)", freq="LF",
         detect=["HID Prox", "HID "],
         what="Classic office access fob. Read and clone to T5577.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf hid reader",
                   details="lf hid reader",
                   clone="lf hid clone -w H10301 --id {id}")),

    dict(key="indala", name="Indala", freq="LF",
         detect=["Indala"],
         what="Motorola/HID Indala prox. Read and clone.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf indala reader",
                   details="lf indala reader",
                   clone="lf indala clone --heden {id}")),

    dict(key="awid", name="AWID", freq="LF",
         detect=["AWID"],
         what="AWID access control. Read and clone.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf awid reader",
                   details="lf awid reader",
                   clone="lf awid clone")),

    dict(key="ioprox", name="ioProx (Kantech)", freq="LF",
         detect=["ioProx", "IoProx", "Kantech"],
         what="Kantech ioProx. Read and clone.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf io reader",
                   details="lf io reader",
                   clone="lf io clone")),

    dict(key="paradox", name="Paradox", freq="LF",
         detect=["Paradox"],
         what="Paradox prox. Read; clone support is limited.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf paradox reader",
                   details="lf paradox reader",
                   clone="lf paradox clone")),

    dict(key="fdxb", name="FDX-B animal chip (134 kHz)", freq="LF",
         detect=["FDX-B", "FDX"],
         what="Pet / livestock microchip. Read and clone to T5577.",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="lf fdxb reader",
                   details="lf fdxb reader",
                   clone="lf fdxb clone --id {id}")),

    dict(key="hitag2", name="Hitag2 / HitagS", freq="LF",
         detect=["Hitag"],
         what="Hitag2 (car immobiliser type). Read; keys can be cracked.",
         caps=dict(read=1, details=1, clone=0, edit=1, fmt=0),
         cmds=dict(read="lf hitag reader",
                   details="lf hitag info",
                   edit="DIALOG:hitag_wr")),

    dict(key="t5577", name="T5577 (writable blank)", freq="LF",
         detect=["T55", "T5577"],
         what="The blank chip you CLONE ONTO. Fully read, write and wipe.",
         caps=dict(read=1, details=1, clone=0, edit=1, fmt=1),
         cmds=dict(read="lf t55xx detect",
                   details="lf t55xx info",
                   edit="DIALOG:t55_wr",
                   fmt="lf t55xx wipe")),

    dict(key="em4305", name="EM4305 / EM4469 (writable)", freq="LF",
         detect=["EM4305", "EM4x05", "EM4469"],
         what="Writable EM chip, alternative blank. Read, write and wipe.",
         caps=dict(read=1, details=1, clone=0, edit=1, fmt=1),
         cmds=dict(read="lf em 4x05 info",
                   details="lf em 4x05 dump",
                   edit="DIALOG:em4x05_wr",
                   fmt="lf em 4x05 wipe")),

    # ---------------- HF 13.56 MHz ----------------
    dict(key="mfc", name="MIFARE Classic 1K / 4K / Mini", freq="HF",
         detect=["MIFARE Classic", "MIFARE CLASSIC", "Mifare Classic"],
         what="Most common HF card. Crack keys, dump, clone to magic card, emulate.",
         caps=dict(read=1, details=1, clone=1, edit=1, fmt=1),
         cmds=dict(read="hf mf autopwn",
                   details="hf 14a info",
                   clone="hf mf cload -f hf-mf-{id}-dump.bin",
                   edit="DIALOG:mf_wr",
                   fmt="hf mf wipe")),

    dict(key="mfu", name="MIFARE Ultralight / C / EV1", freq="HF",
         detect=["MIFARE Ultralight", "Ultralight", "MFU"],
         what="Ultralight / NTAG family. Read, write blocks, emulate.",
         caps=dict(read=1, details=1, clone=1, edit=1, fmt=0),
         cmds=dict(read="hf mfu dump",
                   details="hf mfu info",
                   clone="hf mfu restore",
                   edit="DIALOG:mfu_wr")),

    dict(key="ntag", name="NTAG213 / 215 / 216", freq="HF",
         detect=["NTAG", "NTAG213", "NTAG215", "NTAG216"],
         what="NTAG (URL tags, amiibo-style). Read, write, emulate.",
         caps=dict(read=1, details=1, clone=1, edit=1, fmt=1),
         cmds=dict(read="hf mfu dump",
                   details="hf mfu info",
                   clone="hf mfu restore",
                   edit="DIALOG:mfu_wr",
                   fmt="hf mfu setpwd")),

    dict(key="iclass", name="iCLASS legacy / Elite", freq="HF",
         detect=["iClass", "iCLASS", "PicoPass"],
         what="HID iCLASS legacy. Read, dump, clone (legacy only).",
         caps=dict(read=1, details=1, clone=1, edit=0, fmt=0),
         cmds=dict(read="hf iclass dump",
                   details="hf iclass info",
                   clone="hf iclass restore")),

    dict(key="iclass_se", name="iCLASS SE / Seos", freq="HF",
         detect=["Seos", "iCLASS SE", "SE "],
         what="Modern secure iCLASS. UID only - CANNOT be cloned (AES protected).",
         caps=dict(read=1, details=1, clone=0, edit=0, fmt=0),
         cmds=dict(read="hf iclass info",
                   details="hf 14a info")),

    dict(key="iso15", name="ISO15693 / ICODE / Tag-it", freq="HF",
         detect=["ISO15693", "ICODE", "iCode", "Tag-it", "15693"],
         what="ISO15693 vicinity cards. Read, dump, write.",
         caps=dict(read=1, details=1, clone=1, edit=1, fmt=0),
         cmds=dict(read="hf 15 dump",
                   details="hf 15 info",
                   clone="hf 15 restore",
                   edit="DIALOG:iso15_wr")),

    dict(key="desfire", name="MIFARE DESFire EV1/2/3", freq="HF",
         detect=["DESFire", "Desfire", "MIFARE DESFire"],
         what="Secure AES/3DES card. Read info/apps only - CANNOT be cloned.",
         caps=dict(read=1, details=1, clone=0, edit=0, fmt=0),
         cmds=dict(read="hf mfdes info",
                   details="hf mfdes list")),

    dict(key="mfplus", name="MIFARE Plus", freq="HF",
         detect=["MIFARE Plus", "Mifare Plus"],
         what="Partially secure. Read info; cloning not supported in SL3.",
         caps=dict(read=1, details=1, clone=0, edit=0, fmt=0),
         cmds=dict(read="hf mfp info",
                   details="hf 14a info")),

    dict(key="emv", name="EMV bank / credit card", freq="HF",
         detect=["EMV", "Visa", "Mastercard", "MasterCard"],
         what="Bank card. Read public data only - NEVER cloneable (by design).",
         caps=dict(read=1, details=1, clone=0, edit=0, fmt=0),
         cmds=dict(read="emv reader",
                   details="emv info")),

    dict(key="topaz", name="Topaz / Jewel / SRIX / FeliCa / Legic", freq="HF",
         detect=["Topaz", "Jewel", "SRIX", "SRI512", "FeliCa", "Felica", "Legic"],
         what="Assorted HF tags. Mostly read / sniff; clone varies by type.",
         caps=dict(read=1, details=1, clone=0, edit=0, fmt=0),
         cmds=dict(read="hf search",
                   details="hf 14a info")),
]

# ---------------------------------------------------------------------------
# Normalise capabilities so EVERY card type can be Read, show Details, and has
# a Format button. Format does a real wipe on writable cards; on read-only or
# encrypted cards it opens a short explanation (an "INFO:" command) instead of
# being greyed out.
# ---------------------------------------------------------------------------
_RO_FMT = {"iclass", "iclass_se", "desfire", "mfplus", "emv", "topaz"}
_RO_MSG = ("INFO:This card is read-only or encrypted, so it can't be "
           "formatted / wiped.\n\nOnly blank or magic cards can be wiped:\n"
           "• LF: T5577 or EM4305\n"
           "• HF: magic MIFARE, Ultralight or NTAG")
_HF_NOFMT = ("INFO:No one-tap wipe for this HF tag.\n\nUse the Edit button to "
             "overwrite its blocks/pages, or clone fresh data over it.")

_UID_RO = ("INFO:Changing the UID/ID needs a writable or magic card of this "
           "type.\n\n• LF: write to a T5577 / EM4305 blank\n"
           "• HF: a magic MIFARE / Ultralight / NTAG (UID-writable) card\n\n"
           "A genuine original card has a locked UID that can't be changed.")

for _c in CARD_DB:
    _c["caps"]["read"] = 1
    _c["caps"]["details"] = 1
    _c["caps"]["fmt"] = 1
    _c["caps"]["uid"] = 1
    _cmds = _c["cmds"]
    _cmds.setdefault("details", _cmds.get("read", "hf search"))
    if "fmt" not in _cmds:
        if _c["key"] in _RO_FMT:
            _cmds["fmt"] = _RO_MSG
        elif _c["freq"] == "LF":
            _cmds["fmt"] = "lf t55xx wipe"      # wipe the T5577/EM blank
        else:
            _cmds["fmt"] = _HF_NOFMT
    # Set UID/ID command: write a new UID/ID where the hardware allows it
    _clone = _cmds.get("clone", "")
    if _c["key"] == "mfc":
        _cmds["uid"] = "hf mf csetuid -u {id}"     # magic MIFARE (Gen1a/Gen2)
    elif _c["key"] in ("mfu", "ntag"):
        _cmds["uid"] = "hf mfu setuid -u {id}"     # magic Ultralight / NTAG
    elif "{id}" in _clone:
        _cmds["uid"] = _clone                      # LF clone-with-id writes it
    elif _c["freq"] == "LF" and _clone:
        _cmds["uid"] = _clone
    else:
        _cmds["uid"] = _UID_RO
    # MIFARE Classic: quick Read; keep autopwn as the clone data source
    if _c["key"] == "mfc":
        _cmds["clone_src"] = "hf mf autopwn"
        _cmds["read"] = "hf 14a info"

CAP_LABELS = [("read", "Read"), ("details", "Details"),
              ("clone", "Clone"), ("edit", "Edit"),
              ("uid", "Set UID"), ("fmt", "Format")]

# Icons (unicode) + colour per capability chip
CAP_ICON = dict(read="\U0001F50D", details="ℹ", clone="\U0001F5D0",
                edit="✎", uid="\U0001F194", fmt="\U0001F5D1")
CAP_COLOR = dict(read="#2563eb", details="#475569", clone="#16a34a",
                 edit="#d97706", uid="#7c3aed", fmt="#dc2626")

# ---- UI palette (professional blue/slate theme) ----
C_BRAND = "#1d4ed8"      # header / primary
C_BRAND_DK = "#1e3a8a"
C_SIDEBAR = "#0f172a"    # dark card list
C_SIDE_FG = "#cbd5e1"
C_SIDE_HEAD = "#38bdf8"
C_CONTENT = "#eef2f7"    # page background
C_CARD = "#ffffff"
C_BORDER = "#dbe2ea"
C_INK = "#0f172a"
C_MUTED = "#64748b"
C_OK = "#16a34a"
C_ERR = "#dc2626"
C_WARN = "#d97706"
CONSOLE_BG = "#0b1021"
FREQ_COLOR = dict(LF="#b45309", HF="#1d4ed8")   # badge colours

# Selectable window themes. Each sets the module colour globals.
THEMES = {
    "Ocean (blue)": dict(
        C_BRAND="#1d4ed8", C_BRAND_DK="#1e3a8a", C_SIDEBAR="#0f172a",
        C_SIDE_FG="#cbd5e1", C_SIDE_HEAD="#38bdf8", C_CONTENT="#eef2f7",
        C_CARD="#ffffff", C_BORDER="#dbe2ea", C_INK="#0f172a",
        C_MUTED="#64748b", CONSOLE_BG="#0b1021"),
    "Midnight (dark)": dict(
        C_BRAND="#1f2937", C_BRAND_DK="#0b1220", C_SIDEBAR="#0b1220",
        C_SIDE_FG="#cbd5e1", C_SIDE_HEAD="#60a5fa", C_CONTENT="#111827",
        C_CARD="#1f2937", C_BORDER="#374151", C_INK="#f3f4f6",
        C_MUTED="#9ca3af", CONSOLE_BG="#000000"),
    "Hacker (green)": dict(
        C_BRAND="#065f46", C_BRAND_DK="#022c22", C_SIDEBAR="#03140f",
        C_SIDE_FG="#86efac", C_SIDE_HEAD="#4ade80", C_CONTENT="#0b1f17",
        C_CARD="#052e23", C_BORDER="#14532d", C_INK="#d1fae5",
        C_MUTED="#4ade80", CONSOLE_BG="#001a10"),
    "Light (slate)": dict(
        C_BRAND="#2563eb", C_BRAND_DK="#1e40af", C_SIDEBAR="#1e293b",
        C_SIDE_FG="#e2e8f0", C_SIDE_HEAD="#38bdf8", C_CONTENT="#f8fafc",
        C_CARD="#ffffff", C_BORDER="#e2e8f0", C_INK="#0f172a",
        C_MUTED="#64748b", CONSOLE_BG="#0b1021"),
    "Purple": dict(
        C_BRAND="#6d28d9", C_BRAND_DK="#4c1d95", C_SIDEBAR="#1e1b2e",
        C_SIDE_FG="#ddd6fe", C_SIDE_HEAD="#a78bfa", C_CONTENT="#f5f3ff",
        C_CARD="#ffffff", C_BORDER="#e9d5ff", C_INK="#2e1065",
        C_MUTED="#7c6f93", CONSOLE_BG="#140f26"),
}


# ----------------------------------------------------------------------------
# Tooltip helper
# ----------------------------------------------------------------------------
class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(tw, text=self.text, justify="left",
                       bg="#111", fg="#eee", relief="solid", borderwidth=1,
                       font=("Segoe UI", 9), wraplength=320, padx=8, pady=5)
        lbl.pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ----------------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------------
class ProxmarkGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Proxmark3 Control - kuttu edition")
        self.geometry("1120x760")
        self.minsize(960, 660)
        self.configure(bg=C_CONTENT)

        self.exe_path = tk.StringVar(value=self._guess_exe())
        self.port = tk.StringVar(value="")
        self.current_card = None          # currently selected / detected card dict
        self.last_id = ""                 # last read UID / ID (for {id})
        self.msg_queue = queue.Queue()    # console lines from worker threads
        self.busy = False
        self.theme_name = "Ocean (blue)"
        self.theme_var = tk.StringVar(value=self.theme_name)

        self._setup_style()
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_console()
        self._build_statusbar()

        self.after(80, self._drain_queue)
        self.refresh_ports()
        if os.path.exists(self.exe_path.get()):
            self.log("Client auto-found. Place a card and press "
                     "'Auto-detect card', or 'Test connection' first.", "ok")
        else:
            self.log("Could not auto-find proxmark3.exe - set it with the "
                     "'...' button. The COM port is auto-detected.", "warn")

    # ---------- helpers ----------
    def _guess_exe(self):
        return find_client_exe()

    # ---------- styling ----------
    def _setup_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", font=("Segoe UI", 10))
        st.configure("TFrame", background=C_CONTENT)
        st.configure("Card.TFrame", background=C_CARD)
        st.configure("TLabel", background=C_CONTENT, foreground=C_INK)
        st.configure("Card.TLabel", background=C_CARD, foreground=C_INK)
        st.configure("Muted.TLabel", background=C_CONTENT, foreground=C_MUTED)
        st.configure("TButton", padding=6)
        # dark card-list treeview
        st.configure("Side.Treeview", background=C_SIDEBAR,
                     fieldbackground=C_SIDEBAR, foreground=C_SIDE_FG,
                     borderwidth=0, rowheight=24, font=("Segoe UI", 10))
        st.map("Side.Treeview", background=[("selected", C_BRAND)],
               foreground=[("selected", "#ffffff")])
        st.configure("TNotebook", background=C_CONTENT, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(14, 6), font=("Segoe UI", 9, "bold"))
        st.configure("TCombobox", padding=4)

    def _accent_btn(self, parent, text, cmd, color=C_BRAND, fg="#ffffff"):
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg=fg,
                      activebackground=color, activeforeground=fg,
                      relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
                      font=("Segoe UI", 10, "bold"))
        return b

    # ---------- header banner ----------
    def _build_header(self):
        h = tk.Frame(self, bg=C_BRAND, height=62)
        h.pack(fill="x")
        h.pack_propagate(False)
        left = tk.Frame(h, bg=C_BRAND)
        left.pack(side="left", padx=16)
        tk.Label(left, text="\u26a1 Proxmark3 Control", bg=C_BRAND, fg="#ffffff",
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(9, 0))
        tk.Label(left, text="kuttu edition  \u00b7  Iceman RFID toolkit",
                 bg=C_BRAND, fg="#c7d2fe",
                 font=("Segoe UI", 9)).pack(anchor="w")
        # status pill
        self.status = tk.Label(h, text="\u25cf  not connected", bg="#334155",
                               fg="#e2e8f0", font=("Segoe UI", 9, "bold"),
                               padx=12, pady=5)
        self.status.pack(side="right", padx=16)

    # ---------- toolbar ----------
    def _build_toolbar(self):
        bar = tk.Frame(self, bg=C_CONTENT)
        bar.pack(fill="x", padx=14, pady=(10, 4))

        tk.Label(bar, text="Client", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        e = ttk.Entry(bar, textvariable=self.exe_path, width=40)
        e.pack(side="left", padx=(6, 4))
        ToolTip(e, "Path to proxmark3.exe (auto-found).")
        tk.Button(bar, text="\u2026", command=self.browse_exe, relief="flat",
                  bg="#cbd5e1", padx=8, cursor="hand2").pack(side="left")

        tk.Label(bar, text="  Port", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 0))
        self.port_box = ttk.Combobox(bar, textvariable=self.port, width=9,
                                     values=[])
        self.port_box.pack(side="left", padx=6)
        ToolTip(self.port_box, "COM port of your Proxmark3 (auto-detected).")
        tk.Button(bar, text="\u21bb Refresh", command=self.refresh_ports,
                  relief="flat", bg="#cbd5e1", padx=10, pady=4,
                  cursor="hand2").pack(side="left")

        self.test_btn = self._accent_btn(bar, "Test connection",
                                         self.test_connection)
        self.test_btn.pack(side="left", padx=(12, 0))
        ToolTip(self.test_btn, "Check the device responds (runs hw ver).")

        self.blank_btn = self._accent_btn(bar, "\U0001F50E Blank tester",
                                          self.open_blank_tester, color="#7c3aed")
        self.blank_btn.pack(side="left", padx=(8, 0))
        ToolTip(self.blank_btn,
                "Keeps scanning - place a card and it tells you whether it "
                "can be used as a blank for cloning.")

        # Theme picker (right side)
        theme_box = ttk.Combobox(bar, textvariable=self.theme_var, width=14,
                                 state="readonly", values=list(THEMES.keys()))
        theme_box.pack(side="right")
        theme_box.bind("<<ComboboxSelected>>",
                       lambda e: self.apply_theme(self.theme_var.get()))
        ToolTip(theme_box, "Change the window colour theme.")
        tk.Label(bar, text="\U0001F3A8 Theme", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 6))

    # ---------- body: card list | capability panel ----------
    def _build_body(self):
        body = tk.Frame(self, bg=C_CONTENT)
        body.pack(fill="both", expand=True, padx=14, pady=4)

        # --- left: dark card list ---
        left = tk.Frame(body, bg=C_SIDEBAR, width=250)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="SUPPORTED CARDS", bg=C_SIDEBAR, fg=C_SIDE_HEAD,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14,
                                                    pady=(12, 2))
        tk.Label(left, text="Click a card to see what it can do",
                 bg=C_SIDEBAR, fg="#64748b",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(0, 6))

        tw = tk.Frame(left, bg=C_SIDEBAR)
        tw.pack(fill="both", expand=True, padx=(8, 8), pady=(0, 10))
        self.tree = ttk.Treeview(tw, show="tree", selectmode="browse",
                                 style="Side.Treeview")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tw, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("group", font=("Segoe UI", 10, "bold"),
                                foreground=C_SIDE_HEAD)

        lf = self.tree.insert("", "end", text=" \U0001F7E0  LF  \u00b7  125 / 134 kHz",
                              open=True, tags=("group",))
        hf = self.tree.insert("", "end", text=" \U0001F535  HF  \u00b7  13.56 MHz",
                              open=True, tags=("group",))
        self.tree_map = {}
        for c in CARD_DB:
            parent = lf if c["freq"] == "LF" else hf
            iid = self.tree.insert(parent, "end", text="     " + c["name"])
            self.tree_map[iid] = c
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # --- right: content ---
        right = tk.Frame(body, bg=C_CONTENT)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        det = tk.Frame(right, bg=C_CONTENT)
        det.pack(fill="x")
        self.detect_btn = self._accent_btn(det, "\U0001F50D  Auto-detect card",
                                           self.auto_detect, color=C_OK)
        self.detect_btn.pack(side="left")
        ToolTip(self.detect_btn,
                "Place a card on the antenna, then press this. "
                "Runs lf search then hf search and identifies the card.")
        tk.Label(det, text="  place a card on the reader first",
                 bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")

        # detail card
        card = tk.Frame(right, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        card.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(card, bg=C_CARD)
        inner.pack(fill="x", padx=16, pady=14)

        row1 = tk.Frame(inner, bg=C_CARD)
        row1.pack(fill="x")
        self.card_title = tk.Label(row1, text="No card selected", bg=C_CARD,
                                   fg=C_INK, font=("Segoe UI", 14, "bold"))
        self.card_title.pack(side="left")
        self.card_freq = tk.Label(row1, text="", bg=C_CARD, fg="#ffffff",
                                  font=("Segoe UI", 8, "bold"), padx=8, pady=2)
        self.card_freq.pack(side="left", padx=10)

        self.card_what = tk.Label(inner, text="Pick a card from the list, or "
                                  "press Auto-detect.", bg=C_CARD, fg=C_MUTED,
                                  wraplength=560, justify="left",
                                  font=("Segoe UI", 10))
        self.card_what.pack(anchor="w", pady=(6, 8))
        self.card_uid = tk.Label(inner, text="", bg=C_CARD, fg=C_BRAND,
                                 font=("Consolas", 10, "bold"))
        self.card_uid.pack(anchor="w")

        tk.Label(right, text="WHAT YOU CAN DO", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(16, 6))

        self.cap_frame = tk.Frame(right, bg=C_CONTENT)
        self.cap_frame.pack(anchor="w")
        self.cap_buttons = {}
        tips = dict(read="Read the card and show its data.",
                    details="Show detailed chip info (type, UID, technology).",
                    clone="Guided clone: reads the original, checks the blank "
                          "card, confirms, writes, then verifies.",
                    edit="Write / change data on the card (block-level).",
                    uid="Write a new UID / ID (needs a writable or magic card).",
                    fmt="Wipe the card back to blank / factory.")
        for cap, label in CAP_LABELS:
            tip = tips.get(cap, label)
            b = tk.Button(self.cap_frame,
                          text=CAP_ICON.get(cap, "") + "  " + label, width=12,
                          state="disabled", relief="flat", bd=0, pady=9,
                          cursor="hand2", font=("Segoe UI", 10, "bold"),
                          command=lambda c=cap: self.run_action(c))
            b.pack(side="left", padx=(0, 8), pady=2)
            ToolTip(b, tip)
            self.cap_buttons[cap] = b

        tk.Label(right, text="Coloured = available for this card \u00b7  "
                 "Grey = not possible. Encrypted cards (DESFire, Seos, EMV) "
                 "are read-only.", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 8), wraplength=600,
                 justify="left").pack(anchor="w", pady=(10, 0))

        # Card information panel (filled after Read / Details / Auto-detect)
        tk.Label(right, text="CARD INFORMATION", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(14, 4))
        infowrap = tk.Frame(right, bg=C_CARD, highlightbackground=C_BORDER,
                            highlightthickness=1)
        infowrap.pack(fill="both", expand=True)
        self.info_box = tk.Text(infowrap, height=7, bg=C_CARD, fg=C_INK,
                                relief="flat", font=("Consolas", 10),
                                wrap="word", padx=10, pady=8)
        self.info_box.pack(side="left", fill="both", expand=True)
        isb = ttk.Scrollbar(infowrap, command=self.info_box.yview)
        isb.pack(side="left", fill="y")
        self.info_box.configure(yscrollcommand=isb.set, state="disabled")
        self.info_box.tag_config("hl", foreground=C_BRAND,
                                 font=("Consolas", 10, "bold"))
        self._set_info("Press Read, Details or Auto-detect to see the card's "
                       "UID and info here.")

    # ---------- console ----------
    def _build_console(self):
        wrap = tk.Frame(self, bg=C_CONTENT)
        wrap.pack(fill="both", expand=False, padx=14, pady=(10, 6))

        nb = ttk.Notebook(wrap)
        nb.pack(fill="both", expand=True)

        con = tk.Frame(nb, bg=CONSOLE_BG)
        nb.add(con, text="  Console output  ")
        bar = tk.Frame(con, bg=CONSOLE_BG)
        bar.pack(fill="x")
        tk.Label(bar, text="\u25cf", bg=CONSOLE_BG, fg="#ef4444",
                 font=("Segoe UI", 10)).pack(side="left", padx=(8, 2),
                                             pady=4)
        tk.Label(bar, text="\u25cf", bg=CONSOLE_BG, fg="#f59e0b",
                 font=("Segoe UI", 10)).pack(side="left", padx=2)
        tk.Label(bar, text="\u25cf", bg=CONSOLE_BG, fg="#22c55e",
                 font=("Segoe UI", 10)).pack(side="left", padx=2)
        tk.Label(bar, text="  pm3 console", bg=CONSOLE_BG, fg="#64748b",
                 font=("Consolas", 9)).pack(side="left")
        body = tk.Frame(con, bg=CONSOLE_BG)
        body.pack(fill="both", expand=True)
        self.console = tk.Text(body, height=11, bg=CONSOLE_BG, fg="#d6d6d6",
                               insertbackground="#d6d6d6", relief="flat",
                               font=("Consolas", 10), wrap="word", padx=8,
                               pady=4)
        self.console.pack(side="left", fill="both", expand=True)
        csb = ttk.Scrollbar(body, command=self.console.yview)
        csb.pack(side="left", fill="y")
        self.console.configure(yscrollcommand=csb.set, state="disabled")
        for tag, col in [("ok", "#4ade80"), ("info", "#60a5fa"),
                         ("warn", "#fbbf24"), ("err", "#f87171"),
                         ("cmd", "#a5b4fc")]:
            self.console.tag_config(tag, foreground=col)

        raw = tk.Frame(nb, bg=C_CONTENT)
        nb.add(raw, text="  Raw command  ")
        tk.Label(raw, text="Type any Iceman command and press Run:",
                 bg=C_CONTENT, fg=C_INK).pack(anchor="w", padx=8, pady=(8, 2))
        row = tk.Frame(raw, bg=C_CONTENT)
        row.pack(fill="x", padx=8)
        self.raw_entry = ttk.Entry(row)
        self.raw_entry.pack(side="left", fill="x", expand=True)
        self.raw_entry.bind("<Return>", lambda e: self.run_raw())
        self._accent_btn(row, "Run", self.run_raw).pack(side="left",
                                                        padx=(6, 0))
        tk.Label(raw, text="Examples:  hw ver   |   hw tune   |   lf search   "
                 "|   hf search   |   hf mf autopwn", bg=C_CONTENT, fg=C_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(6, 8))

    # ---------- status bar ----------
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=C_BRAND_DK, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.statusbar = tk.Label(bar, text="Ready", bg=C_BRAND_DK,
                                  fg="#c7d2fe", font=("Segoe UI", 9),
                                  anchor="w")
        self.statusbar.pack(side="left", padx=12)
        tk.Label(bar, text="Proxmark3 Easy 512 KB  \u00b7  Iceman firmware",
                 bg=C_BRAND_DK, fg="#818cf8",
                 font=("Segoe UI", 8)).pack(side="right", padx=12)

    # ---------- theme switching ----------
    def apply_theme(self, name):
        if name not in THEMES:
            return
        globals().update(THEMES[name])      # repoint the colour globals
        self.theme_name = name
        # preserve state across the rebuild
        card = self.current_card
        exe, port = self.exe_path.get(), self.port.get()
        try:
            old = self.console.get("1.0", "end")
        except Exception:
            old = ""
        # tear down and rebuild the whole UI with the new palette
        for w in list(self.children.values()):
            try:
                w.destroy()
            except Exception:
                pass
        self.configure(bg=C_CONTENT)
        self._setup_style()
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_console()
        self._build_statusbar()
        # restore state
        self.exe_path.set(exe)
        self.port.set(port)
        self.theme_var.set(name)
        self.port_box["values"] = list_com_ports()
        if old.strip():
            self.console.configure(state="normal")
            self.console.insert("end", old.strip() + "\n")
            self.console.see("end")
            self.console.configure(state="disabled")
        if card:
            self._select_card(card, detected=False)
        self._set_status_bar("Theme: " + name)

    # ---------- console logging ----------
    def log(self, text, tag=None):
        self.msg_queue.put((text, tag))

    def _drain_queue(self):
        try:
            while True:
                text, tag = self.msg_queue.get_nowait()
                self.console.configure(state="normal")
                if tag:
                    self.console.insert("end", text + "\n", tag)
                else:
                    self.console.insert("end", text + "\n")
                self.console.see("end")
                self.console.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    # ---------- port / exe ----------
    def browse_exe(self):
        p = filedialog.askopenfilename(
            title="Select proxmark3.exe",
            filetypes=[("proxmark3 client", "proxmark3.exe"), ("All", "*.*")])
        if p:
            self.exe_path.set(p)

    def refresh_ports(self):
        ports = list_com_ports()
        self.port_box["values"] = ports
        pm3 = find_proxmark_port()
        if pm3:
            self.port.set(pm3)
            self.log(f"Proxmark3 auto-detected on {pm3}.", "ok")
        elif ports and not self.port.get():
            self.port.set(ports[0])
            self.log(f"No Proxmark id found; using {ports[0]}. "
                     "Pick another port if this is wrong.", "warn")
        elif not ports:
            self.log("No COM ports found. Plug in the Proxmark3 (data port) "
                     "and press Refresh.", "warn")

    # ---------- command runner ----------
    def _run_client(self, pm3_cmd, label=""):
        """Run a pm3 command, retrying once on a transient connection glitch
        (the previous command's COM port had not been released yet)."""
        out = self._run_once(pm3_cmd)
        low = out.lower()
        transient = ("failed to get current device config" in low or
                     "command execution time out" in low or
                     "failed to copy from graphbuffer" in low or
                     "pm3 is not available" in low)
        if transient and "[+]" not in out:
            self.log("Transient glitch (port still busy) - retrying once...",
                     "warn")
            time.sleep(0.8)
            out = self._run_once(pm3_cmd)
        return out

    def _run_once(self, pm3_cmd):
        """Run proxmark3.exe PORT -c "cmd" once and stream output."""
        exe = self.exe_path.get().strip()
        port = self.port.get().strip()
        if not port:
            self.log("No COM port set. Pick your port first.", "err")
            return ""
        if not (os.path.exists(exe) or exe == "proxmark3.exe"):
            self.log(f"Client not found: {exe}", "err")
            return ""

        self.log(f"[pm3] {port}  -c \"{pm3_cmd}\"", "cmd")
        # The Iceman client keeps its DLLs (libbz2-1.dll etc.) in a "libs"
        # subfolder next to proxmark3.exe. pm3.bat adds that folder to PATH;
        # we must do the same or the exe fails to start ("libbz2-1.dll not
        # found"). Add libs and libs\shell to PATH and run from the client dir.
        client_dir = os.path.dirname(os.path.abspath(exe))
        libs = os.path.join(client_dir, "libs")
        env = os.environ.copy()
        # Replicate the client's setup.bat so proxmark3.exe finds its DLLs
        # and Qt plugins (otherwise: "libbz2-1.dll was not found", etc.)
        if os.path.isdir(libs):
            env["HOME"] = client_dir + os.sep
            env["QT_PLUGIN_PATH"] = libs + os.sep
            env["QT_QPA_PLATFORM_PLUGIN_PATH"] = libs + os.sep
            env["MSYSTEM"] = "MINGW64"
            extra = os.pathsep.join([libs, os.path.join(libs, "shell")])
            env["PATH"] = extra + os.pathsep + env.get("PATH", "")
        try:
            proc = subprocess.Popen(
                [exe, port, "-c", pm3_cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
                cwd=client_dir if os.path.isdir(client_dir) else None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            self.log(f"Could not launch client: {exe}", "err")
            return ""
        except Exception as ex:
            self.log(f"Error: {ex}", "err")
            return ""

        # Hard timeout so a hanging command (e.g. a read with no card) can
        # never freeze the app. The watchdog kills the process after N seconds.
        TIMEOUT_S = 60
        timed_out = {"v": False}

        def _kill():
            timed_out["v"] = True
            try:
                proc.kill()
            except Exception:
                pass
        killer = threading.Timer(TIMEOUT_S, _kill)
        killer.start()

        out_lines = []
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                out_lines.append(line)
                tag = None
                low = line.lower()
                if "[+]" in line:
                    tag = "ok"
                elif "[=]" in line:
                    tag = "info"
                elif "[!]" in line or "fail" in low or "error" in low:
                    tag = "warn"
                self.log(line, tag)
        finally:
            proc.wait()
            killer.cancel()
        if timed_out["v"]:
            self.log(f"Command stopped after {TIMEOUT_S}s (it was hanging - "
                     "usually means no card on the reader). Re-place the card "
                     "and try again.", "err")
        time.sleep(0.35)   # let Windows release the COM port before next cmd
        return "\n".join(out_lines)

    def _guard(self):
        if self.busy:
            self.log("Busy - wait for the current command to finish.", "warn")
            return False
        if not self.port.get().strip():
            messagebox.showwarning("No port", "Pick your COM port first.")
            return False
        return True

    def _async(self, fn):
        self.busy = True
        self._set_enabled(False)

        def wrap():
            try:
                fn()
            finally:
                self.busy = False
                self.after(0, lambda: self._set_enabled(True))
        threading.Thread(target=wrap, daemon=True).start()

    def _set_enabled(self, on):
        state = "normal" if on else "disabled"
        self.detect_btn.configure(state=state)
        self.test_btn.configure(state=state)
        if on and self.current_card:
            self._apply_caps(self.current_card)
        elif not on:
            for b in self.cap_buttons.values():
                b.configure(state="disabled")

    # ---------- actions ----------
    def test_connection(self):
        if not self._guard():
            return
        def job():
            out = self._run_client("hw ver", "hw ver")
            if "Proxmark3" in out or "firmware" in out.lower() or "os:" in out.lower():
                self.after(0, lambda: self.status.configure(
                    text="\u25cf  connected", bg="#15803d", fg="#ffffff"))
                self.after(0, lambda: self._set_status_bar("Connected to Proxmark3"))
                self.log("Connection OK.", "ok")
            else:
                self.after(0, lambda: self.status.configure(
                    text="\u25cf  no response", bg="#b91c1c", fg="#ffffff"))
                self.after(0, lambda: self._set_status_bar(
                    "No response - check port / cable / firmware"))
                self.log("No response - check port / cable / firmware.", "err")
        self._async(job)

    def auto_detect(self):
        if not self._guard():
            return
        def job():
            self.log("Scanning LF then HF ...", "info")
            out = self._run_client("lf search", "lf search")
            card = self._match(out)
            if not card:
                out2 = self._run_client("hf search", "hf search")
                card = self._match(out2)
                out = out + "\n" + out2
            if card:
                self._extract_id(out)
                self.after(0, lambda: self._select_card(card, detected=True))
                self.log(f"Detected: {card['name']}", "ok")
                self._show_card_info(out)
            else:
                self.log("No known card found. Is it placed on the antenna?",
                         "warn")
                self._show_card_info(out)
        self._async(job)

    def _match(self, text):
        for c in CARD_DB:
            for token in c["detect"]:
                if token.lower() in text.lower():
                    return c
        return None

    def _extract_id(self, text):
        import re
        for pat in [r"ID[:\s]+([0-9A-Fa-f]{6,})",
                    r"UID[:\s]+([0-9A-Fa-f :]{6,})",
                    r"EM410x ID ([0-9A-Fa-f]+)"]:
            m = re.search(pat, text)
            if m:
                self.last_id = m.group(1).replace(" ", "").replace(":", "")
                self.after(0, lambda: self.card_uid.configure(
                    text=f"UID / ID: {self.last_id}"))
                return

    def on_tree_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        card = self.tree_map.get(sel[0])
        if card:
            self._select_card(card, detected=False)

    def _select_card(self, card, detected):
        self.current_card = card
        self.card_title.configure(text=card["name"])
        freq_txt = (" LF · 125/134 kHz " if card["freq"] == "LF"
                    else " HF · 13.56 MHz ")
        self.card_freq.configure(text=freq_txt,
                                 bg=FREQ_COLOR.get(card["freq"], C_BRAND))
        self.card_what.configure(text=card["what"], fg=C_INK)
        if detected:
            self.card_uid.configure(text="✓ detected"
                                    if not self.last_id else
                                    f"UID / ID: {self.last_id}")
        else:
            self.card_uid.configure(
                text="Tip: press Read to get this card's UID")
        self._set_status_bar(("Detected: " if detected else "Selected: ")
                             + card["name"])
        self._apply_caps(card)

    def _set_status_bar(self, text):
        if hasattr(self, "statusbar"):
            self.statusbar.configure(text=text)

    def _set_info(self, text):
        if not hasattr(self, "info_box"):
            return
        self.info_box.configure(state="normal")
        self.info_box.delete("1.0", "end")
        self.info_box.insert("end", text)
        self.info_box.configure(state="disabled")

    def _show_card_info(self, out):
        """Pull the useful lines (UID, type, config, ...) from a command's
        output and show them in the Card information panel."""
        keep = []
        for ln in out.splitlines():
            s = ln.strip()
            if not s:
                continue
            low = s.lower()
            useful = (s.startswith("[+]") or "uid" in low or " id" in low
                      or "id:" in low or "type" in low or "tag" in low
                      or "chip" in low or "config" in low or "block" in low
                      or "sak" in low or "atqa" in low or "manufacturer" in low)
            if not useful:
                continue
            for p in ("[+]", "[=]", "[usb|script]", "[!]", "[-]", "[#]"):
                if s.startswith(p):
                    s = s[len(p):].strip()
            if s:
                keep.append(s)
        if keep:
            text = "\n".join(keep[:60])
        else:
            text = ("No card information returned.\n"
                    "Is a card flat on the reader? Try again.")
        self.after(0, lambda: self._set_info(text))

    def _apply_caps(self, card):
        for cap, _ in CAP_LABELS:
            b = self.cap_buttons[cap]
            if card["caps"].get(cap, 0):
                col = CAP_COLOR.get(cap, C_BRAND)
                b.configure(state="normal", bg=col, fg="#ffffff",
                            activebackground=col, activeforeground="#ffffff")
            else:
                b.configure(state="disabled", bg="#e5e7eb", fg="#9ca3af",
                            disabledforeground="#9ca3af",
                            activebackground="#e5e7eb")

    def run_action(self, cap):
        if not self.current_card or not self._guard():
            return
        card = self.current_card
        cmd = card["cmds"].get(cap)
        if not cmd:
            self.log(f"'{cap}' has no command for this card.", "warn")
            return

        # dialogs for edit / special writes
        if cmd.startswith("DIALOG:"):
            self._edit_dialog(card, cmd.split(":", 1)[1])
            return

        # Clone opens the guided wizard: read original -> check the blank card
        # -> confirm -> write -> verify.
        if cap == "clone":
            CloneWizard(self, card)
            return

        # Set UID / ID
        if cap == "uid":
            self._setuid_dialog(card)
            return

        # Format / wipe: confirm, run, then always report completion.
        if cap == "fmt":
            if cmd.startswith("INFO:"):
                messagebox.showinfo("Format - not possible", cmd[5:])
                self.log("Format not possible for this card: it is read-only "
                         "or has no wipe command.", "warn")
                return
            note = ""
            if self.current_card["freq"] == "LF" and "t55" in cmd.lower():
                note = ("\n\nNote: this wipes the T5577 / blank card on the "
                        "reader - not a locked original.")
            if not messagebox.askyesno("Format / wipe",
                    "This ERASES the card on the reader back to blank." + note +
                    "\n\nContinue?"):
                return
            final = cmd.replace("{id}", self.last_id).replace("{uid}", self.last_id)

            def fmt_job():
                out = self._run_client(final, cap)
                ok = "fail" not in out.lower() and "error" not in out.lower()
                def done():
                    self.log("Formatting complete.", "ok")
                    messagebox.showinfo("Format",
                        "Formatting complete." if ok else
                        "Formatting complete (check the log for warnings).")
                self.after(0, done)
            self._async(fmt_job)
            return

        final = cmd.replace("{id}", self.last_id).replace("{uid}", self.last_id)
        if cap in ("read", "details"):
            def job():
                out = self._run_client(final, cap)
                self._extract_id(out)
                self._show_card_info(out)
            self._async(job)
            return
        self._async(lambda: self._run_client(final, cap))

    def run_cmd_async(self, cmd, on_done):
        """Run one pm3 command in the background; call on_done(output) on the
        main thread when finished. Used by the Clone Wizard."""
        if not self._guard():
            return False

        def job():
            out = self._run_client(cmd)
            if on_done:
                self.after(0, lambda: on_done(out))
        self._async(job)
        return True

    def _setuid_dialog(self, card):
        cmd = card["cmds"].get("uid", "")
        if cmd.startswith("INFO:"):
            messagebox.showinfo("Set UID / ID", cmd[5:])
            return
        win = tk.Toplevel(self)
        win.title("Set UID / ID - " + card["name"])
        win.geometry("460x240")
        win.transient(self)
        ttk.Label(win, text="Write a new UID / ID to " + card["name"],
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                      pady=(12, 2))
        hint = ("LF: this writes the new ID to the T5577 / blank on the reader."
                if card["freq"] == "LF" else
                "HF: this needs a magic (UID-writable) card on the reader.")
        ttk.Label(win, text=hint, foreground="#555", wraplength=420,
                  justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        row = ttk.Frame(win)
        row.pack(fill="x", padx=14)
        ttk.Label(row, text="New UID / ID (hex):").pack(side="left")
        ent = ttk.Entry(row, width=24)
        ent.pack(side="left", padx=6)
        if self.last_id:
            ent.insert(0, self.last_id)
        ttk.Label(win, text="For MIFARE enter 8 hex (4-byte) or 14 hex "
                  "(7-byte) UID.\nFor EM4100 enter 10 hex. HID: the card "
                  "number.", foreground="#777",
                  font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(8, 0))

        def do_set():
            val = ent.get().strip().replace(" ", "")
            if not val:
                messagebox.showwarning("Set UID", "Enter the new UID / ID.",
                                       parent=win)
                return
            final = cmd.replace("{id}", val).replace("{uid}", val)
            if not messagebox.askyesno(
                    "Confirm", "Write new UID / ID = " + val + " ?\n\n"
                    "Command:\n  " + final, parent=win):
                return
            self.last_id = val
            win.destroy()
            def job():
                out = self._run_client(final, "uid")
                low = out.lower()
                ok = "[+]" in out and "fail" not in low and "error" not in low
                self.after(0, lambda: messagebox.showinfo(
                    "Set UID",
                    "New UID / ID written." if ok else
                    "Command ran - check the console. If it failed, the card "
                    "may not be UID-writable (needs a magic/blank card)."))
            self._async(job)

        ttk.Button(win, text="Write UID / ID", command=do_set).pack(pady=14)

    def _edit_dialog(self, card, kind):
        win = tk.Toplevel(self)
        win.title("Edit / write block")
        win.geometry("420x200")
        ttk.Label(win, text=f"Write to {card['name']}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=8)
        frm = ttk.Frame(win)
        frm.pack(fill="x", padx=12)
        ttk.Label(frm, text="Block number:").grid(row=0, column=0, sticky="w")
        blk = ttk.Entry(frm, width=8)
        blk.grid(row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(frm, text="Data (hex):").grid(row=1, column=0, sticky="w")
        data = ttk.Entry(frm, width=36)
        data.grid(row=1, column=1, sticky="w", padx=6, pady=3)
        key = None
        if kind == "mf_wr":
            ttk.Label(frm, text="Key (hex, A):").grid(row=2, column=0, sticky="w")
            key = ttk.Entry(frm, width=16)
            key.insert(0, "FFFFFFFFFFFF")
            key.grid(row=2, column=1, sticky="w", padx=6, pady=3)

        def do_write():
            b = blk.get().strip()
            d = data.get().strip()
            if not b or not d:
                messagebox.showwarning("Missing", "Enter block and data.")
                return
            if kind == "mf_wr":
                cmd = f"hf mf wrbl --blk {b} -k {key.get().strip()} -d {d}"
            elif kind == "mfu_wr":
                cmd = f"hf mfu wrbl -b {b} -d {d}"
            elif kind == "t55_wr":
                cmd = f"lf t55xx write -b {b} -d {d}"
            elif kind == "em4x05_wr":
                cmd = f"lf em 4x05 write -a {b} -d {d}"
            elif kind == "iso15_wr":
                cmd = f"hf 15 wrbl -b {b} -d {d}"
            elif kind == "hitag_wr":
                cmd = f"lf hitag wrbl --page {b} -d {d}"
            else:
                cmd = ""
            win.destroy()
            if cmd:
                self._async(lambda: self._run_client(cmd, "edit"))

        ttk.Button(win, text="Write", command=do_write).pack(pady=12)

    def run_raw(self):
        cmd = self.raw_entry.get().strip()
        if not cmd or not self._guard():
            return
        self._async(lambda: self._run_client(cmd, "raw"))

    def open_blank_tester(self):
        if not self.port.get().strip():
            messagebox.showwarning("No port", "Pick your COM port first.")
            return
        BlankTester(self)


# ----------------------------------------------------------------------------
# Blank Card Tester - keeps scanning and says if a card is a usable blank
# ----------------------------------------------------------------------------
class BlankTester(tk.Toplevel):
    """Continuously scans the card on the reader and reports whether it can be
    used as a BLANK for cloning (T5577 / EM4305 for LF, magic card for HF)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.running = False
        self.title("Blank Card Tester")
        self.geometry("460x330")
        self.resizable(False, False)
        self.transient(app)

        tk.Label(self, text="\U0001F50E  Blank Card Tester",
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16,
                                                     pady=(14, 2))
        tk.Label(self, text="Place a card on the reader. I'll keep scanning and "
                 "tell you if it can be used as a blank for cloning.",
                 wraplength=420, justify="left", fg="#555").pack(anchor="w",
                                                                 padx=16)

        self.verdict = tk.Label(self, text="Press Start, then place a card",
                                font=("Segoe UI", 15, "bold"), wraplength=420,
                                justify="center", fg="#555")
        self.verdict.pack(pady=(22, 6), padx=16)
        self.detail = tk.Label(self, text="", wraplength=420, justify="center",
                               fg="#444", font=("Segoe UI", 10))
        self.detail.pack(padx=16)

        # Appears when the card is writable but has data on it - wipe to reuse.
        self._wipe_cmd = None
        self._format_shown = False
        self.b_format = tk.Button(self, text="\U0001F5D1 Format this card to use "
                                  "as a blank", command=self.step_format,
                                  bg="#dc2626", fg="#fff", relief="flat",
                                  padx=12, pady=6, font=("Segoe UI", 10, "bold"),
                                  cursor="hand2")
        # packed only when relevant (see _report)

        btns = tk.Frame(self)
        btns.pack(pady=18)
        self.b_start = tk.Button(btns, text="▶ Start scanning",
                                 command=self.start, bg="#16a34a", fg="#fff",
                                 relief="flat", padx=14, pady=7,
                                 font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.b_start.pack(side="left", padx=6)
        self.b_stop = tk.Button(btns, text="■ Stop", command=self.stop,
                                bg="#64748b", fg="#fff", relief="flat",
                                padx=14, pady=7, state="disabled",
                                font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.b_stop.pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._close)

    def start(self):
        if self.running:
            return
        self.running = True
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self._loop()

    def stop(self):
        self.running = False
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self.verdict.configure(text="Stopped", fg="#555")

    def _close(self):
        self.running = False
        self.destroy()

    def _loop(self):
        if not self.running:
            return
        self.verdict.configure(text="Scanning…", fg="#555")
        self.detail.configure(text="")

        def job():
            kind, detail = self._scan()
            self.app.after(0, lambda: self._report(kind, detail))
        self.app._async(job)

    def _scan(self):
        """Run the blank-detection checks once; return (kind, detail) or
        (None, '')."""
        run = self.app._run_client
        lf = run("lf t55xx detect")
        low = lf.lower()
        if ("t55x" in low or "downlink" in low) and "could not detect" not in low:
            return ("T5577", "LF blank - clone EM4100, HID, Indala, AWID, "
                    "FDX-B and more onto it.")
        em = run("lf em 4x05 info").lower()
        if "em4x05" in em or "em4305" in em or "em4469" in em:
            return ("EM4305", "LF writable blank - works for LF cloning.")
        mf = run("hf mf info")
        if CloneWizard._magic_ok(mf):
            return ("Magic MIFARE", "HF blank - full clone of MIFARE Classic "
                    "(UID is writable).")
        mfu = run("hf mfu info")
        if CloneWizard._magic_ok(mfu):
            return ("Magic Ultralight / NTAG",
                    "HF blank - UID-writable UL / NTAG.")
        return (None, "")

    def _report(self, kind, detail):
        self._wipe_cmd = {"T5577": "lf t55xx wipe",
                          "EM4305": "lf em 4x05 wipe",
                          "Magic MIFARE": "hf mf wipe"}.get(kind)
        if kind:
            self.verdict.configure(text="✓  " + kind + "\nUSABLE as a blank",
                                   fg="#15803d")
            self.detail.configure(
                text=detail + ("\n\nAlready has data on it? Format it below to "
                               "reuse as a blank." if self._wipe_cmd else ""))
        else:
            self.verdict.configure(
                text="✗  Not a usable blank", fg="#b91c1c")
            self.detail.configure(
                text="Either no card is on the reader, or it's a normal/locked "
                     "card whose UID can't be changed. Try another card.")
        # Show the Format button only for a writable card that can be wiped.
        if self._wipe_cmd and not self._format_shown:
            self.b_format.pack(pady=(0, 10))
            self._format_shown = True
        elif not self._wipe_cmd and self._format_shown:
            self.b_format.pack_forget()
            self._format_shown = False
        if self.running:
            self.after(1200, self._loop)

    def step_format(self):
        cmd = self._wipe_cmd
        if not cmd:
            return
        was = self.running
        self.running = False     # pause scanning while we wipe
        if not messagebox.askyesno(
                "Format to blank",
                "Erase this card so it can be reused as a blank?\n\n"
                "(" + cmd + ")", parent=self):
            if was:
                self.start()
            return
        self.verdict.configure(text="Formatting…", fg="#555")

        def done(out):
            low = out.lower()
            if "fail" in low or "error" in low:
                self.verdict.configure(text="Format had warnings - see log.",
                                       fg="#b91c1c")
            else:
                self.verdict.configure(text="✓ Formatted - ready as a blank!",
                                       fg="#15803d")
            if was:
                self.running = True
                self.after(1200, self._loop)
        self.app.run_cmd_async(cmd, done)


# ----------------------------------------------------------------------------
# Clone Wizard - guided, safe cloning
# ----------------------------------------------------------------------------
class CloneWizard(tk.Toplevel):
    """Step-by-step clone:
       1) Read the ORIGINAL card.
       2) Place the BLANK card -> check whether it can RECEIVE this clone.
       3) Only if compatible: confirm, then WRITE.
       4) VERIFY by reading the blank back.
    """

    def __init__(self, app, card):
        super().__init__(app)
        self.app = app
        self.card = card
        self.flow = self._build_flow(card)
        self.src_ok = False
        self.compatible = False
        self.written = False
        self.gen1a = False

        self.title("Clone Wizard - " + card["name"])
        self.geometry("560x520")
        self.resizable(False, False)
        self.transient(app)

        pad = dict(padx=14, pady=4)
        ttk.Label(self, text="Clone " + card["name"],
                  font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14,
                                                      pady=(12, 2))
        ttk.Label(self, text="Target (blank) needed:  " + self.flow["target"],
                  foreground="#555").pack(anchor="w", padx=14)

        # Step 1 - read original
        f1 = ttk.LabelFrame(self, text="Step 1 - read the ORIGINAL card")
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="Place the card you want to COPY on the reader.").pack(
            anchor="w", padx=10, pady=(6, 2))
        self.b_read = ttk.Button(f1, text="Read original",
                                 command=self.step_read)
        self.b_read.pack(anchor="w", padx=10, pady=(0, 6))
        self.l_read = ttk.Label(f1, text="- not read yet -", foreground="#777")
        self.l_read.pack(anchor="w", padx=10, pady=(0, 8))

        # Step 2 - check blank
        f2 = ttk.LabelFrame(self, text="Step 2 - check the BLANK (receiving) card")
        f2.pack(fill="x", **pad)
        ttk.Label(f2, text="Now remove the original and place the BLANK card.").pack(
            anchor="w", padx=10, pady=(6, 2))
        self.b_check = ttk.Button(f2, text="Check blank card",
                                  command=self.step_check, state="disabled")
        self.b_check.pack(anchor="w", padx=10, pady=(0, 6))
        self.l_check = ttk.Label(f2, text="- not checked yet -",
                                 foreground="#777")
        self.l_check.pack(anchor="w", padx=10, pady=(0, 4))

        # Prep the blank: wipe or set its UID so a leftover UID/data doesn't
        # make the write fail.
        ttk.Label(f2, text="Blank already has data or a different UID? "
                  "Clean it first:", foreground="#555").pack(anchor="w",
                                                             padx=10)
        prep = ttk.Frame(f2)
        prep.pack(anchor="w", padx=10, pady=(2, 8))
        self.b_prep_wipe = ttk.Button(prep, text="Wipe / format blank",
                                      command=self.step_prep_wipe)
        self.b_prep_wipe.pack(side="left")
        self.b_prep_uid = ttk.Button(prep, text="Set UID / ID on blank",
                                     command=self.step_prep_uid)
        self.b_prep_uid.pack(side="left", padx=6)

        # Step 3 - write
        f3 = ttk.LabelFrame(self, text="Step 3 - write the clone")
        f3.pack(fill="x", **pad)
        self.b_write = ttk.Button(f3, text="Write FULL clone (UID + data)",
                                  command=self.step_write, state="disabled")
        self.b_write.pack(anchor="w", padx=10, pady=(6, 2))
        self.b_dataonly = ttk.Button(
            f3, text="Copy DATA only (card keeps its own UID)",
            command=self.step_dataonly, state="disabled")
        self.b_dataonly.pack(anchor="w", padx=10, pady=(0, 2))
        self.l_dataonly = ttk.Label(f3, text="", foreground="#8a5a00",
                                    wraplength=500, justify="left")
        self.l_dataonly.pack(anchor="w", padx=10)
        self.l_write = ttk.Label(f3, text="- waiting -", foreground="#777")
        self.l_write.pack(anchor="w", padx=10, pady=(2, 8))

        # Step 4 - verify
        f4 = ttk.LabelFrame(self, text="Step 4 - verify the clone")
        f4.pack(fill="x", **pad)
        self.b_verify = ttk.Button(f4, text="Verify clone",
                                   command=self.step_verify, state="disabled")
        self.b_verify.pack(anchor="w", padx=10, pady=6)
        self.l_verify = ttk.Label(f4, text="- waiting -", foreground="#777")
        self.l_verify.pack(anchor="w", padx=10, pady=(0, 8))

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=8)

    # ----- flow definition per card -----
    @staticmethod
    def _magic_ok(o):
        """True if the card on the reader is a UID-writable 'magic' card."""
        o = o.lower()
        if "magic capabilities" in o and "n/a" not in o:
            return True
        return any(t in o for t in ("gen 1a", "gen1a", "gen 2", "gen2",
                                    "gen 3", "gen3", "gen 4", "gen4",
                                    "cuid", "magic backdoor"))

    def _build_flow(self, card):
        freq = card["freq"]
        if freq == "LF":
            return dict(
                target="T5577 (or EM4305) rewritable blank",
                check_cmd="lf t55xx detect",
                is_ok=lambda o: (("t55x" in o.lower() or "downlink" in o.lower())
                                 and "could not detect" not in o.lower()),
                verify_cmd=card["cmds"].get("read", "lf search"),
                data_only=None,
                prep_wipe="lf t55xx wipe")
        key = card.get("key", "")
        if key in ("mfu", "ntag"):
            return dict(target="magic (UID-writable) Ultralight/NTAG",
                        check_cmd="hf mfu info", is_ok=self._magic_ok,
                        verify_cmd="hf mfu info", data_only="hf mfu restore",
                        prep_wipe=None)
        if key == "iso15":
            return dict(target="writable ISO15693 tag",
                        check_cmd="hf 15 info",
                        is_ok=lambda o: "[+]" in o and "no tag" not in o.lower(),
                        verify_cmd="hf 15 info", data_only=None,
                        prep_wipe=None)
        # MIFARE Classic family
        return dict(
            target="magic card (Gen1a / Gen2 / CUID)",
            check_cmd="hf mf info",
            is_ok=self._magic_ok,
            verify_cmd="hf 14a info",
            data_only="hf mf restore --uid {id}",
            prep_wipe="hf mf wipe")

    def step_prep_wipe(self):
        """Wipe/format the blank on the reader so a leftover UID/data doesn't
        make the clone write fail."""
        cmd = self.flow.get("prep_wipe")
        if not cmd:
            messagebox.showinfo(
                "Wipe blank",
                "No one-tap wipe for this card type. Use 'Set UID / ID on "
                "blank', or just write the clone over it.", parent=self)
            return
        if not messagebox.askyesno(
                "Wipe blank",
                "Erase the BLANK card on the reader now?\n\n(" + cmd + ")",
                parent=self):
            return
        self.l_check.configure(text="Wiping blank ...", foreground="#555")

        def done(out):
            low = out.lower()
            if "fail" in low or "error" in low:
                self.l_check.configure(
                    text="Wipe had warnings - see log.", foreground="#c62828")
            else:
                self.l_check.configure(
                    text="✓ Blank wiped. Press 'Check blank card' again.",
                    foreground="#2e7d32")
        self.app.run_cmd_async(cmd, done)

    def step_prep_uid(self):
        """Set a UID/ID on the blank before cloning (reuses the main dialog)."""
        self.app._setuid_dialog(self.card)

    def _busy(self, btn):
        btn.configure(state="disabled")

    # ----- Step 1 -----
    def step_read(self):
        self.l_read.configure(text="Reading ...", foreground="#555")
        cmd = (self.card["cmds"].get("clone_src")
               or self.card["cmds"].get("read", "lf search"))

        def done(out):
            self.app._extract_id(out)
            got = self.app.last_id
            if got or "[+]" in out:
                self.src_ok = True
                self.l_read.configure(
                    text="Read OK" + (f" - ID/UID: {got}" if got else ""),
                    foreground="#2e7d32")
                self.b_check.configure(state="normal")
            else:
                self.l_read.configure(
                    text="Nothing read - is the original on the reader?",
                    foreground="#c62828")
        self.app.run_cmd_async(cmd, done)

    # ----- Step 2 -----
    def step_check(self):
        self.l_check.configure(text="Checking blank card ...", foreground="#555")

        def done(out):
            low = out.lower()
            self.gen1a = ("gen 1a" in low or "gen1a" in low)
            if self.flow["is_ok"](out):
                self.compatible = True
                self.l_check.configure(
                    text="✓ Compatible - this blank CAN receive the clone.",
                    foreground="#2e7d32")
                self.b_write.configure(state="normal")
            else:
                self.compatible = False
                self.b_write.configure(state="disabled")
                self.l_check.configure(
                    text="✗ Not a magic card. A full clone (UID) needs a "
                         + self.flow["target"] + ".",
                    foreground="#c62828")
                if self.flow.get("data_only"):
                    self.b_dataonly.configure(state="normal")
                    self.l_dataonly.configure(
                        text="But this card IS the same family - I can copy the "
                             "DATA sectors onto it. It will keep its OWN UID "
                             "(the UID is not cloned). Use this only if the "
                             "system doesn't check the UID.")
        self.app.run_cmd_async(self.flow["check_cmd"], done)

    # ----- Step 3 -----
    def _mfc_write_cmd(self, uid):
        """MIFARE Classic full-clone command. Gen1a uses cload (writes the
        whole dump incl. block 0/UID via the backdoor); Gen2/CUID uses restore
        with --uid so it loads the ORIGINAL's dump, not the blank's."""
        if getattr(self, "gen1a", False):
            return "hf mf cload -f hf-mf-" + uid + "-dump.bin"
        return "hf mf restore --uid " + uid

    def step_write(self):
        if not self.compatible:
            return
        if not self.app.last_id:
            val = simpledialog.askstring(
                "Clone", "No ID captured. Enter the ID/UID to write (hex):",
                parent=self)
            if not val:
                return
            self.app.last_id = val.strip()
        if self.card.get("key") == "mfc":
            write = self._mfc_write_cmd(self.app.last_id)
        else:
            write = self.card["cmds"].get("clone", "").replace(
                "{id}", self.app.last_id).replace("{uid}", self.app.last_id)

        if not messagebox.askyesno(
                "Confirm clone",
                "Write this clone now?\n\n"
                "Source: " + self.card["name"] +
                (f"\nID/UID: {self.app.last_id}" if self.app.last_id else "") +
                "\nTarget: the blank currently on the reader\n\n"
                "Command:\n  " + write,
                parent=self):
            return

        self.l_write.configure(text="Writing ...", foreground="#555")

        def done(out):
            low = out.lower()
            if "fail" in low or "error" in low or "no card" in low:
                self.l_write.configure(text="Write failed - see main log.",
                                       foreground="#c62828")
            else:
                self.written = True
                self.l_write.configure(text="✓ Clone written.",
                                       foreground="#2e7d32")
                self.b_verify.configure(state="normal")
        self.app.run_cmd_async(write, done)

    def step_dataonly(self):
        """Copy data sectors to a normal same-family card; UID is unchanged."""
        cmd = self.flow.get("data_only")
        if not cmd:
            return
        # Use the ORIGINAL card's UID so restore finds its saved dump/key files.
        cmd = cmd.replace("{id}", self.app.last_id or "").replace(
            "{uid}", self.app.last_id or "")
        if not messagebox.askyesno(
                "Copy data only",
                "This copies the DATA onto the card, but the card KEEPS its "
                "own UID - the UID is NOT cloned (a normal card's UID is "
                "locked).\n\nThis only works if you have the card's keys "
                "(run Read / autopwn on the original first) and only helps if "
                "the system does not check the UID.\n\nContinue?", parent=self):
            return
        self.l_write.configure(text="Writing data (UID unchanged) ...",
                               foreground="#555")

        def done(out):
            low = out.lower()
            if "fail" in low or "error" in low or "no card" in low:
                self.l_write.configure(
                    text="Data write had errors - see log (block 0 / UID "
                         "cannot change on a normal card).",
                    foreground="#c62828")
            else:
                self.written = True
                self.l_write.configure(
                    text="✓ Data copied. The card kept its own UID.",
                    foreground="#2e7d32")
                self.b_verify.configure(state="normal")
        self.app.run_cmd_async(cmd, done)

    # ----- Step 4 -----
    def step_verify(self):
        self.l_verify.configure(text="Verifying ...", foreground="#555")
        expect = self.app.last_id

        def done(out):
            ok = ("[+]" in out) and (
                not expect or expect.lower() in out.lower().replace(" ", ""))
            if ok:
                self.l_verify.configure(
                    text="✓ Verified - the blank now reads as the clone.",
                    foreground="#2e7d32")
                messagebox.showinfo("Clone complete",
                    "Clone complete and verified.", parent=self)
            else:
                self.l_verify.configure(
                    text="Could not confirm - compare the log output.",
                    foreground="#c62828")
        self.app.run_cmd_async(self.flow["verify_cmd"], done)


if __name__ == "__main__":
    app = ProxmarkGUI()
    app.mainloop()
