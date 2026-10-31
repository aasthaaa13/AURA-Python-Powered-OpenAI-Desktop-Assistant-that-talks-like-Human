# AURA — AI-Powered Desktop Assistant

<p align="center">
  <strong>Your Desktop. Your Commands. Your Intelligent Assistant.</strong>
</p>

<p align="center">
  A Python-powered desktop assistant featuring a futuristic HUD interface,
  voice interaction, AI engine integration, and real-time system monitoring.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.x-blue?logo=python"/>
  <img src="https://img.shields.io/badge/GUI-PySide6-green?logo=qt"/>
  <img src="https://img.shields.io/badge/Platform-Desktop-lightgrey"/>
  <img src="https://img.shields.io/badge/Status-Development-orange"/>
  <img src="https://img.shields.io/badge/License-MIT-blue"/>
</p>

`🌌 Explore` 
`🎙️ Audio Input`
`🗣️ Speech Engine`
`✨ Discover`
`🚀 Activate`
`↗️ Initialize`
`💬 Command Deck`
`🤖 Enter Aura`

---

##  Overview

**AURA** is an AI-powered desktop assistant developed using Python and
PySide6. It is designed to provide an interactive desktop experience
through voice-based communication, AI-powered responses, system
monitoring, and local command execution.

Inspired by modern AI assistants and futuristic heads-up displays,
AURA combines an animated orb interface with a modular software
architecture to create a visually engaging and functional desktop
assistant.

The application aims to make everyday desktop interactions more
convenient by combining artificial intelligence, speech processing,
and system-level information in one interface.

Rather than relying on a traditional chatbot layout, AURA introduces
a HUD-inspired interface that provides a more immersive way to
interact with a desktop assistant.

##  Key Features

### 2.1 Futuristic HUD Interface

- Modern dark-themed desktop interface.
- Animated assistant orb.
- Interactive graphical components.
- Visual feedback for assistant activity.
- Main and compact interface components.

### 2.2 Voice Interaction

- Speech recognition for voice input.
- Microphone-based audio capture.
- Speech-based interaction.
- Audio playback support.
- Text-to-speech integration where configured.

### 2.3 AI Engine Integration

- Modular AI engine architecture.
- OpenAI integration.
- Anthropic Claude integration.
- Support for configuring different AI backends.
- Separation of AI processing from the user interface.

*AI provider functionality depends on configuration and valid API
credentials.*

### 2.4 System Monitoring

AURA includes system monitoring capabilities through `psutil`.

The monitoring module is designed to retrieve information such as:

- CPU utilization.
- Memory usage.
- Disk information.
- Battery status.
- Network statistics.

### 2.5 Desktop Command Execution

- Local command processing.
- Application launching through supported commands.
- Command validation.
- Allow-listed command execution.
- Separation of command processing and execution logic.

### 2.6 Modular Architecture

The application is divided into independent modules for:

- User interface management.
- AI processing.
- Voice interaction.
- Command handling.
- System monitoring.
- Configuration management.

This structure makes the project easier to understand, maintain,
debug, and extend.

---

##  Technology Stack

| Technology | Purpose |
|---|---|
| Python | Core application development |
| PySide6 | Desktop graphical interface |
| NumPy | Numerical operations |
| psutil | System monitoring |
| SpeechRecognition | Speech-to-text processing |
| sounddevice | Audio input and playback |
| OpenAI SDK | OpenAI AI engine integration |
| Anthropic SDK | Claude AI engine integration |
| ElevenLabs SDK | Optional voice integration |
| python-dotenv | Environment variable management |

---

##  Project Architecture

AURA follows a modular architecture in which different components
handle specific responsibilities.

### Directory Structure

```text
AURA/
│
├── assets/
│   ├── README.md
│   └── screenshot.png
│
├── assistant/
│   ├── __init__.py
│   ├── ai_engine.py
│   ├── assistant_manager.py
│   ├── commands.py
│   ├── hotkey.py
│   └── speech.py
│
├── system/
│   ├── __init__.py
│   ├── system_info.py
│   └── system_monitor.py
│
├── ui/
│   ├── __init__.py
│   ├── animations.py
│   ├── hud.py
│   ├── main_window.py
│   ├── mini_window.py
│   ├── orb.py
│   └── widgets.py
│
├── .env.example
├── .gitignore
├── aura.py
├── config.py
├── main.py
├── README.md
└── requirements.txt
```
---

## Installation and Setup
Follow these steps to run AURA on your local machine.

### Prerequisites
Before installing the application, make sure you have:
- Python installed.
- Git installed.
- Visual Studio Code or another Python-compatible editor.
- A working microphone for voice input.
- An internet connection for online speech recognition and
- configured cloud AI services.

---


