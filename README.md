# Kitti - Voice-Enabled Personal Assistant

Kitti is an intelligent, real-time, voice-activated personal assistant. It combines cutting-edge client-side voice activity detection (VAD), local high-speed speech-to-text (STT), a Claude-powered LLM reasoning engine with dynamic tool execution (agentic file/office automation), and high-quality cloud text-to-speech (TTS) with seamless barge-in support.

---

## Key Features

*   **Real-time Voice Interaction:** Persistent WebSocket connections allow for minimal latency.
*   **Local Voice Activity Detection (VAD):** Employs `@ricky0123/vad-web` running Silero VAD locally via ONNX Runtime Web in the browser, reducing unnecessary network usage.
*   **Barge-In (Interruption) Support:** Speak at any time to immediately interrupt Kitti's response. The frontend pauses playback and the backend cancels active generations instantly.
*   **Agentic Tool Execution (`run_code`):** Kitti does not just chat—she can execute Python code locally on your machine to automate tasks:
    *   **Document Generation:** Create Word docs (`.docx`), Excel spreadsheets (`.xlsx`), PowerPoint presentations (`.pptx`), and PDFs.
    *   **File Management:** Move, rename, copy, or delete files directly in your `~/Desktop` directory.
    *   **COM Automation:** Control local Microsoft Office applications on Windows dynamically using `win32com`.
    *   **Self-Correction:** If Kitti's generated code encounters an error, the traceback is fed back to the model for automated self-correction (up to 5 attempts).
*   **Persistent & Condensed Memory:** Tracks conversation summaries and history in a local JSON database. Automatically summarizes older conversations to keep prompt context windows clean and context-aware.

---
---

## Tech Stack

### Frontend
*   **Core Framework:** React.js (v19)
*   **Styling:** Tailwind CSS (v3) for a modern, glowing dark-theme user interface
*   **VAD:** `@ricky0123/vad-web` + `onnxruntime-web` (Client-side Voice Activity Detection via WebAssembly)
*   **Protocol:** WebSockets for bidirectional raw audio and event transmission

### Backend
*   **Framework:** FastAPI (Python 3.10+) & Uvicorn for asynchronous networking
*   **Speech-to-Text (STT):** `faster-whisper` (utilizing the `large-v3-turbo` model in CPU int8 mode)
*   **Text-to-Speech (TTS):** `edge-tts` (Microsoft Edge's TTS Service - `en-US-JennyNeural`)
*   **LLM Brain:** Anthropic Claude API (`anthropic` library)
*   **Office Automation Libraries:**
    *   `python-docx` (Word Documents)
    *   `openpyxl` (Excel Spreadsheets)
    *   `python-pptx` (PowerPoint Presentations)
    *   `reportlab` (PDF generation)
    *   `pywin32` / `win32com` / `pythoncom` (Windows COM Automation for Microsoft Office)

---

## Installation & Setup

### 1. Prerequisites
*   Python 3.10 or higher
*   Node.js (v18 or higher) & npm
*   Microsoft Office installed (optional, required only for win32com PDF conversions)
*   An Anthropic API Key

### 2. Backend Setup
1.  Navigate to the `backend` directory:
    ```bash
    cd backend
    ```
2.  Create a virtual environment and activate it:
    ```bash
    python -m venv venv
    # On Windows:
    .\venv\Scripts\activate
    # On macOS/Linux:
    source venv/bin/activate
    ```
3.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
4.  Create a `.env` file in the `backend` directory:
    ```env
    ANTHROPIC_API_KEY=your_anthropic_api_key_here
    LLM_MODEL=claude-3-5-sonnet-latest
    ```
5.  Start the FastAPI backend server:
    ```bash
    uvicorn main:app --reload --port 8000
    ```

### 3. Frontend Setup
1.  Navigate to the `frontend` directory:
    ```bash
    cd ../frontend
    ```
2.  Install dependencies:
    ```bash
    npm install
    ```
3.  Start the React application:
    ```bash
    npm start
    ```
4.  Open [http://localhost:3000](http://localhost:3000) in your web browser. Grant microphone access when prompted.

---
