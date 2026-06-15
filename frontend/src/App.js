import { useState, useEffect, useRef, useCallback } from 'react';
import { MicVAD } from "@ricky0123/vad-web";
import './App.css';

function App() {
  const [backendStatus, setBackendStatus] = useState("checking...");
  const [appState, setAppState] = useState("starting"); // starting | listening | speaking | speaking | processing | denied
  const [message, setMessage] = useState("");
  const [transcript, setTranscript] = useState("");
  const [response, setResponse] = useState("");
  const [isMuted, setIsMuted] = useState(false);
  const [textInput, setTextInput] = useState("");

  const websocketRef = useRef(null);
  const vadRef = useRef(null);
  const currentAudioRef = useRef(null);   // tracks currently playing audio
  const isRespondingRef = useRef(false);  // true while Kitti is speaking aloud
  const isMutedRef = useRef(false);       // tracks mute state for VAD callbacks

  // Health check
  useEffect(() => {
    fetch("http://localhost:8000/health")
      .then((res) => res.json())
      .then((data) => setBackendStatus(data.status))
      .catch(() => setBackendStatus("backend not reachable"));
  }, []);

  // WebSocket setup
  useEffect(() => {
    const ws = new WebSocket("ws://localhost:8000/ws");
    ws.onopen = () => console.log("WebSocket connected");
    ws.onmessage = (event) => {
      // Audio bytes come as Blob, text messages come as string
      if (event.data instanceof Blob) {
        // Stop any currently playing audio first
        if (currentAudioRef.current) {
          currentAudioRef.current.pause();
          currentAudioRef.current = null;
        }

        const audioUrl = URL.createObjectURL(event.data);
        const audio = new Audio(audioUrl);
        currentAudioRef.current = audio;

        audio.play();
        isRespondingRef.current = true;   //  lock — Kitti is speaking
        setAppState("responding");

        audio.onended = () => {
          URL.revokeObjectURL(audioUrl);
          currentAudioRef.current = null;
          isRespondingRef.current = false; //  unlock — Kitti finished
          setAppState("listening");
        };
        return;
      }

      try {
        const data = JSON.parse(event.data);
        if (data.type === "transcript") {
          setTranscript(data.text || "(no speech detected)");
          setResponse("");
        } else if (data.type === "response") {
          setResponse(data.text);
          // stay in "processing" until audio arrives and plays
        } else if (data.type === "error") {
          setMessage(data.text);
          setAppState("listening");
          setTimeout(() => setMessage(""), 2000);
        }
      } catch {
        console.log("Backend says:", event.data);
      }
    };
    ws.onclose = () => console.log("WebSocket disconnected");
    ws.onerror = (err) => console.error("WebSocket error:", err);
    websocketRef.current = ws;
    return () => ws.close();
  }, []);

  // Send text message via WebSocket (bypasses VAD)
  const sendTextMessage = useCallback((text) => {
    if (websocketRef.current?.readyState === WebSocket.OPEN) {
      // Send as JSON message with type "text"
      websocketRef.current.send(JSON.stringify({ type: "text", text }));
    }
  }, []);

  // Convert Float32Array audio (from VAD) to WAV blob
  const float32ToWav = useCallback((audioData, sampleRate = 16000) => {
    const buffer = new ArrayBuffer(44 + audioData.length * 2);
    const view = new DataView(buffer);

    // WAV header
    const writeString = (offset, str) => {
      for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    writeString(0, "RIFF");
    view.setUint32(4, 36 + audioData.length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, "data");
    view.setUint32(40, audioData.length * 2, true);

    // PCM samples
    let offset = 44;
    for (let i = 0; i < audioData.length; i++) {
      const s = Math.max(-1, Math.min(1, audioData[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      offset += 2;
    }

    return new Blob([buffer], { type: "audio/wav" });
  }, []);

  // Init VAD on page load
  useEffect(() => {
    const initVAD = async () => {
      try {
        // Tell VAD where to find model + worklet
        const vad = await MicVAD.new({
          baseAssetPath: "/",
          onnxWASMBasePath: "/",
          model: "legacy",

          onSpeechStart: () => {
            // Skip if muted
            if (isMutedRef.current) return;

            // Stop Kitti's audio if she is speaking
            if (currentAudioRef.current) {
              currentAudioRef.current.pause();
              currentAudioRef.current = null;
              console.log("Kitti interrupted by user!");
            }
            isRespondingRef.current = false;

            // Clear previous transcript/response so UI feels fresh
            setTranscript("");
            setResponse("");
            setAppState("speaking");
          },
          onSpeechEnd: (audio) => {
            // If muted, ignore
            if (isMutedRef.current) return;

            // If Kitti is still speaking, ignore this detection (it's her own voice)
            if (isRespondingRef.current) {
              console.log("Ignored — Kitti is speaking");
              return;
            }
            console.log("Speech ended, samples:", audio.length);
            setAppState("processing");

            // Send VAD speech-end timestamp BEFORE audio bytes — so backend can measure true E2E
            if (websocketRef.current?.readyState === WebSocket.OPEN) {
              websocketRef.current.send(JSON.stringify({
                type: "speechEnd",
                time: performance.now(),  // ms since page load, matches perf_counter() basis
              }));
            }

            const wavBlob = float32ToWav(audio);
            wavBlob.arrayBuffer().then((buffer) => {
              if (websocketRef.current?.readyState === WebSocket.OPEN) {
                websocketRef.current.send(buffer);
              }
            });
          },
          onVADMisfire: () => {
            console.log("VAD misfire (too short)");
            setAppState("listening");
          },

          positiveSpeechThreshold: 0.65,  // need higher confidence to count as speech start
          negativeSpeechThreshold: 0.3,   // lower bar to declare silence
          redemptionFrames: 40,           // ~1.3s — won't split on natural pauses in sentences
          minSpeechFrames: 6,             // ignore brief blips
          preSpeechPadFrames: 5,          // grab a bit before speech starts (avoids clipping first word)
        });

        vad.start();
        vadRef.current = vad;
        setAppState("listening");
      } catch (err) {
        console.error("VAD init failed:", err);
        setAppState("denied");
        setMessage(err.message || String(err));
      }
    };

    initVAD();

    return () => {
      vadRef.current?.destroy();
    };
  }, [float32ToWav]);

  return (
    <div className="min-h-screen bg-gradient-to-b from-gray-950 via-gray-900 to-black flex flex-col items-center justify-center text-white relative overflow-hidden">

      {/* Title */}
      <div className="absolute top-12 text-center">
        <h1 className="text-5xl font-bold tracking-[0.3em] text-indigo-300">
          KITTI
        </h1>
        <p className="text-gray-600 text-xs mt-2 tracking-[0.4em] uppercase">
          Your Personal Assistant
        </p>
      </div>

      {/* Animated Orb */}
      <div className="relative flex items-center justify-center">

        {appState === "listening" && !isMuted && (
          <>
            <div className="absolute w-64 h-64 rounded-full border border-indigo-500/20 animate-ping" />
            <div className="absolute w-48 h-48 rounded-full border border-indigo-500/30 animate-ping" style={{ animationDelay: '0.5s' }} />
          </>
        )}

        {appState === "speaking" && !isMuted && (
          <>
            <div className="absolute w-72 h-72 rounded-full border-2 border-red-500/30 animate-ping" />
            <div className="absolute w-56 h-56 rounded-full border-2 border-red-500/40 animate-ping" style={{ animationDelay: '0.3s' }} />
          </>
        )}

        {appState === "processing" && !isMuted && (
          <div className="absolute w-60 h-60 rounded-full border-4 border-yellow-500/30 border-t-yellow-400 animate-spin" />
        )}

        <div className={`
          w-40 h-40 rounded-full transition-all duration-500
          ${appState === "listening" && !isMuted && "bg-indigo-500 shadow-[0_0_100px_30px_rgba(99,102,241,0.6)] animate-pulse"}
          ${appState === "speaking"  && !isMuted && "bg-red-500 shadow-[0_0_120px_40px_rgba(239,68,68,0.7)] scale-110"}
          ${appState === "processing" && !isMuted && "bg-yellow-500 shadow-[0_0_100px_30px_rgba(234,179,8,0.6)]"}
          ${appState === "starting"   && "bg-gray-700  shadow-[0_0_50px_15px_rgba(75,85,99,0.4)]"}
          ${appState === "responding" && "bg-green-500 shadow-[0_0_100px_30px_rgba(34,197,94,0.6)] animate-pulse"}
          ${appState === "denied"     && "bg-red-900   shadow-[0_0_50px_15px_rgba(127,29,29,0.4)]"}
          ${isMuted && "bg-gray-600 shadow-[0_0_50px_15px_rgba(75,85,99,0.4)]"}
          blur-sm opacity-90
        `} />
      </div>

      {/* Status text */}
      <div className="absolute bottom-32 text-center w-full px-6">
        <p className="text-gray-300 text-lg font-light tracking-wide h-7">
          {appState === "starting"   && "Loading voice model..."}
          {appState === "listening"  && "I'm listening"}
          {appState === "speaking"   && "Go on..."}
          {appState === "processing" && "Thinking..."}
          {appState === "responding" && "Kitti is speaking..."}
          {appState === "denied"     && "VAD failed to start"}
          {isMuted && <span className="text-red-400"> MIC OFF</span>}
        </p>

        {/* User transcript */}
        {transcript && (
          <div className="mt-6 max-w-2xl mx-auto">
            <p className="text-gray-500 text-xs uppercase tracking-widest mb-2">You said</p>
            <p className="text-white text-lg italic">"{transcript}"</p>
          </div>
        )}

        {/* Kitti's response */}
        {response && (
          <div className="mt-6 max-w-2xl mx-auto">
            <p className="text-indigo-400 text-xs uppercase tracking-widest mb-2">Kitti</p>
            <p className="text-indigo-100 text-lg">{response}</p>
          </div>
        )}

        {message && (
          <p className={`text-sm mt-2 max-w-md mx-auto ${appState === "denied" ? "text-red-400" : "text-green-400"}`}>
            {message}
          </p>
        )}

        {/* Text input box */}
        <div className="mt-8 max-w-2xl mx-auto">
          <textarea
            value={textInput}
            onChange={(e) => setTextInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && textInput.trim()) {
                e.preventDefault();
                sendTextMessage(textInput.trim());
                setTextInput("");
              }
            }}
            placeholder="Type a message... (Enter to send, Shift+Enter for new line)"
            rows={2}
            className="w-full bg-gray-800 border border-gray-600 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500 resize-y min-h-[60px] max-h-[200px]"
          />
          <div className="flex justify-end mt-2">
            <button
              onClick={() => {
                if (textInput.trim()) {
                  sendTextMessage(textInput.trim());
                  setTextInput("");
                }
              }}
              disabled={!textInput.trim()}
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed px-6 py-2 rounded-lg text-white font-medium transition-colors"
            >
              Send
            </button>
          </div>
        </div>
      </div>

      {/* Backend status */}
      <div className="absolute bottom-6 right-6 flex items-center gap-3">
        {/* Mute button */}
        <button
          onClick={() => {
            const newMuted = !isMuted;
            setIsMuted(newMuted);
            isMutedRef.current = newMuted;
          }}
          className={`w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 cursor-pointer ${
            isMuted
              ? "bg-red-600 hover:bg-red-500"
              : "bg-gray-700 hover:bg-gray-600"
          }`}
          title={isMuted ? "Unmute microphone" : "Mute microphone"}
        >
          {isMuted ? (
            /* Mic-off icon */
            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="1" y1="1" x2="23" y2="23" />
              <path d="M9 9v3a3 3 0 0 0 5.12 2.12" />
              <path d="M15 9.34V4a3 3 0 0 0-5.94-.6" />
              <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          ) : (
            /* Mic-on icon */
            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          )}
        </button>

        <span className={`w-2 h-2 rounded-full ${
          backendStatus === "kitti is alive" ? "bg-green-400 animate-pulse" : "bg-red-400"
        }`} />
        <span className="text-gray-600 text-xs tracking-wide">
          {backendStatus === "kitti is alive" ? "online" : "offline"}
        </span>
      </div>

    </div>
  );
}

export default App;
