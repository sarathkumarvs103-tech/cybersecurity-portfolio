# 02 – RFID Security Analysis with Proxmark3 (11/2026)

Security testing of RFID/NFC access cards – directly linked to my previous work running electronic locking and access-control systems at Frankfurt Airport.

## What I did
- Read, cloned and emulated RFID/NFC cards with a **Proxmark3 Easy** (Iceman / RRG firmware)
- Built my own **Windows GUI in Python (Tkinter)** that wraps the Proxmark3 client, so common commands can be run with one click

## Why it matters
Many access-control systems still use cards that can be copied. Understanding this helps to assess and harden physical access security.

## Skills shown
RFID/NFC · physical security · Python · tool development

## Code
- [`src/proxmark3_gui.py`](src/proxmark3_gui.py) – Windows GUI (Python 3, Tkinter): card catalogue for LF/HF card families, auto-detect (`lf search` / `hf search`), read, clone, edit and format buttons enabled per card type, raw command console
- [`src/SETUP.txt`](src/SETUP.txt) – installation and usage guide

Run: `python src/proxmark3_gui.py` (optional: `pip install pyserial` for automatic COM-port detection)
