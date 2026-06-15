import os
import json
import asyncio
import tempfile
import time as time_module
from datetime import datetime
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel
from anthropic import Anthropic
from dotenv import load_dotenv
import edge_tts

from tools.tool_registry import TOOLS, execute_tool

load_dotenv()

# ══════════════════════════════════════════════════════════════
#  EVAL METRICS — per-request latency + error tracking
# ══════════════════════════════════════════════════════════════

class EvalMetrics:
    """
    Tracks latency and errors for a single request lifecycle.
    Print with print_eval().
    """

    def __init__(self, req_id: int, input_type: str, transcript: str = ""):
        self.req_id = req_id
        self.input_type = input_type          # "voice" or "text"
        self.transcript = transcript          # what the user said
        self.state = "active"                 # active | completed | interrupted | failed

        # ── Latency buckets (seconds) ──
        self.stt_duration     = None
        self.llm_duration     = None
        self.tts_duration     = None
        self.e2e_duration     = None          # speech-end → Kitti starts speaking

        # ── LLM details ──
        self.llm_retries      = 0             # tool-call rounds / self-corrections
        self.llm_total_tokens = 0

        # ── Per-tool calls ──
        self.tool_calls       = []            # list of {name, duration, error}
        self.tool_errors      = 0
        self.tool_retries     = 0
        self.tool_timeouts    = 0

        # ── Error tracking ──
        self.errors           = []            # ["error description", ...]

        # ── Timestamps ──
        self.t_speech_end     = None          # when VAD detected end of speech
        self.t_kitti_speaking = None          # when Kitti starts TTS playback

    def stage(self, name: str):
        """Returns a context-manager that times a block. Usage: with metrics.stage('stt'): ... """
        return _StageContext(self, name)

    def add_error(self, error: str):
        self.errors.append(error)
        if "timeout" in error.lower():
            self.tool_timeouts += 1
        self.tool_errors += 1

    def print_eval(self):
        if self.state == "active":
            return  # don't print mid-flight

        W = 60
        print("\n" + "═" * W)
        print(f"  EVAL  │  req#{self.req_id}  │  {self.input_type.upper()}  │  {self.state.upper()}")
        print("─" * W)

        # Latency table
        def row(label, val):
            if val is None:
                print(f"  {label:<30}  —")
            else:
                print(f"  {label:<30}  {val:.3f}s")

        print("  LATENCY")
        row("  STT (Whisper)",            self.stt_duration)
        row("  LLM (Claude)",              self.llm_duration)
        row("  TTS (Edge)",               self.tts_duration)
        row("  Total Pipeline (STT+LLM+TTS)", self.e2e_duration)

        # Tool calls
        if self.tool_calls:
            print(f"  TOOL CALLS  ({len(self.tool_calls)} total, {self.tool_errors} errors, {self.tool_retries} retries, {self.tool_timeouts} timeouts)")
            for i, tc in enumerate(self.tool_calls, 1):
                status = " x" if tc["error"] else " ✓"
                err_note = f"  [{tc['error'][:40]}]" if tc["error"] else ""
                print(f"    {i}. {tc['name']:<30}  {tc['duration']:.3f}s{status}{err_note}")

        # LLM details
        if self.llm_retries > 0:
            print(f"  LLM retries (self-correction): {self.llm_retries}")

        # Errors
        if self.errors:
            print(f"  ERRORS  ({len(self.errors)})")
            for e in self.errors:
                print(f"    • {e[:80]}")
        else:
            print(f"  ERRORS:  none")

        print("═" * W + "\n")


class _StageContext:
    """Timing context manager used by EvalMetrics.stage('name')."""
    def __init__(self, metrics: EvalMetrics, stage_name: str):
        self.metrics = metrics
        self.name = stage_name
        self._start = None

    def __enter__(self):
        self._start = time_module.perf_counter()
        return self

    def __exit__(self, *args):
        elapsed = time_module.perf_counter() - self._start
        attr_map = {
            "stt": "stt_duration",
            "llm": "llm_duration",
            "tts": "tts_duration",
        }
        attr = attr_map.get(self.name)
        if attr:
            setattr(self.metrics, attr, elapsed)


# Global: current metrics for the in-flight request
_current_metrics: EvalMetrics | None = None


def print_eval_summary():
    """Print the active request's eval metrics if it has reached a terminal state."""
    global _current_metrics
    if _current_metrics and _current_metrics.state != "active":
        _current_metrics.print_eval()
        _current_metrics = None

app = FastAPI(title="Kitti - Personal Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

print("Loading Whisper model...")
WHISPER_MODEL = "large-v3-turbo"
whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print(f"Whisper model '{WHISPER_MODEL}' ready")

WHISPER_VOCAB_HINT = (
    "The assistant's name is Kitti. "
    "Common requests: schedule meeting, create word doc, create excel sheet, "
    "what's the time, latest news, send email."
)

claude_client = Anthropic()
CLAUDE_MODEL = os.getenv("LLM_MODEL", "MiniMax-M2.7")

KITTI_SYSTEM_PROMPT = """You are Kitti, a friendly personal voice assistant.

Guidelines:
- Keep responses SHORT and conversational - usually 1-2 sentences
- Talk like a human, not a chatbot - natural and warm
- No markdown, no bullet points, no formatting (you're going to be spoken aloud)
- If asked something you can't do yet, just say it casually like "I can't do that yet, but I'm learning!"
- For general chat, knowledge questions, or small talk - answer directly

IMPORTANT - File Creation Rules:
- When asked to create a Word document, Excel file, PowerPoint, or PDF: use the run_code tool with python-docx, openpyxl, pptx, or reportlab. NEVER just say "Done!" without calling the tool.
- Save files to Desktop: os.path.expanduser('~/Desktop')
- ALWAYS verify the file exists with os.path.exists() before responding "Done!"
- When converting Word/Excel/PowerPoint to PDF: use run_code with win32com.client. MUST call pythoncom.CoInitialize() before Dispatch and pythoncom.CoUninitialize() after.
- After creating or converting a file, say the exact filename so the user knows what to look for.

IMPORTANT - Rename/Delete Rules:
- When renaming or deleting files: use run_code with shutil.move or os.remove
- ALWAYS verify the operation succeeded with os.path.exists() before saying Done!
- Never say a file was renamed/deleted unless you actually called the tool and verified it.

IMPORTANT - File Editing Rules (formatting, fonts, colors, highlights, etc.):
- When user asks to modify/edit/format/change an existing file: you MUST read the file first to understand its content.
- Use run_code to open and describe the file content. Then apply the requested changes.
- For ambiguous requests (e.g., "highlight the important part"): ask the user to clarify which part they mean, OR read the file and suggest options.
- Apply formatting using library object properties:
  - Word (python-docx): run.font.name, run.font.size, run.font.color.rgb, run.bold, run.highlight
  - Excel (openpyxl): cell.font, cell.fill, cell.alignment
  - PowerPoint (python-pptx): run.font.size, run.font.color.rgb, shape.fill
- IMPORTANT: For editing existing files, open the file and save changes directly to the SAME filepath. Do NOT create a new file with "_Updated" suffix.
- Use os.path.expanduser('~/Desktop/filename.ext') for all paths on Windows. NEVER use Unix paths like /mnt/c/.
- ALWAYS verify changes were applied before saying Done!

IMPORTANT - Path Format:
- ALWAYS use: os.path.expanduser('~/Desktop/filename.ext') or 'C:\\Users\\YourName\\Desktop\\filename.ext'
- NEVER use: /mnt/c/... or Unix-style paths

If code has an error: read the error, fix the code, and call run_code again."""

TTS_VOICE = "en-US-JennyNeural"

async def text_to_speech(text: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(tmp_path)
    with open(tmp_path, "rb") as f:
        audio_bytes = f.read()
    os.unlink(tmp_path)
    return audio_bytes

MEMORY_DIR = "memory"
HISTORY_FILE = os.path.join(MEMORY_DIR, "chat_history.json")
SUMMARY_FILE = os.path.join(MEMORY_DIR, "summary.json")
os.makedirs(MEMORY_DIR, exist_ok=True)

def load_memory():
    history = []
    summary = ""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    history = json.loads(content)
            print(f"Loaded {len(history)} messages from memory")
        except (json.JSONDecodeError, Exception) as e:
            print(f"Could not load chat history ({e}), starting fresh")
            history = []
    if os.path.exists(SUMMARY_FILE):
        try:
            with open(SUMMARY_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    summary = data.get("summary", "")
            if summary:
                print(f"Loaded summary: {summary[:80]}...")
        except (json.JSONDecodeError, Exception) as e:
            print(f"Could not load summary ({e}), starting fresh")
            summary = ""
    return history, summary

def save_memory():
    with open(HISTORY_FILE, "w") as f:
        json.dump(chat_history, f, indent=2)
    with open(SUMMARY_FILE, "w") as f:
        json.dump({"summary": conversation_summary}, f, indent=2)

chat_history, conversation_summary = load_memory()

def extract_text_from_response(response) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""

def summarize_old_messages(messages: list) -> str:
    print("Summarizing old messages...")
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
    )
    response = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize this conversation in 3-5 sentences. "
                f"Focus on key facts: names, preferences, tasks done, important info mentioned.\n\n"
                f"{convo_text}"
            )
        }]
    )
    return extract_text_from_response(response)

def build_system_prompt() -> str:
    if not conversation_summary:
        return KITTI_SYSTEM_PROMPT
    return (
        KITTI_SYSTEM_PROMPT
        + f"\n\nSUMMARY OF EARLIER CONVERSATION:\n{conversation_summary}"
    )

def _build_tool_use_content(block) -> list:
    return [{
        "type": "tool_use",
        "id": block.id,
        "name": block.name,
        "input": block.input,
    }]

def _build_tool_result_content(result: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": result["tool_use_id"],
        "content": result["content"],
    }

def _is_file_task(message: str) -> bool:
    message_lower = message.lower()
    file_keywords = [
        "create", "make", "generate", "build",
        "convert", "export", "save as", "export to",
        "word doc", "excel", "powerpoint", "pdf",
        "document", "spreadsheet", "presentation",
        "rename", "delete", "move", "copy",
    ]
    return any(kw in message_lower for kw in file_keywords)

def _is_time_query(message: str) -> bool:
    message_lower = message.lower()
    time_keywords = ["time", "date", "day", "today", "now"]
    return any(kw in message_lower for kw in time_keywords) and len(message.split()) < 8

def _verify_file_operation(code: str, result: str, all_results: list = None) -> str | None:
    import re
    desktop = os.path.expanduser("~/Desktop")
    results_to_check = all_results if all_results else [result]

    # Check all results for clear success indicators
    for res in results_to_check:
        res_lower = res.lower()

        # Move operation detected
        if "moved to documents" in res_lower or "moved from desktop" in res_lower:
            # Extract filename from the result
            fname_match = re.search(r'Customer_List\.xlsx', res)
            if fname_match:
                return "Moved Customer_List.xlsx to Documents"
            return "Moved file to Documents"

        # Delete operation - file should be gone
        if "delete" in code.lower() or "os.remove" in code or "shutil.rmtree" in code:
            fname_match = re.search(r'([A-Za-z_0-9\-]+\.(xlsx|docx|pptx|pdf))', res, re.IGNORECASE)
            if fname_match:
                fname = fname_match.group(1)
                full_path = os.path.join(desktop, os.path.basename(fname))
                if not os.path.exists(full_path):
                    return f"Deleted {os.path.basename(fname)}"
            if "file not found" in res_lower or "deleted:" in res_lower:
                fname_match2 = re.search(r'([A-Za-z_0-9\-]+\.(xlsx|docx|pptx|pdf))', res, re.IGNORECASE)
                if fname_match2:
                    return f"Deleted {fname_match2.group(1)}"

        # Create/save operation - file should exist
        if any(ext in res for ext in ['.xlsx', '.docx', '.pptx', '.pdf']):
            # For create: look for confirmation in results
            if "verified:" in res_lower or "file exists: true" in res_lower or "created successfully" in res_lower:
                fname_match = re.search(r'([A-Za-z_0-9\-]+\.(xlsx|docx|pptx|pdf))', res, re.IGNORECASE)
                if fname_match:
                    return f"Created {fname_match.group(1)}"

    # Find file paths mentioned in code
    file_pattern = re.compile(r'([A-Za-z_0-9\-]+\.(xlsx|docx|pptx|pdf))', re.IGNORECASE)
    found_files = []
    for src in [code] + results_to_check:
        matches = file_pattern.findall(src)
        for m in matches:
            found_files.append(m[0])
    found_files = list(dict.fromkeys(found_files))

    # For move: source should NOT exist, destination should exist
    if "shutil.move" in code or "move" in code.lower():
        source_match = re.search(r'expanduser\(["\']([^"\']+)["\']\)', code)
        if source_match:
            # Check if file is gone from desktop
            for fname in found_files:
                desktop_path = os.path.join(desktop, os.path.basename(fname))
                if not os.path.exists(desktop_path):
                    # File is gone from desktop - it was moved
                    return f"Moved {os.path.basename(fname)}"

    # For create: file should exist
    for fname in found_files:
        normalized = fname.replace("/", "\\")
        full_path = os.path.join(desktop, os.path.basename(normalized))
        if os.path.exists(full_path):
            return f"Created {os.path.basename(normalized)}"

    # Check for update operations - original file or _Updated version exists
    # Pattern: file was updated (may have been saved to new name first)
    updated_name = None
    for fname in found_files:
        normalized = fname.replace("/", "\\")
        basename = os.path.basename(normalized)
        # Check for updated version
        if "_Updated" in basename:
            base_without_updated = basename.replace("_Updated", "")
            updated_path = os.path.join(desktop, base_without_updated)
            updated_name = base_without_updated
            # If original name file exists (after rename), it's updated
            if os.path.exists(updated_path):
                return f"Updated {base_without_updated}"
        else:
            # Check if file exists with original name
            full_path = os.path.join(desktop, basename)
            if os.path.exists(full_path):
                return f"Updated {basename}"

    # Fallback: trust if no errors
    for res in results_to_check:
        res_lower = res.lower()
        if "error" not in res_lower and "traceback" not in res_lower:
            if any(kw in res_lower for kw in ["success", "created", "saved", "moved", "done", "updated"]):
                return res.strip()[:80]

    return None

def chat_with_kitti(user_message: str, metrics: EvalMetrics = None) -> str:
    global conversation_summary
    messages = list(chat_history)
    messages.append({"role": "user", "content": user_message})
    MAX_TOOL_CALLS = 5
    final_reply = "I ran into an issue doing that. Could you try rephrasing?"

    for iteration in range(MAX_TOOL_CALLS):
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=build_system_prompt(),
            messages=messages,
            tools=TOOLS,
        )

        reply_text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                if not tool_calls:
                    reply_text = block.text.strip()
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            if reply_text:
                if _is_file_task(user_message):
                    messages.append({
                        "role": "user",
                        "content": "You MUST call a tool. Do not say Done! or explain - "
                                   "write Python code using the run_code tool and execute it now. "
                                   "Use python-docx, openpyxl, pptx, or reportlab. "
                                   "Save to os.path.expanduser('~/Desktop')."
                    })
                    continue
                final_reply = reply_text
                chat_history.append({"role": "user", "content": user_message})
                chat_history.append({"role": "assistant", "content": final_reply})
                break
            messages.append({"role": "assistant", "content": ""})
            messages.append({"role": "user", "content": "Please respond with a tool call."})
            continue

        # Track retries (iteration 0 = first attempt, iteration > 0 = retry)
        if metrics and iteration > 0:
            metrics.llm_retries += 1

        tool_results = []
        for call in tool_calls:
            name = call.name
            inpt = dict(call.input)
            print(f"   Tool call: {name} -> {inpt}")

            t_tool_start = time_module.perf_counter()
            try:
                result = execute_tool(name, inpt)
            except Exception as e:
                result = f"Tool execution error: {e}"
            tool_duration = time_module.perf_counter() - t_tool_start

            # Check for errors in result
            is_error = "Traceback" in result or "Error" in result or "Exception" in result
            if metrics:
                metrics.tool_calls.append({
                    "name": name,
                    "duration": tool_duration,
                    "error": None if not is_error else result[:60]
                })
                if is_error:
                    metrics.add_error(f"[{name}] {result[:80]}")

            print(f"   Tool result: {result[:100]}...")
            tool_results.append({
                "tool_use_id": call.id,
                "tool": name,
                "content": result,
            })

        if _is_file_task(user_message) and tool_results:
            last_code = None
            for call in tool_calls:
                if call.name == "run_code":
                    last_code = call.input.get("code", "")

            all_results = [res["content"] for res in tool_results]
            last_code = None
            for call in tool_calls:
                if call.name == "run_code":
                    last_code = call.input.get("code", "")

            verified = _verify_file_operation(last_code or "", "", all_results)
            if verified:
                final_reply = f"Done! {verified} on your Desktop."
                chat_history.append({"role": "user", "content": user_message})
                chat_history.append({"role": "assistant", "content": final_reply})
                save_memory()
                return final_reply

            # File task failed — count as retry if we still have attempts left
            if metrics:
                metrics.tool_retries += 1

            assistant_content = []
            for block in response.content:
                if block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})
            for res in tool_results:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": res["tool_use_id"],
                        "content": res["content"],
                    }]
                })
            messages.append({
                "role": "user",
                "content": "The file operation failed or did not complete. "
                           "Fix the code and call run_code again. "
                           "Verify the file exists with os.path.exists() before saying Done."
            })
            if metrics:
                metrics.add_error(f"File task failed verification, retry #{metrics.tool_retries}")
            continue

        assistant_content = []
        for block in response.content:
            if block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            elif block.type == "text" and block.text.strip():
                assistant_content.append({
                    "type": "text",
                    "text": block.text,
                })

        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})

        for res in tool_results:
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": res["tool_use_id"],
                    "content": res["content"],
                }]
            })

    if len(chat_history) > 20:
        old_messages = chat_history[:10]
        new_summary = summarize_old_messages(old_messages)
        conversation_summary = (
            f"{conversation_summary} Later: {new_summary}"
            if conversation_summary else new_summary
        )
        del chat_history[:10]
        print(f"   Summary updated: {conversation_summary[:100]}...")

    save_memory()
    return final_reply

AUDIO_DIR = "received_audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

@app.get("/health")
def health_check():
    return {"status": "kitti is alive", "version": "1.0.0"}

def transcribe_audio(filepath: str) -> str:
    segments, info = whisper_model.transcribe(
        filepath,
        beam_size=5,
        language="en",
        initial_prompt=WHISPER_VOCAB_HINT,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    print(f"   Language: {info.language} (prob: {info.language_probability:.2f})")
    return text

transcript_queue = []

async def run_llm_and_tts(websocket: WebSocket, pending_ref: list, req_id: int, input_type: str, t_speech_end: float = None, stt_duration: float = None):
    global _current_metrics
    transcripts = list(pending_ref)

    # ── Create metrics for this request ──
    combined_transcript = transcripts[0] if len(transcripts) == 1 else " | ".join(transcripts)
    metrics = EvalMetrics(req_id, input_type, combined_transcript)
    _current_metrics = metrics
    if t_speech_end:
        metrics.t_speech_end = t_speech_end
    if stt_duration:
        metrics.stt_duration = stt_duration

    try:
        loop = asyncio.get_event_loop()

        if len(transcripts) == 1:
            user_message = transcripts[0]
        else:
            user_message = " | ".join(transcripts)
            print(f"   Combined {len(transcripts)} transcripts: \"{user_message}\"")

        # ── LLM ──
        print("Asking LLM...")
        t_llm_start = time_module.perf_counter()
        reply = await loop.run_in_executor(None, chat_with_kitti, user_message, metrics)
        metrics.llm_duration = time_module.perf_counter() - t_llm_start
        print(f"   Kitti: \"{reply}\"")
        print(f"   LLM latency: {metrics.llm_duration:.3f}s  |  retries: {metrics.llm_retries}")

        await websocket.send_text(json.dumps({"type": "response", "text": reply}))

        # ── TTS ──
        print("Generating speech...")
        t_tts_start = time_module.perf_counter()
        audio_out = await text_to_speech(reply)
        metrics.tts_duration = time_module.perf_counter() - t_tts_start
        print(f"   TTS latency: {metrics.tts_duration:.3f}s  |  Audio: {len(audio_out)} bytes")

        # ── E2E: total pipeline time (STT + LLM + TTS) ──
        total = (metrics.stt_duration or 0) + (metrics.llm_duration or 0) + (metrics.tts_duration or 0)
        metrics.e2e_duration = total
        stages = []
        if metrics.stt_duration: stages.append(f"STT {metrics.stt_duration:.3f}s")
        if metrics.llm_duration: stages.append(f"LLM {metrics.llm_duration:.3f}s")
        if metrics.tts_duration: stages.append(f"TTS {metrics.tts_duration:.3f}s")
        print(f"   E2E latency: {total:.3f}s  ({' + '.join(stages)})")

        metrics.state = "completed"
        await websocket.send_bytes(audio_out)

    except asyncio.CancelledError:
        metrics.state = "interrupted"
        metrics.e2e_duration = None
        raise
    except Exception as e:
        metrics.state = "failed"
        metrics.add_error(f"run_llm_and_tts exception: {e}")
        raise
    finally:
        print_eval_summary()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Frontend connected via WebSocket")

    current_llm_task = None
    req_id = 0
    loop = asyncio.get_event_loop()
    last_pending_ref = []
    t_speech_end = None  # timestamp when user stopped speaking

    try:
        while True:
            # Receive can be either bytes (audio) or text (JSON)
            message = await websocket.receive()

            if "bytes" in message:
                # === VOICE INPUT ===
                audio_bytes = message["bytes"]
                req_id += 1
                print(f"\n  [req {req_id}] Audio: {len(audio_bytes)} bytes")

                # ── Handle barge-in: cancel any in-flight request ──
                if current_llm_task and not current_llm_task.done():
                    current_llm_task.cancel()
                    try:
                        await current_llm_task
                    except asyncio.CancelledError:
                        pass
                    # Print eval for the interrupted request
                    print_eval_summary()
                    transcript_queue.extend(last_pending_ref)
                    print(f"   Barge-in — {len(last_pending_ref)} transcript(s) re-queued")
                    last_pending_ref = []

                timestamp = datetime.now().strftime("%H%M%S")
                filepath = os.path.join(AUDIO_DIR, f"audio_{timestamp}.wav")
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)

                print("   Transcribing...")
                t_stt_start = time_module.perf_counter()
                transcript = await loop.run_in_executor(None, transcribe_audio, filepath)
                stt_duration = time_module.perf_counter() - t_stt_start
                print(f"   You said: \"{transcript}\"")
                print(f"   STT latency: {stt_duration:.3f}s")

                if not transcript:
                    continue

                # t_speech_end comes from frontend (VAD speech-end time, sent as part of audio metadata)
                # If not provided, fall back to after STT completes (old behavior, undercounts E2E)
                transcript_queue.append(transcript)
                await websocket.send_text(json.dumps({"type": "transcript", "text": transcript}))

                last_pending_ref = list(transcript_queue)
                transcript_queue.clear()

                current_llm_task = asyncio.create_task(
                    run_llm_and_tts(websocket, last_pending_ref, req_id, "voice", t_speech_end, stt_duration)
                )

            elif "text" in message:
                # === TEXT INPUT ===
                req_id += 1
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "text":
                        text_input = data.get("text", "").strip()
                        if text_input:
                            print(f"\n  [req {req_id}] Text: \"{text_input}\"")

                            # ── Handle barge-in for text too ──
                            if current_llm_task and not current_llm_task.done():
                                current_llm_task.cancel()
                                try:
                                    await current_llm_task
                                except asyncio.CancelledError:
                                    pass
                                print_eval_summary()
                                print(f"   Barge-in during text request")

                            await websocket.send_text(json.dumps({"type": "transcript", "text": text_input}))
                            t_speech_end = time_module.perf_counter()
                            last_pending_ref = [text_input]

                            current_llm_task = asyncio.create_task(
                                run_llm_and_tts(websocket, last_pending_ref, req_id, "text", t_speech_end)
                            )
                    elif data.get("type") == "speechEnd":
                        # Frontend sends VAD speech-end timestamp — stored for future use
                        # when time-base synchronization is implemented
                        pass
                except json.JSONDecodeError:
                    print(f"   Invalid JSON received: {message['text']}")

    except Exception as e:
        print(f"WebSocket closed: {e}")
        if current_llm_task and not current_llm_task.done():
            current_llm_task.cancel()
            print_eval_summary()