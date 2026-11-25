# 06 – Local AI Assistant without Cloud (10/2026 – today)

## What I did
- Run large language models fully **offline** with **llama.cpp** and **Ollama** – no data sent to cloud services
- Built my own **Python agent** with a desktop GUI that connects to local model servers
- The agent must ask for **approval before every write or execute action** (least-privilege principle)

## Skills shown
Python · local LLM operation · data protection by design · AI governance (ISO/IEC 42001)

## Code
- [`src/KUTTU_AGENT_GUI.py`](src/KUTTU_AGENT_GUI.py) – Tkinter desktop agent for local OpenAI-compatible model servers (LM Studio, Ollama, llama.cpp); selectable agent roles; file tools; **Approve / Skip** bar before any file write or command
- [`src/kuttu_agent_config.json`](src/kuttu_agent_config.json) – local server endpoints (127.0.0.1 only) and agent roles
- [`src/KUTTU_AGENT.bat`](src/KUTTU_AGENT.bat) – launcher
