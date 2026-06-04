import os
from datetime import datetime

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def get_desktop_path() -> str:
    """Returns the user's Desktop path."""
    return os.path.expanduser("~/Desktop")

# ─────────────────────────────────────────
# TOOL SCHEMAS
# Tells the LLM what tools exist and what inputs they need
# ─────────────────────────────────────────

TOOLS = [
    {
        "name": "get_current_time",
        "description": "Returns the current date and time. Use when user asks what time it is, what day it is, today's date, etc. ALWAYS call this tool for time/date queries instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "run_code",
        "description": "Executes Python code on the user's machine. Use for ALL file operations: creating/editing files (Word, Excel, PowerPoint, PDF), converting formats, renaming, deleting, moving, copying, reading, data processing, and COM automation. Desktop: os.path.expanduser('~/Desktop'). Available libraries: os, shutil, glob, json, csv, pathlib, zipfile, openpyxl, python-docx, pptx, reportlab, win32com.client, pythoncom, datetime, math. IMPORTANT for COM automation (Excel/Word/PowerPoint via win32com): you MUST call pythoncom.CoInitialize() before creating the Dispatch object, and pythoncom.CoUninitialize() after. Example: pythoncom.CoInitialize(); xl = win32com.client.Dispatch('Excel.Application'); ...; xl.Quit(); pythoncom.CoUninitialize(). If code errors, read the error, fix it, and call run_code again. Always verify files exist with os.path.exists() before reporting success.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Always save output files to Desktop using os.path.expanduser('~/Desktop')."
                }
            },
            "required": ["code"]
        }
    }
]

# ─────────────────────────────────────────
# TOOL EXECUTORS
# ─────────────────────────────────────────

def get_current_time() -> str:
    now = datetime.now()
    return now.strftime("It's %I:%M %p on %A, %B %d, %Y")


def _patch_common_code_mistakes(code: str) -> str:
    """Fix common LLM-generated code mistakes before execution."""

    # Fix PP_ALIGN import — it's pp.align, not a separate import
    # Only remove the import line, not usage of PP_ALIGN in the code
    import re
    code = re.sub(r'^from pptx\.enum\.text import PP_ALIGN.*$', '# PP_ALIGN import removed', code, flags=re.MULTILINE)
    # Replace PP_ALIGN usage with None (placeholder, actual alignment not needed for basic edits)
    code = code.replace("PP_ALIGN.CENTER", "PP_ALIGN.CENTER")  # Keep valid usage
    code = re.sub(r'\bPP_ALIGN\b(?![\.])', 'None', code)  # Replace standalone PP_ALIGN

    # Fix RgbColor vs RGBColor - use RGBColor which is the correct import
    code = code.replace("from pptx.dml.color import RgbColor", "from pptx.dml.color import RGBColor")
    code = code.replace("RgbColor(", "RGBColor(")

    # Fix Unix-style paths on Windows - convert /mnt/c/ to C:\
    code = code.replace("/mnt/c/", "C:/")  # Use forward slash for cross-compat

    # Ensure pythoncom.CoInitialize() is called before win32com Dispatch in COM code
    lines = code.split('\n')
    patched_lines = []
    com_started = False
    needs_coinit = False

    for line in lines:
        stripped = line.strip()
        if 'win32com.client.Dispatch' in stripped or 'win32com.client' in stripped:
            needs_coinit = True
        if needs_coinit and ('win32com.client.Dispatch' in stripped or '.Dispatch(' in stripped and 'win32com' in stripped):
            # Insert CoInitialize before this line
            if 'pythoncom.CoInitialize()' not in code:
                patched_lines.append('    pythoncom.CoInitialize()')
            needs_coinit = False
            com_started = True
        patched_lines.append(line)

    code = '\n'.join(patched_lines)
    return code


def run_code(code: str) -> str:
    """
    Executes Python code in an isolated thread with a timeout.
    If the code fails, the error is returned so the LLM can read it, fix the code, and retry.
    """
    import concurrent.futures
    import traceback
    import sys
    from io import StringIO

    desktop = os.path.expanduser("~/Desktop")

    # Patch common LLM mistakes before executing
    code = _patch_common_code_mistakes(code)

    def _execute():
        output_buffer = StringIO()
        old_stdout = sys.stdout
        sys.stdout = output_buffer

        try:
            namespace = {
                "__builtins__": __builtins__,
                "desktop": desktop,
                "os": os,
                "datetime": datetime,
                "json": __import__("json"),
                "csv": __import__("csv"),
                "shutil": __import__("shutil"),
                "glob": __import__("glob"),
                "pathlib": __import__("pathlib"),
                "zipfile": __import__("zipfile"),
                "openpyxl": __import__("openpyxl"),
                "docx": __import__("docx"),
                "pptx": __import__("pptx"),
                "reportlab": __import__("reportlab"),
                "win32com": __import__("win32com"),
                "pythoncom": __import__("pythoncom"),
            }
            exec(code, namespace)
            sys.stdout = old_stdout
            captured = output_buffer.getvalue()
            if captured.strip():
                return captured.strip()
            return "Code executed successfully."
        except Exception:
            sys.stdout = old_stdout
            return traceback.format_exc()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_execute)
            try:
                output = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                return "Code timed out after 30 seconds. Check for infinite loops."
    except Exception as e:
        return f"Execution error: {str(e)}"

    if "Traceback" in output and ("Error" in output or "Exception" in output):
        return f"Error in code:\n{output}"
    return output


# ─────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict) -> str:
    print(f"   🔧 Executing tool: {tool_name} with input: {tool_input}")

    if tool_name == "get_current_time":
        return get_current_time()

    elif tool_name == "run_code":
        return run_code(code=tool_input.get("code", ""))

    return f"Unknown tool: {tool_name}"