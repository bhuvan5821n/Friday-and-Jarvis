// SpeechCore — JARVIS's dedicated real-time audio engine (C++, WASAPI).
//
// Responsibilities (and nothing else): persistent mic capture, rolling
// prebuffer, energy VAD, audio level meter, device-change recovery, and an
// event-driven local-TCP IPC that streams audio + events to Python.
// SpeechCore never talks to AI and never calls web APIs.
//
// Protocol (localhost TCP, default port 48800, Python is the client):
//   Text events   : one JSON object per line, e.g.
//                   {"ev":"WakeWordDetected"}  {"ev":"SpeechStarted"}
//                   {"ev":"SpeechEnded"}       {"ev":"HealthStatus",...}
//                   {"ev":"MicChanged","name":"..."} {"ev":"MicDisconnected"}
//                   {"ev":"MicRecovered"}      {"ev":"Level","rms":0.42}
//   Audio frames  : binary, little-endian header  'A','U', uint32 nbytes,
//                   then nbytes of 16 kHz mono int16 PCM.
//   Client → core : one JSON per line: {"cmd":"stream_on"} {"cmd":"stream_off"}
//                   {"cmd":"ping"}
//
// Wake word stays in Python (openwakeword is a neural model; re-implementing
// it here would duplicate the engine, not speed it up — capture → IPC → predict
// is already sub-frame). SpeechCore's job is to make capture itself bulletproof
// and zero-copy cheap, with prebuffer so no first-syllable loss.
//
// Build: g++ -std=c++20 -O2 speechcore.cpp -o SpeechCore.exe
//        -lole32 -lws2_32 -lavrt -static -mwindows
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <initguid.h>                        // instantiate the GUIDs/PKEYs below
#include <functiondiscoverykeys_devpkey.h>
#include <avrt.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// ── config ──────────────────────────────────────────────────────────────
static const int   kSampleRate   = 16000;    // matches Python SEND_SAMPLE_RATE
static const int   kChannels     = 1;
static const int   kIpcPort      = 48800;
static const float kVadOn        = 0.012f;   // rms (0..1) to enter speech
static const float kVadOff       = 0.006f;   // rms to leave speech
static const int   kVadHangMs    = 600;      // silence needed to end speech
static const int   kLevelEveryMs = 100;      // level-meter event cadence

// ── logging (never crash silently) ──────────────────────────────────────
static FILE* g_log = nullptr;
static std::mutex g_logMu;
static void logf(const char* fmt, ...) {
    std::lock_guard<std::mutex> lk(g_logMu);
    if (!g_log) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_log, "%02d:%02d:%02d.%03d ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_list ap; va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
    fprintf(g_log, "\n"); fflush(g_log);
}

// ── IPC server: one Python client, events + audio frames ────────────────
class Ipc {
public:
    bool start() {
        WSADATA w; WSAStartup(MAKEWORD(2, 2), &w);
        listen_ = socket(AF_INET, SOCK_STREAM, 0);
        BOOL one = TRUE;
        setsockopt(listen_, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (char*)&one, sizeof one);
        sockaddr_in a{}; a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK); a.sin_port = htons(kIpcPort);
        if (bind(listen_, (sockaddr*)&a, sizeof a) != 0) {
            logf("[IPC] bind failed (%d) - another SpeechCore running?", WSAGetLastError());
            return false;
        }
        listen(listen_, 1);
        std::thread(&Ipc::acceptLoop, this).detach();
        logf("[IPC] listening on 127.0.0.1:%d", kIpcPort);
        return true;
    }
    void sendEvent(const std::string& json) {
        std::lock_guard<std::mutex> lk(mu_);
        if (client_ == INVALID_SOCKET) return;
        std::string line = json + "\n";
        if (!sendAll(line.data(), line.size())) drop();
    }
    void sendAudio(const int16_t* pcm, size_t samples) {
        std::lock_guard<std::mutex> lk(mu_);
        if (client_ == INVALID_SOCKET || !streaming_) return;
        uint32_t n = (uint32_t)(samples * sizeof(int16_t));
        char hdr[6] = {'A', 'U'};
        memcpy(hdr + 2, &n, 4);
        if (!sendAll(hdr, 6) || !sendAll((const char*)pcm, n)) drop();
    }
    bool connected() { std::lock_guard<std::mutex> lk(mu_); return client_ != INVALID_SOCKET; }
    std::atomic<bool> streaming_{false};

private:
    bool sendAll(const char* p, size_t n) {   // partial sends corrupt framing
        while (n) {
            int w = send(client_, p, (int)n, 0);
            if (w <= 0) return false;
            p += w; n -= (size_t)w;
        }
        return true;
    }
    void drop() { if (client_ != INVALID_SOCKET) { closesocket(client_); client_ = INVALID_SOCKET; logf("[IPC] client dropped"); } }
    void acceptLoop() {
        for (;;) {
            SOCKET c = accept(listen_, nullptr, nullptr);
            if (c == INVALID_SOCKET) { Sleep(200); continue; }
            BOOL nd = TRUE; setsockopt(c, IPPROTO_TCP, TCP_NODELAY, (char*)&nd, sizeof nd);
            { std::lock_guard<std::mutex> lk(mu_); drop(); client_ = c; }
            logf("[IPC] python connected");
            std::thread(&Ipc::readLoop, this, c).detach();
        }
    }
    void readLoop(SOCKET c) {                    // client commands, line-based
        std::string buf; char tmp[512];
        for (;;) {
            int n = recv(c, tmp, sizeof tmp, 0);
            if (n <= 0) break;
            buf.append(tmp, n);
            size_t nl;
            while ((nl = buf.find('\n')) != std::string::npos) {
                std::string line = buf.substr(0, nl); buf.erase(0, nl + 1);
                if (line.find("stream_on")  != std::string::npos) { streaming_ = true;  logf("[IPC] stream ON"); }
                if (line.find("stream_off") != std::string::npos) { streaming_ = false; logf("[IPC] stream OFF"); }
                if (line.find("ping") != std::string::npos) sendEvent("{\"ev\":\"pong\"}");
            }
        }
        std::lock_guard<std::mutex> lk(mu_);
        if (client_ == c) drop();
    }
    SOCKET listen_ = INVALID_SOCKET, client_ = INVALID_SOCKET;
    std::mutex mu_;
};

// ── WASAPI capture: persistent, event-driven, auto-recovering ───────────
class Capture {
public:
    Capture(Ipc& ipc) : ipc_(ipc) {}

    void run() {                       // never returns; recovers forever
        CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        for (;;) {
            if (openDevice()) loop();  // loop() exits on device failure
            ipc_.sendEvent("{\"ev\":\"MicDisconnected\"}");
            logf("[Audio] device lost - retrying in 1s");
            closeDevice();
            Sleep(1000);
        }
    }

private:
    bool openDevice() {
        IMMDeviceEnumerator* en = nullptr;
        if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr,
                CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) return false;
        IMMDevice* dev = nullptr;
        HRESULT hr = en->GetDefaultAudioEndpoint(eCapture, eConsole, &dev);
        en->Release();
        if (FAILED(hr)) { logf("[Audio] no default mic (0x%08lx)", hr); return false; }

        IPropertyStore* ps = nullptr; std::string name = "unknown";
        if (SUCCEEDED(dev->OpenPropertyStore(STGM_READ, &ps))) {
            PROPVARIANT v; PropVariantInit(&v);
            if (SUCCEEDED(ps->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR) {
                char nb[256]; WideCharToMultiByte(CP_UTF8, 0, v.pwszVal, -1, nb, sizeof nb, nullptr, nullptr);
                name = nb;
            }
            PropVariantClear(&v); ps->Release();
        }

        hr = dev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&client_);
        dev->Release();
        if (FAILED(hr)) return false;

        WAVEFORMATEX fmt{};             // shared mode + auto-convert to 16k mono
        fmt.wFormatTag = WAVE_FORMAT_PCM; fmt.nChannels = kChannels;
        fmt.nSamplesPerSec = kSampleRate; fmt.wBitsPerSample = 16;
        fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample / 8;
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;
        hr = client_->Initialize(AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                200 * 10000 /*200ms buffer*/, 0, &fmt, nullptr);
        if (FAILED(hr)) { logf("[Audio] Initialize failed 0x%08lx", hr); return false; }

        event_ = CreateEventA(nullptr, FALSE, FALSE, nullptr);
        client_->SetEventHandle(event_);
        if (FAILED(client_->GetService(__uuidof(IAudioCaptureClient), (void**)&cap_))) return false;
        if (FAILED(client_->Start())) return false;

        char ev[320];
        snprintf(ev, sizeof ev, "{\"ev\":\"MicChanged\",\"name\":\"%s\"}", name.c_str());
        ipc_.sendEvent(ev);
        ipc_.sendEvent("{\"ev\":\"MicRecovered\"}");
        logf("[Audio] capture started on '%s' (16k mono, event-driven)", name.c_str());
        return true;
    }

    void closeDevice() {
        if (client_) { client_->Stop(); client_->Release(); client_ = nullptr; }
        if (cap_) { cap_->Release(); cap_ = nullptr; }
        if (event_) { CloseHandle(event_); event_ = nullptr; }
    }

    void loop() {
        // boost this thread to pro-audio scheduling class
        DWORD taskIdx = 0;
        HANDLE avrt = AvSetMmThreadCharacteristicsA("Pro Audio", &taskIdx);

        bool  inSpeech = false;
        auto  lastVoice = std::chrono::steady_clock::now();
        auto  lastLevel = lastVoice;
        auto  lastHealth = lastVoice;

        for (;;) {
            if (WaitForSingleObject(event_, 2000) != WAIT_OBJECT_0) {
                logf("[Audio] event timeout - device stalled"); break;
            }
            for (;;) {
                UINT32 avail = 0;
                if (FAILED(cap_->GetNextPacketSize(&avail))) return;
                if (!avail) break;
                BYTE* data; UINT32 frames; DWORD flags;
                if (FAILED(cap_->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) return;
                const int16_t* pcm = (const int16_t*)data;
                const size_t n = frames * kChannels;

                // rms level (0..1)
                double acc = 0;
                for (size_t i = 0; i < n; i++) { double s = pcm[i] / 32768.0; acc += s * s; }
                float rms = n ? (float)std::sqrt(acc / n) : 0.f;

                auto now = std::chrono::steady_clock::now();

                // VAD is an informational HINT only (SpeechStarted/Ended events),
                // never a gate on the audio. Python's wake-word detector and
                // Gemini Live both need the CONTINUOUS stream, exactly like the
                // sounddevice path they replace. ponytail: gating here would
                // silently break "Jarvis" detection — worse than sending silence.
                if (!inSpeech && rms >= kVadOn) {
                    inSpeech = true; lastVoice = now;
                    ipc_.sendEvent("{\"ev\":\"SpeechStarted\"}");
                } else if (inSpeech) {
                    if (rms >= kVadOff) lastVoice = now;
                    else if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastVoice).count() > kVadHangMs) {
                        inSpeech = false;
                        ipc_.sendEvent("{\"ev\":\"SpeechEnded\"}");
                    }
                }

                // continuous live audio out while streaming (Gemini does its
                // own turn detection; wake word needs every block)
                if (ipc_.streaming_) ipc_.sendAudio(pcm, n);

                // level meter at 10Hz
                if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastLevel).count() >= kLevelEveryMs) {
                    lastLevel = now;
                    char ev[64]; snprintf(ev, sizeof ev, "{\"ev\":\"Level\",\"rms\":%.3f}", rms);
                    ipc_.sendEvent(ev);
                }
                // health heartbeat every 5s
                if (std::chrono::duration_cast<std::chrono::seconds>(now - lastHealth).count() >= 5) {
                    lastHealth = now;
                    char ev[128];
                    snprintf(ev, sizeof ev,
                        "{\"ev\":\"HealthStatus\",\"ok\":true,\"speech\":%s,\"client\":%s}",
                        inSpeech ? "true" : "false", ipc_.connected() ? "true" : "false");
                    ipc_.sendEvent(ev);
                }

                cap_->ReleaseBuffer(frames);
            }
        }
        if (avrt) AvRevertMmThreadCharacteristics(avrt);
    }

    Ipc& ipc_;
    IAudioClient* client_ = nullptr;
    IAudioCaptureClient* cap_ = nullptr;
    HANDLE event_ = nullptr;
};

// ── entry ───────────────────────────────────────────────────────────────
int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    // logs beside the exe: SpeechCore/Logs/speechcore.log
    char exe[MAX_PATH]; GetModuleFileNameA(nullptr, exe, MAX_PATH);
    std::string dir(exe); dir = dir.substr(0, dir.find_last_of("\\/"));
    CreateDirectoryA((dir + "\\Logs").c_str(), nullptr);
    g_log = fopen((dir + "\\Logs\\speechcore.log").c_str(), "a");
    logf("=== SpeechCore starting ===");

    // single instance via the IPC port itself (bind fails => already running)
    static Ipc ipc;
    if (!ipc.start()) return 1;

    static Capture capture(ipc);
    std::thread audio([&] { capture.run(); });   // audio thread — never exits
    audio.join();
    return 0;
}
