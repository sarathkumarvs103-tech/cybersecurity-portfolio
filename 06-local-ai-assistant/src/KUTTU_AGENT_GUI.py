#!/usr/bin/env python3
"""
KUTTU Agent - a local desktop AI agent with a GUI.

- No API key. Talks to YOUR local model via OpenAI-compatible servers.
- Model-server dropdown AND Agent dropdown: both saved to kuttu_agent_config.json,
  so you can add your own servers and agent roles and they stay in the list.
- Detect servers scans all saved servers. Works with files; you approve writes/commands.

Everything runs on this machine. Nothing is sent to any cloud service.
"""

import json
import os
import subprocess
import threading
import queue
import urllib.request
import urllib.error

import tkinter as tk
from tkinter import scrolledtext, filedialog, ttk, simpledialog, messagebox

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kuttu_agent_config.json")

DEFAULT_SERVERS = {
    "LM Studio": "http://127.0.0.1:1234/v1",
    "Ollama":    "http://127.0.0.1:11434/v1",
    "llama.cpp": "http://127.0.0.1:8080/v1",
}

DEFAULT_AGENTS = {
    "General Assistant":
        "You are KUTTU Agent, a helpful general assistant running locally on the user's Windows PC.",
    "File Analyst":
        "You are KUTTU Agent in File Analyst mode. You specialise in exploring folders, reading and "
        "summarising documents, and finding things across files on this PC. Prefer list_dir, read_file "
        "and search before answering.",
    "Coding Helper":
        "You are KUTTU Agent in Coding Helper mode. You read code files, explain them, and help write or "
        "fix code. Show changes clearly and ask approval before writing files or running commands.",
    "System Helper":
        "You are KUTTU Agent in System Helper mode. You help inspect and organise this Windows PC. "
        "Always ask approval before running commands, and prefer safe, read-only checks first.",
}

TOOL_PROTOCOL = """You can use tools to work with files and the system. You MUST reply with a SINGLE JSON object and nothing else.

Available actions:
{"action":"list_dir","path":"<folder>"}            - list the files in a folder
{"action":"read_file","path":"<file>"}             - read a text file (first part)
{"action":"search","path":"<folder>","query":"x"}  - find text inside files under a folder
{"action":"write_file","path":"<file>","content":"..."}  - create/overwrite a file (needs user approval)
{"action":"run_cmd","command":"<cmd>"}             - run a Windows command (needs user approval)
{"action":"final","message":"<your answer to the user>"}  - when you are done, give the answer

Rules:
- Think step by step: use read/list/search actions to gather what you need, THEN give a "final" answer.
- Use "final" for normal conversation or when you have finished the task.
- Only ONE JSON object per reply. No markdown, no code fences, no extra text.
- Paths are Windows paths. The current working folder is provided to you."""

MAX_READ_BYTES = 12000


def load_config():
    cfg = {"servers": dict(DEFAULT_SERVERS), "agents": dict(DEFAULT_AGENTS)}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("servers"), dict) and data["servers"]:
                cfg["servers"] = data["servers"]
            if isinstance(data.get("agents"), dict) and data["agents"]:
                cfg["agents"] = data["agents"]
        except Exception:
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


class Agent:
    def __init__(self, api_base, model, workdir, system_prompt):
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.workdir = workdir
        self.messages = [{"role": "system", "content": system_prompt}]

    def set_system(self, system_prompt):
        self.messages = [{"role": "system", "content": system_prompt}]

    def _resolve(self, path):
        if not path:
            return self.workdir
        if os.path.isabs(path):
            return path
        return os.path.join(self.workdir, path)

    def chat(self):
        url = self.api_base + "/chat/completions"
        payload = {"model": self.model, "messages": self.messages,
                   "temperature": 0.3, "stream": False}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer not-needed"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return out["choices"][0]["message"]["content"]

    def do_list_dir(self, path):
        p = self._resolve(path)
        try:
            items = os.listdir(p)
        except Exception as e:
            return f"ERROR listing {p}: {e}"
        lines = []
        for name in sorted(items)[:300]:
            full = os.path.join(p, name)
            tag = "<DIR>" if os.path.isdir(full) else str(os.path.getsize(full)) + " B"
            lines.append(f"{tag}\t{name}")
        return f"Folder {p} ({len(items)} items):\n" + "\n".join(lines)

    def do_read_file(self, path):
        p = self._resolve(path)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(MAX_READ_BYTES)
        except Exception as e:
            return f"ERROR reading {p}: {e}"
        more = "\n...[truncated]..." if len(data) >= MAX_READ_BYTES else ""
        return f"Contents of {p}:\n{data}{more}"

    def do_search(self, path, query):
        root = self._resolve(path)
        hits = []
        for dirpath, _, files in os.walk(root):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if query.lower() in line.lower():
                                hits.append(f"{fp}:{i}: {line.strip()[:160]}")
                                if len(hits) >= 100:
                                    return "Search results:\n" + "\n".join(hits)
                except Exception:
                    pass
        return "Search results:\n" + ("\n".join(hits) if hits else "(no matches)")

    def do_write_file(self, path, content):
        p = self._resolve(path)
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"ERROR writing {p}: {e}"
        return f"Wrote {len(content)} chars to {p}"

    def do_run_cmd(self, command):
        try:
            r = subprocess.run(command, shell=True, cwd=self.workdir,
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            return f"ERROR running command: {e}"
        return f"exit={r.returncode}\nSTDOUT:\n{r.stdout[:4000]}\nSTDERR:\n{r.stderr[:2000]}"


def parse_action(text):
    text = text.strip()
    start = text.find("{")
    if start == -1:
        return {"action": "final", "message": text}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start:i + 1]
                try:
                    return json.loads(chunk)
                except Exception:
                    break
    return {"action": "final", "message": text}


class App:
    def __init__(self, root):
        self.root = root
        root.title("KUTTU Agent - local, no API key")
        root.geometry("1000x740")

        self.cfg = load_config()
        self.servers = self.cfg["servers"]
        self.agents = self.cfg["agents"]

        self.q = queue.Queue()
        self.agent = None
        self.pending = None

        cfg = ttk.Frame(root, padding=6)
        cfg.pack(fill="x")

        # Row 0: Agent dropdown (+ add)
        ttk.Label(cfg, text="Agent:").grid(row=0, column=0, sticky="w")
        self.agent_box = ttk.Combobox(cfg, values=list(self.agents.keys()),
                                      width=20, state="readonly")
        self.agent_box.set(next(iter(self.agents)))
        self.agent_box.grid(row=0, column=1, padx=4)
        self.agent_box.bind("<<ComboboxSelected>>", self.on_agent_change)
        ttk.Button(cfg, text="+ Add agent", command=self.add_agent).grid(row=0, column=2, padx=2)

        # Row 1: Model server dropdown (editable) + url + buttons
        ttk.Label(cfg, text="Model server:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.server_box = ttk.Combobox(cfg, values=list(self.servers.keys()), width=20)
        self.server_box.set(next(iter(self.servers)))
        self.server_box.grid(row=1, column=1, padx=4, pady=(4, 0))
        self.server_box.bind("<<ComboboxSelected>>", self.on_server_pick)

        self.api_var = tk.StringVar(value=self.servers[next(iter(self.servers))])
        ttk.Entry(cfg, textvariable=self.api_var, width=30).grid(row=1, column=2, padx=4, pady=(4, 0))
        ttk.Button(cfg, text="Save server", command=self.save_server).grid(row=1, column=3, padx=2, pady=(4, 0))
        ttk.Button(cfg, text="Detect servers", command=self.detect_servers).grid(row=1, column=4, padx=2, pady=(4, 0))

        # Row 2: Model
        ttk.Label(cfg, text="Model:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.model_var = tk.StringVar(value="local-model")
        self.model_box = ttk.Combobox(cfg, textvariable=self.model_var, width=44)
        self.model_box.grid(row=2, column=1, columnspan=2, sticky="w", padx=4, pady=(4, 0))
        ttk.Button(cfg, text="Load models", command=self.load_models).grid(row=2, column=3, columnspan=2, sticky="w", pady=(4, 0))

        wd = ttk.Frame(root, padding=(6, 0))
        wd.pack(fill="x")
        ttk.Label(wd, text="Working folder:").pack(side="left")
        self.workdir_var = tk.StringVar(value=os.path.dirname(os.path.abspath(__file__)))
        ttk.Entry(wd, textvariable=self.workdir_var, width=66).pack(side="left", padx=4)
        ttk.Button(wd, text="Browse", command=self.browse).pack(side="left")

        self.log = scrolledtext.ScrolledText(root, wrap="word", height=24, state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_config("you", foreground="#1a6")
        self.log.tag_config("agent", foreground="#06c")
        self.log.tag_config("tool", foreground="#888")
        self.log.tag_config("warn", foreground="#c40")

        self.approve_bar = ttk.Frame(root, padding=6)
        self.approve_lbl = ttk.Label(self.approve_bar, text="", foreground="#c40", wraplength=800)
        self.approve_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(self.approve_bar, text="Approve", command=lambda: self.resolve_pending(True)).pack(side="left", padx=4)
        ttk.Button(self.approve_bar, text="Skip", command=lambda: self.resolve_pending(False)).pack(side="left")

        bottom = ttk.Frame(root, padding=6)
        bottom.pack(fill="x")
        self.inp = tk.Text(bottom, height=3, wrap="word")
        self.inp.pack(side="left", fill="x", expand=True)
        self.inp.bind("<Control-Return>", lambda e: self.send())
        ttk.Button(bottom, text="Send", command=self.send).pack(side="left", padx=6)

        self.write_log("KUTTU Agent ready. Pick an Agent and a Model server (or add your own), "
                       "click Detect servers or Load models, choose the model, then type a task.\n"
                       "Servers and agents you add are saved in kuttu_agent_config.json.\n", "tool")
        self.root.after(100, self.pump)

    # ---------- config-driven helpers ----------
    def build_system(self, name):
        role = self.agents.get(name, next(iter(self.agents.values())))
        return role + "\n\n" + TOOL_PROTOCOL

    def on_server_pick(self, _):
        name = self.server_box.get()
        if name in self.servers:
            self.api_var.set(self.servers[name])

    def save_server(self):
        name = self.server_box.get().strip()
        url = self.api_var.get().strip()
        if not name or not url:
            messagebox.showwarning("Save server", "Enter a server name and a URL first.")
            return
        self.servers[name] = url
        self.cfg["servers"] = self.servers
        save_config(self.cfg)
        self.server_box["values"] = list(self.servers.keys())
        self.write_log(f"[saved server: {name} -> {url}]", "tool")

    def add_agent(self):
        name = simpledialog.askstring("Add agent", "Agent name:")
        if not name:
            return
        desc = simpledialog.askstring("Add agent", f"Describe what '{name}' should do\n(its role / instructions):")
        if not desc:
            return
        self.agents[name] = desc
        self.cfg["agents"] = self.agents
        save_config(self.cfg)
        self.agent_box["values"] = list(self.agents.keys())
        self.agent_box.set(name)
        self.on_agent_change(None)
        self.write_log(f"[added agent: {name}]", "tool")

    def on_agent_change(self, _):
        name = self.agent_box.get()
        if self.agent:
            self.agent.set_system(self.build_system(name))
        self.write_log(f"[agent: {name} - conversation reset]", "tool")

    def browse(self):
        d = filedialog.askdirectory()
        if d:
            self.workdir_var.set(d)

    def write_log(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def detect_servers(self):
        self.write_log("Scanning your saved model servers...", "tool")
        servers = dict(self.servers)
        def worker():
            found = []
            for name, base in servers.items():
                try:
                    with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=3) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    ids = [m["id"] for m in data.get("data", [])]
                    found.append((name, base, ids))
                except Exception:
                    pass
            self.q.put(("detect", found))
        threading.Thread(target=worker, daemon=True).start()

    def load_models(self):
        base = self.api_var.get().rstrip("/")
        def worker():
            try:
                with urllib.request.urlopen(base + "/models", timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ids = [m["id"] for m in data.get("data", [])]
                self.q.put(("models", ids))
            except Exception as e:
                self.q.put(("error", f"Could not reach model server at {base}: {e}\n"
                                     "Start the server (LM Studio local server / Ollama) first."))
        threading.Thread(target=worker, daemon=True).start()

    def send(self):
        if self.pending:
            self.write_log("Please Approve or Skip the pending action first.", "warn")
            return "break"
        text = self.inp.get("1.0", "end").strip()
        if not text:
            return "break"
        self.inp.delete("1.0", "end")
        self.write_log("You: " + text, "you")
        if self.agent is None:
            self.agent = Agent(self.api_var.get(), self.model_var.get(),
                               self.workdir_var.get(), self.build_system(self.agent_box.get()))
        self.agent.api_base = self.api_var.get().rstrip("/")
        self.agent.model = self.model_var.get()
        self.agent.workdir = self.workdir_var.get()
        self.agent.messages.append({"role": "user", "content":
                                    f"(current working folder: {self.agent.workdir})\n{text}"})
        self.run_step()
        return "break"

    def run_step(self):
        def worker():
            try:
                reply = self.agent.chat()
                self.q.put(("reply", reply))
            except urllib.error.URLError as e:
                self.q.put(("error", f"Cannot reach the model server: {e}\n"
                                     "Is the model server running?"))
            except Exception as e:
                self.q.put(("error", f"Error: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def handle_reply(self, reply):
        self.agent.messages.append({"role": "assistant", "content": reply})
        act = parse_action(reply)
        action = act.get("action", "final")
        if action == "final":
            self.write_log("Agent: " + act.get("message", reply), "agent")
            return
        if action in ("write_file", "run_cmd"):
            if action == "write_file":
                desc = f"Agent wants to WRITE file: {act.get('path')}\n--- content ---\n{act.get('content','')[:800]}"
            else:
                desc = f"Agent wants to RUN command:\n{act.get('command')}"
            self.pending = act
            self.approve_lbl.config(text=desc)
            self.approve_bar.pack(fill="x")
            self.write_log(f"[approval needed] {action}", "warn")
            return
        self.write_log(f"[tool] {action} {act.get('path','')} {act.get('query','')}".rstrip(), "tool")
        self.feed_result(self.exec_action(act))

    def exec_action(self, act):
        a = act.get("action")
        if a == "list_dir":
            return self.agent.do_list_dir(act.get("path", ""))
        if a == "read_file":
            return self.agent.do_read_file(act.get("path", ""))
        if a == "search":
            return self.agent.do_search(act.get("path", ""), act.get("query", ""))
        if a == "write_file":
            return self.agent.do_write_file(act.get("path", ""), act.get("content", ""))
        if a == "run_cmd":
            return self.agent.do_run_cmd(act.get("command", ""))
        return f"Unknown action: {a}"

    def resolve_pending(self, approved):
        act = self.pending
        self.pending = None
        self.approve_bar.pack_forget()
        if not act:
            return
        if approved:
            self.write_log(f"[approved] {act.get('action')}", "tool")
            result = self.exec_action(act)
        else:
            result = f"User skipped the {act.get('action')} action. Do not retry it; continue or give a final answer."
            self.write_log("[skipped]", "warn")
        self.feed_result(result)

    def feed_result(self, result):
        self.write_log(result[:1500] + ("..." if len(result) > 1500 else ""), "tool")
        self.agent.messages.append({"role": "user", "content": "Tool result:\n" + result})
        self.run_step()

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "reply":
                    self.handle_reply(payload)
                elif kind == "detect":
                    if payload:
                        for name, base, ids in payload:
                            self.write_log(f"Found: {name} at {base} ({len(ids)} model(s))", "tool")
                        name, base, ids = payload[0]
                        self.server_box.set(name)
                        self.api_var.set(base)
                        if ids:
                            self.model_box["values"] = ids
                            self.model_var.set(ids[0])
                        self.write_log(f"Using {name}. Model: {self.model_var.get()}", "tool")
                    else:
                        self.write_log("No saved server is running. Start one, or add it with Save server, then Detect again.", "warn")
                elif kind == "models":
                    if payload:
                        self.model_box["values"] = payload
                        self.model_var.set(payload[0])
                        self.write_log("Models found: " + ", ".join(payload), "tool")
                    else:
                        self.write_log("Server reachable but no models loaded.", "warn")
                elif kind == "error":
                    self.write_log(payload, "warn")
        except queue.Empty:
            pass
        self.root.after(120, self.pump)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
