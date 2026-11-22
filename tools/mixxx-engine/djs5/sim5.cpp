// sim5：v5 调度器的独立模拟器。调度器（engine5.hpp）和声音引擎里是同一份代码，这里只把 deck 换成假的——
// 假 deck 没有音频文件，按素材的网格在每一拍上出一个短脉冲（每小节第 1 拍响一点），所以测试能在输出里
// 直接量「这一拍落在第几帧」。只依赖标准库：c++ -O2 -std=c++17 sim5.cpp -o sim5 -lpthread。
//
// 行协议和声音引擎的 v5 模式一样：stdin 每行「#」+ JSON 命令（带 _id），stdout「#」+ 回复；
// 「@」+ JSON = 调度器主动发的回执 / 事件 / 状态。虚拟时钟按 DJSTUDIO_SPEED 倍速走（默认 1）。
//
// 假 deck 模拟了 Mixxx 的几处会影响对时的行为：装载要一会儿（按音频帧算）、定位和起播在下一块开头生效、
// 起播那一块从 0 渐入（EnginePregain）、loop 到尾回绕落在采样上、通道音频比位置晚一个固定的延迟（EQ 群延迟）。
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <thread>

#include "engine5.hpp"

using namespace djs5;

namespace {

constexpr int kSampleRate = 44100;
constexpr int kBufferFrames = 512;
constexpr int kDelayFrames = 59; // 和 Mixxx 默认 EQ 在 250 Hz 的群延迟一样
constexpr double kClickSec = 0.004;

struct Asset {
    std::string id;
    double dur = 0.0, bpm = 120.0, first = 0.0;
    bool fail = false;
    int wave = 0; // 0 = 每拍一个脉冲；1 = 白噪声；2 = 对数扫频（40 Hz → 16 kHz，2 秒一遍）。后两种是 −12 dBFS RMS 的宽带夹具
};

struct FakeDeck {
    const Asset* asset = nullptr;
    int gen = 0;         // 装好的代数（负 = 失败）
    int pendingGen = 0;  // 正在装的
    const Asset* pendingAsset = nullptr;
    long long readyAt = 0;
    double pos = 0.0;
    bool playing = false, wasPlaying = false;
    double rate = 1.0;
    bool loopOn = false;
    double l0 = 0.0, l1 = 0.0;
    double params[P_COUNT] = {1, 1, 1, 0.5, 1};
    bool keylock = false;
    bool xfade = false; // 这一块是定位后的交叉淡化
    double oldPos = 0.0;
    double hints[kHints] = {-1, -1, -1, -1, -1, -1, -1, -1}; // 预读点（秒）
    int miss = 0; // 读盘没跟上：2 = 这一块静音，1 = 这一块从 0 渐入
    std::vector<float> out = std::vector<float>(kBufferFrames * 2, 0.0f);
    std::deque<float> delay = std::deque<float>(static_cast<size_t>(kDelayFrames) * 2, 0.0f);
    long long seeks = 0, loads = 0;
};

class FakeBackend : public Backend {
  public:
    std::map<std::string, Asset> library;
    FakeDeck d[kMaxDecks + 1];
    long long frames = 0;
    int loadFrames = static_cast<int>(0.25 * kSampleRate);
    int nDecks = kMaxDecks;

    int decks() override { return nDecks; }
    void load(int k, const std::string& asset, int gen) override {
        FakeDeck& x = d[k];
        auto it = library.find(asset);
        x.playing = false;
        x.asset = nullptr;
        x.gen = 0;
        x.loopOn = false;
        x.pendingGen = gen;
        x.pendingAsset = it == library.end() ? nullptr : &it->second;
        x.readyAt = frames + loadFrames;
        ++x.loads;
    }
    int loaded(int k) override { return d[k].gen; }
    double position(int k) override { return d[k].pos; }
    void hint(int k, int slot, double sec, bool on) override {
        d[k].hints[slot] = on ? sec : -1.0;
    }
    void seek(int k, double sec) override {
        FakeDeck& x = d[k];
        if (x.playing && x.asset) { // 放着的时候定位：下一块里旧位置渐出、新位置渐入（同 Mixxx）
            x.xfade = true;
            x.oldPos = x.pos;
            // 没登记预读的远处：读盘线程还没读好，这一块是静音，下一块从 0 渐入（同 Mixxx 的 cache miss）
            bool cached = std::fabs(sec - x.pos) < 2.0;
            for (double h : x.hints) {
                cached = cached || (h >= 0.0 && std::fabs(sec - h) < 2.0);
            }
            x.miss = cached ? 0 : 2;
        }
        x.pos = sec;
        ++x.seeks;
    }
    void play(int k, bool on) override { d[k].playing = on && d[k].asset; }
    void rate(int k, double r) override { d[k].rate = r; }
    void loop(int k, double s0, double s1, bool on) override {
        if (on) {
            d[k].l0 = s0;
            d[k].l1 = s1;
        }
        d[k].loopOn = on;
    }
    void param(int k, int p, double v) override { d[k].params[p] = v; }
    void keylock(int k, bool on) override { d[k].keylock = on; }
    const float* channel(int k) override { return d[k].asset ? d[k].out.data() : nullptr; }
    double delayFrames() override { return kDelayFrames; }

    // 一个脉冲：拍上起音的 4 ms 衰减正弦；小节第 1 拍（网格拍号 % 4 == 0）幅度 0.8，其余 0.4
    static float click(const Asset& a, double pos) {
        if (a.wave == 1) { // 白噪声：位置的函数（同一个位置永远是同一个值，定位、重放都一样）
            if (pos < 0.0 || pos >= a.dur) {
                return 0.0f;
            }
            std::uint64_t h = static_cast<std::uint64_t>(std::llround(pos * kSampleRate)) * 0x9E3779B97F4A7C15ull;
            h ^= h >> 29;
            h *= 0xBF58476D1CE4E5B9ull;
            h ^= h >> 32;
            return static_cast<float>(0.435 * (static_cast<double>(h & 0xFFFFFF) / 8388607.5 - 1.0));
        }
        if (a.wave == 2) { // 扫频
            if (pos < 0.0 || pos >= a.dur) {
                return 0.0f;
            }
            const double T = 2.0, f0 = 40.0, k = std::log(16000.0 / f0) / T;
            const double t = pos - T * std::floor(pos / T);
            return static_cast<float>(0.355 * std::sin(2.0 * 3.14159265358979 * f0 * (std::exp(k * t) - 1.0) / k));
        }
        const double beat = (pos - a.first) * a.bpm / 60.0;
        const double k = std::floor(beat);
        const double dt = (beat - k) * 60.0 / a.bpm;
        if (pos < 0.0 || pos >= a.dur || dt >= kClickSec) {
            return 0.0f;
        }
        const double amp = (static_cast<long long>(k) % 4 == 0) ? 0.8 : 0.4;
        return static_cast<float>(amp * std::exp(-dt / 0.001) * std::cos(2.0 * 3.14159265358979 * 1000.0 * dt));
    }

    void process() {
        for (int k = 1; k <= nDecks; ++k) {
            FakeDeck& x = d[k];
            if (x.pendingGen && frames >= x.readyAt) {
                if (!x.pendingAsset || x.pendingAsset->fail) {
                    x.gen = -x.pendingGen;
                } else {
                    x.asset = x.pendingAsset;
                    x.gen = x.pendingGen;
                    x.pos = 0.0;
                }
                x.pendingGen = 0;
            }
            for (int n = 0; n < kBufferFrames; ++n) {
                float v = 0.0f;
                if (x.asset && x.playing) {
                    v = click(*x.asset, x.pos);
                    if (x.miss == 2) {
                        v = 0.0f;
                    } else if (x.miss == 1) {
                        v *= static_cast<float>(n) / kBufferFrames;
                    }
                    if (x.xfade) {
                        const float t = static_cast<float>(n) / kBufferFrames;
                        v = v * t + click(*x.asset, x.oldPos) * (1.0f - t);
                        x.oldPos += x.rate / kSampleRate;
                    }
                    if (!x.wasPlaying) {
                        v *= static_cast<float>(n) / kBufferFrames; // 起播那一块从 0 渐入
                    }
                    // EQ：只模拟「切到底」——LOW 切到底时小节第 1 拍的脉冲没了（测试看得见 EQ 生效在哪一拍）
                    v *= static_cast<float>(std::min(1.0, x.params[P_LO]));
                    x.pos += x.rate / kSampleRate;
                    if (x.loopOn && x.pos >= x.l1) {
                        x.pos -= x.l1 - x.l0;
                    }
                    if (x.pos >= x.asset->dur) {
                        x.playing = false;
                    }
                }
                x.delay.push_back(v);
                x.delay.push_back(v);
                x.out[static_cast<size_t>(2 * n)] = x.delay.front();
                x.delay.pop_front();
                x.out[static_cast<size_t>(2 * n + 1)] = x.delay.front();
                x.delay.pop_front();
            }
            x.wasPlaying = x.playing;
            x.xfade = false;
            if (x.miss > 0) {
                --x.miss;
            }
        }
        frames += kBufferFrames;
    }
};

std::mutex gOutLock;
void emitLine(const std::string& s) {
    std::lock_guard<std::mutex> lock(gOutLock);
    std::fputs(s.c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

} // namespace

int main() {
    FakeBackend be;
    if (const char* n = std::getenv("DJSTUDIO_DECKS")) {
        be.nDecks = std::max(1, std::min(kMaxDecks, std::atoi(n)));
    }
    Engine5 e5(be, kSampleRate, kBufferFrames, [](int sr) { return std::unique_ptr<Reverb>(new SimpleReverb(sr)); });
    double speed = 1.0;
    if (const char* s = std::getenv("DJSTUDIO_SPEED")) {
        speed = std::max(0.01, std::atof(s));
    }

    std::mutex inLock;
    std::deque<std::string> inbox;
    std::atomic<bool> eof{false};
    std::thread reader([&] {
        std::string line;
        while (std::getline(std::cin, line)) {
            std::lock_guard<std::mutex> lock(inLock);
            inbox.push_back(line);
        }
        eof = true;
    });
    reader.detach();

    std::FILE* rec = nullptr;
    long long recFrames = 0; (void)recFrames;
    std::vector<float> out(kBufferFrames * 2);
    bool quit = false;
    const auto t0 = std::chrono::steady_clock::now();
    double simSec = 0.0;      // 已经处理了多少（虚拟）秒
    double pausedWall = 0.0;  // 全场暂停着的墙钟秒数（不算进追赶）
    auto lastStatus = t0;
    auto wallSec = [&] { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
    double lastWall = 0.0;

    while (!quit && !eof) {
        std::deque<std::string> lines;
        {
            std::lock_guard<std::mutex> lock(inLock);
            lines.swap(inbox);
        }
        for (const std::string& line : lines) {
            if (line.empty() || line[0] != '#') {
                continue;
            }
            Json c, r = Json::object();
            try {
                c = Json::parse(line.substr(1));
            } catch (const std::exception& e) {
                emitLine(std::string("#{\"ok\":false,\"err\":\"") + e.what() + "\"}");
                continue;
            }
            const std::string cmd = c["cmd"].str("");
            if (cmd == "library") {
                Json tracks = Json::array();
                for (const Json& t : c["tracks"].arr()) {
                    Asset a;
                    a.id = t["id"].str("");
                    a.dur = t["dur"].num();
                    a.bpm = t["bpm"].num();
                    a.first = t["first_beat"].num();
                    a.fail = t["fail"].boolean();
                    be.library[a.id] = a;
                    Json o = Json::object();
                    o.set("id", a.id).set("duration", a.dur).set("samplerate", kSampleRate).set("bpm", a.bpm);
                    tracks.push(o);
                }
                r.set("ok", true).set("tracks", tracks);
            } else if (cmd == "quit") {
                quit = true;
                r.set("ok", true);
            } else if (cmd == "sim.record") { // 主输出写成 float32 立体声裸数据（测试里量脉冲落在第几帧）
                if (rec) {
                    std::fclose(rec);
                    rec = nullptr;
                }
                const std::string path = c["path"].str("");
                if (!path.empty()) {
                    rec = std::fopen(path.c_str(), "wb");
                    recFrames = 0;
                }
                r.set("ok", path.empty() || rec != nullptr).set("frame", e5.frame());
            } else if (cmd == "sim.set") { // 故障注入 / 调参
                if (c.has("load_ms")) {
                    be.loadFrames = static_cast<int>(c["load_ms"].num() * kSampleRate / 1000.0);
                }
                if (c.has("fail")) {
                    auto it = be.library.find(c["fail"].str(""));
                    if (it != be.library.end()) {
                        it->second.fail = c.has("on") ? c["on"].boolean() : true;
                    }
                }
                if (c.has("speed")) { // 换倍速：从现在的位置接着按新的倍速走（不等墙钟追上，也不猛追）
                    speed = std::max(0.01, c["speed"].num());
                    lastWall = wallSec();
                    pausedWall = lastWall - simSec / speed;
                }
                if (c.has("wave")) { // 换一份素材的声音：click / noise / sweep
                    auto it = be.library.find(c["asset"].str(""));
                    if (it != be.library.end()) {
                        const std::string wv = c["wave"].str("click");
                        it->second.wave = wv == "noise" ? 1 : (wv == "sweep" ? 2 : 0);
                    }
                }
                if (c.has("nudge")) { // 把一个 deck 的位置人为推开（验证位置核对）
                    be.d[static_cast<int>(c["deck"].num())].pos += c["nudge"].num();
                }
                r.set("ok", true);
            } else if (cmd == "sim.decks") { // 假 deck 内部状态
                Json a = Json::array();
                for (int k = 1; k <= be.nDecks; ++k) {
                    const FakeDeck& x = be.d[k];
                    Json o = Json::object();
                    o.set("deck", k).set("asset", x.asset ? Json(x.asset->id) : Json()).set("gen", x.gen);
                    o.set("pos", x.pos).set("playing", x.playing).set("rate", x.rate).set("loop", x.loopOn);
                    o.set("l0", x.l0).set("l1", x.l1).set("seeks", x.seeks).set("loads", x.loads);
                    o.set("keylock", x.keylock);
                    Json p = Json::object();
                    for (int i = 0; i < P_VOL; ++i) {
                        p.set(paramName(i), x.params[i]);
                    }
                    o.set("params", p);
                    a.push(o);
                }
                r.set("ok", true).set("decks", a).set("frames", be.frames);
            } else {
                r = e5.command(c);
            }
            r.set("_id", c["_id"]);
            emitLine("#" + r.dump());
        }

        // 走时钟：暂停着不走；预运行不看墙钟，尽快跑；其余按倍速追墙钟
        const double now = wallSec();
        if (e5.frozen()) {
            pausedWall += now - lastWall;
        }
        lastWall = now;
        int budget = 0;
        while (!e5.frozen() && (e5.prerunning() || simSec < (now - pausedWall) * speed) && budget < 4096) {
            e5.preProcess(false);
            be.process();
            e5.render(out.data());
            if (rec) {
                std::fwrite(out.data(), sizeof(float), out.size(), rec);
                recFrames += kBufferFrames;
            }
            simSec += static_cast<double>(kBufferFrames) / kSampleRate;
            ++budget;
        }
        if (e5.prerunning() || budget >= 4096) {
            // 预运行跑过的那段不算欠墙钟的账
            pausedWall = now - simSec / speed;
        }
        for (const std::string& m : e5.drain()) {
            emitLine("@" + m);
        }
        const auto tn = std::chrono::steady_clock::now();
        if (tn - lastStatus >= std::chrono::milliseconds(50)) {
            lastStatus = tn;
            emitLine("@" + e5.status().dump());
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if (rec) {
        std::fclose(rec);
    }
    return 0;
}
