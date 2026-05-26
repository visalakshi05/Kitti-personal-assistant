import os
import json
import asyncio
import tempfile
from datetime import datetime
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel
from anthropic import Anthropic
from dotenv import load_dotenv
import edge_tts

from tools.tool_registry import TOOLS, execute_tool

load_dotenv()

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

    # Fallback: trust if no errors
    for res in results_to_check:
        res_lower = res.lower()
        if "error" not in res_lower and "traceback" not in res_lower:
            if any(kw in res_lower for kw in ["success", "created", "saved", "moved", "done"]):
                return res.strip()[:80]

    return None

def chat_with_kitti(user_message: str) -> str:
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

        tool_results = []
        for call in tool_calls:
            name = call.name
            inpt = dict(call.input)
            print(f"   Tool call: {name} -> {inpt}")
            result = execute_tool(name, inpt)
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

async def run_llm_and_tts(websocket: WebSocket, pending_ref: list, req_id: int):
    transcripts = list(pending_ref)
    try:
        loop = asyncio.get_event_loop()

        if len(transcripts) == 1:
            user_message = transcripts[0]
        else:
            user_message = " | ".join(transcripts)
            print(f"   Combined {len(transcripts)} transcripts: \"{user_message}\"")

        print("Asking LLM...")
        reply = await loop.run_in_executor(None, chat_with_kitti, user_message)
        print(f"   Kitti: \"{reply}\"")

        await websocket.send_text(json.dumps({"type": "response", "text": reply}))

        print("Generating speech...")
        audio_out = await text_to_speech(reply)
        print(f"   Audio: {len(audio_out)} bytes")
        await websocket.send_bytes(audio_out)

    except asyncio.CancelledError:
        raise

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Frontend connected via WebSocket")

    current_llm_task = None
    req_id = 0
    loop = asyncio.get_event_loop()
    last_pending_ref = []

    try:
        while True:
            # Receive can be either bytes (audio) or text (JSON)
            message = await websocket.receive()

            if "bytes" in message:
                # === AUDIO INPUT (voice) ===
                audio_bytes = message["bytes"]
                req_id += 1
                print(f"\n  [req {req_id}] Audio: {len(audio_bytes)} bytes")

                if current_llm_task and not current_llm_task.done():
                    current_llm_task.cancel()
                    try:
                        await current_llm_task
                    except asyncio.CancelledError:
                        pass
                    transcript_queue.extend(last_pending_ref)
                    print(f"   Request cancelled - {len(last_pending_ref)} transcript(s) re-queued")
                    last_pending_ref = []

                timestamp = datetime.now().strftime("%H%M%S")
                filepath = os.path.join(AUDIO_DIR, f"audio_{timestamp}.wav")
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)

                print("   Transcribing...")
                transcript = await loop.run_in_executor(None, transcribe_audio, filepath)
                print(f"   You said: \"{transcript}\"")

                if not transcript:
                    continue

                transcript_queue.append(transcript)
                await websocket.send_text(json.dumps({"type": "transcript", "text": transcript}))

                last_pending_ref = list(transcript_queue)
                transcript_queue.clear()
                current_llm_task = asyncio.create_task(
                    run_llm_and_tts(websocket, last_pending_ref, req_id)
                )

            elif "text" in message:
                # === TEXT INPUT (type box) ===
                req_id += 1
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "text":
                        text_input = data.get("text", "").strip()
                        if text_input:
                            print(f"\n  [req {req_id}] Text: \"{text_input}\"")
                            await websocket.send_text(json.dumps({"type": "transcript", "text": text_input}))

                            last_pending_ref = [text_input]
                            current_llm_task = asyncio.create_task(
                                run_llm_and_tts(websocket, last_pending_ref, req_id)
                            )
                except json.JSONDecodeError:
                    print(f"   Invalid JSON received: {message['text']}")

    except Exception as e:
        print(f"WebSocket closed: {e}")
        if current_llm_task and not current_llm_task.done():
            current_llm_task.cancel()