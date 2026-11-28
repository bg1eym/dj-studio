// v5 原生调度器：按音频时钟（已经处理的采样帧）执行一份四 Deck 的执行计划（djplan/1）。
//
// 分工（和 v4 的「快的在引擎，慢的在 Python」一样）：Python 把谱编成全场拍上的片段 / 曲线 / 效果，演出前整份交给这里；
// 这里只做确定性的执行，不做任何决策。浏览器和 Python 不参与逐拍的实时调度。
//
// 两条线：
// - 控制步（controlStep，每块音频之前一次）：装载、定位、起停、loop / 跳段、速率、EQ / 滤波——都是 deck 的控件，
//   块边界上生效。同一步里的改动在同一块音频之前一起生效（外面用同一把锁 gate 把「控制步」和「处理一块音频」隔开）。
// - 混音（render，每块音频之后）：门（起播 / 停止，落在采样上）、推子、发送效果、主输出、电平——全部按帧算，
//   和控制步来得早晚无关。
//
// 起播 = 预卷 + 门：deck 提前 kPrerollBuffers 块开始放（位置按计划往回推，到起播那一帧正好读到起播点），
// 门在起播那一帧打开。这样起播落在采样上，Mixxx 起播时那一块的渐入（EnginePregain）也落在门外面。
// 四路同一拍起播 = 四个门在同一帧打开。
//
// deck 一律跟全场速度图：速率 = 全场 BPM / 曲子的网格 BPM；每块核对一次实际位置和计划位置（差值 = phase_ms），
// 小的差用微调速率拉回来（≤ ±0.4%），大的（> 25 ms）直接重新定位并报 resync。
//
// 不依赖 Qt / Mixxx：deck 和通道音频经 Backend 接口来（声音引擎里是 Mixxx 的 deck，模拟器里是假 deck）。
#pragma once

#include <cstdint>
#include <fstream>
#include <mutex>

#include "core.hpp"
#include "bus.hpp"
#include "fx.hpp"

namespace djs5 {

// 版本化的固定参数（改了要加 kFxVersion / 协议版本，并同步 docs/ENGINE5.md）
constexpr double kVolSmoothMs = 2.0; // 推子一下跳变时的平滑（去掉爆音）
constexpr double kMasterFadeMs = 10.0; // 全场暂停 / 继续的淡出淡入
constexpr int kPrerollBuffers = 4;
constexpr int kFxSlots = 8;            // 同时最多几个发送效果实例（含尾音）
constexpr int kHints = 8;              // 每个 deck 预读几个跳段目标
constexpr double kPhaseHardMs = 25.0;  // 位置差超过这么多：重新定位
constexpr double kPhaseCorrSec = 0.25; // 小的位置差在这么久里拉回来
constexpr double kPhaseMaxNudge = 0.004;
constexpr int kProtocol = 2;

// deck 后端：声音引擎里 = Mixxx 的 deck + EQ + 滤波；模拟器里 = 假 deck。
// 除 channel() 外都在控制步里调（和「处理一块音频」互斥）。位置单位 = 原曲的秒。
struct Backend {
    virtual ~Backend() = default;
    virtual int decks() = 0;
    virtual void load(int deck, const std::string& asset, int gen) = 0; // 异步；之后看 loaded()
    virtual int loaded(int deck) = 0;       // 装好的代数；0 = 没有 / 还在装；负数 = 那一代装载失败
    virtual double position(int deck) = 0;  // 下一块音频开头时的位置（秒）
    virtual void seek(int deck, double sec) = 0;
    virtual void play(int deck, bool on) = 0;
    virtual void rate(int deck, double ratio) = 0;
    virtual void loop(int deck, double s0, double s1, bool on) = 0; // on = false：只关 loop，位置不动
    virtual void param(int deck, int p, double v) = 0;              // P_LO / P_MID / P_HI / P_FILTER（Mixxx 原生值）
    virtual void keylock(int deck, bool on) = 0;
    // 预读点：告诉 deck 等会儿要跳到原曲的这里，先把那一段读进缓存（slot = 0..kHints-1；on = false 清掉）。
    // 没预读的位置一跳过去，读盘线程还没读好，那一块是静音、下一块从 0 渐入（Mixxx：readaheadmanager.cpp:93-113）
    virtual void hint(int deck, int slot, double sec, bool on) = 0;
    virtual const float* channel(int deck) = 0; // 刚处理完的这一块：推子前（EQ / 滤波之后），交错立体声；没装歌 = nullptr
    virtual double delayFrames() = 0;           // 通道音频比 deck 位置晚多少帧（EQ 群延迟）
};

inline double dbfs(double ms) {
    return ms > 1e-10 ? 10.0 * std::log10(ms) : -100.0;
}

class Engine5 {
  public:
    enum State { Idle, Preparing, Prerun, Running, Pausing, Paused, Stopping, Done };

    Engine5(Backend& be, int sampleRate, int bufferFrames, ReverbFactory reverb)
            : m_be(be), m_sr(sampleRate), m_n(bufferFrames), m_reverbFactory(std::move(reverb)) {
        m_decks = std::min(kMaxDecks, m_be.decks());
        m_post.assign(static_cast<size_t>(kMaxDecks + 1), std::vector<float>(static_cast<size_t>(m_n) * 2, 0.0f));
        m_wet.assign(static_cast<size_t>(m_n) * 2, 0.0f);
        m_fxIn.assign(static_cast<size_t>(m_n) * 2, 0.0f);
        for (auto& s : m_slots) {
            s.delay.init(m_sr);
        }
        const size_t sz = static_cast<size_t>(m_n) * 2;
        m_u.assign(static_cast<size_t>(kMaxDecks + 1), std::vector<float>(sz, 0.0f));
        m_v.assign(static_cast<size_t>(kMaxDecks + 1), std::vector<float>(sz, 0.0f));
        m_vd.assign(static_cast<size_t>(kMaxDecks + 1), std::vector<float>(static_cast<size_t>(m_n), 0.0f));
        m_busIn.assign(sz, 0.0f);
        m_busOut.assign(sz, 0.0f);
        m_zero.assign(sz, 0.0f);
        m_sampler.init(m_sr);
        m_limiter.init(m_sr);
        BandSplit3 probe;
        probe.init(m_sr);
        m_splitLatency = probe.latency();
    }

    std::recursive_mutex& gate() { return m_gate; }
    State state() const { return m_state; }
    const char* stateName() const {
        static const char* names[] = {"idle", "preparing", "prerun", "running", "pausing", "paused", "stopping", "done"};
        return names[m_state];
    }
    // 全场暂停着：外面不要处理 deck，也不要调 render（deck 和效果原地冻住）
    bool frozen() const { return m_state == Paused; }
    // 静音预运行（定位到中间时重建效果的历史）：外面不出声，尽快往前处理
    bool prerunning() const { return m_state == Prerun; }
    long long frame() const { return m_frame; }
    int lateSteps() const { return m_lateSteps; }
    // 主输出的限幅器（dsp.hpp MasterLimiter）：声音引擎默认开；模拟器默认关（测试里量的是没压过的电平），要测它时用 v5.limiter 打开
    void setLimiter(bool on) {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        m_limit = on;
        m_limiter.reset();
    }

    // ------------------------------------------------------------ 命令（主线程）

    Json command(const Json& c) {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        Json r = Json::object();
        const std::string cmd = c["cmd"].str("");
        try {
            if (cmd == "v5.caps") {
                r = caps();
            } else if (cmd == "v5.plan") {
                loadPlan(c["plan"]);
                r.set("exec", m_plan->exec);
                r.set("latency", m_latA + m_latM); // 这份计划的输出比 deck 晚多少帧（用到分频才有）
            } else if (cmd == "v5.start") {
                start(c["beat"].num(), c.has("prerun") ? c["prerun"].num() : c["beat"].num());
            } else if (cmd == "v5.pause") {
                if (m_state == Running) {
                    m_state = Pausing;
                    m_masterTarget = 0.0f;
                    m_masterStep = masterStep(kMasterFadeMs);
                }
            } else if (cmd == "v5.resume") {
                if (m_state == Paused || m_state == Pausing) {
                    m_state = Running;
                    m_masterTarget = 1.0f;
                    m_masterStep = masterStep(kMasterFadeMs);
                    event("resumed");
                }
            } else if (cmd == "v5.stop") {
                stop(c.has("fade_ms") ? c["fade_ms"].num() : 150.0);
            } else if (cmd == "v5.limiter") {
                m_limit = c["on"].num(1.0) != 0.0;
                m_limiter.reset();
                r.set("on", m_limit);
            } else if (cmd == "v5.takeover") {
                takeover(static_cast<int>(c["deck"].num()), c["param"].str(""), c["value"].num());
            } else if (cmd == "v5.return") {
                giveBack(static_cast<int>(c["deck"].num()), c["param"].str(""), c["when"].str("beat"),
                        c.has("ramp") ? c["ramp"].num() : 1.0);
            } else if (cmd == "v5.state") {
                r = status();
            } else {
                throw std::runtime_error("unknown command " + cmd);
            }
            r.set("ok", true);
        } catch (const std::exception& e) {
            r = Json::object();
            r.set("ok", false);
            r.set("err", std::string(e.what()));
        }
        return r;
    }

    Json caps() const {
        Json r = Json::object();
        r.set("schema", "djcaps/1");
        r.set("protocol", kProtocol);
        r.set("plan", kPlanSchema);
        r.set("max_decks", m_decks);
        r.set("sample_rate", m_sr);
        r.set("buffer_frames", m_n);
        Json fx = Json::object();
        fx.set("delay", kFxVersion).set("echo", kFxVersion).set("reverb", kFxVersion);
        // 现场效果（处理组里的件）：版本 = DSP 的版本 + 组里的顺序 / 时间常数的版本
        const int live = kDspVersion * 100 + kBusVersion;
        for (int k = 0; k < U_COUNT; ++k) {
            fx.set(k == U_ECHO ? "insert_echo" : unitKindName(k), live);
        }
        fx.set("sample", live);
        r.set("fx", fx);
        r.set("fx_slots", kFxSlots);
        r.set("routes", Json::array().push("send").push("insert").push("group").push("master"));
        r.set("buses", kBuses);
        Json split = Json::object();
        split.set("low_hz", 250.0).set("high_hz", 2500.0).set("latency_frames", m_splitLatency);
        r.set("split", split);
        Json timing = Json::object();
        timing.set("gate", "sample").set("deck_control", "buffer").set("preroll_buffers", kPrerollBuffers);
        timing.set("gate_in_ms", kGateInMs).set("gate_out_ms", kGateOutMs);
        r.set("timing", timing);
        Json restore = Json::object();
        restore.set("prerun", true).set("max_beats", 256);
        r.set("restore", restore);
        r.set("slip_loop", false);
        r.set("keylock_compensated", false);
        r.set("sampler", static_cast<int>(Sampler::kSlots));       // 一份计划最多几个采样
        r.set("sampler_voices", static_cast<int>(Sampler::kVoices)); // 同时最多几个在响
        r.set("protect", false);
        return r;
    }

    // 给主机的消息（回执、事件）：外面取走后一行一条发出去
    std::vector<std::string> drain() {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        std::vector<std::string> out;
        out.swap(m_out);
        return out;
    }

    Json status() {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        Json s = Json::object();
        s.set("t", "st5");
        s.set("exec", m_plan ? m_plan->exec : std::string());
        s.set("seq", static_cast<long long>(++m_seq));
        s.set("state", stateName());
        s.set("frame", m_frame);
        const bool timed = m_plan && m_state != Idle && m_state != Preparing;
        const double g = timed ? beatAtFrame(static_cast<double>(m_frame)) : m_startBeat;
        s.set("beat", g);
        s.set("bpm", m_plan ? m_plan->tempo.bpmAt(g) : 0.0);
        s.set("late", m_lateSteps);
        Json decks = Json::array();
        for (int d = 1; d <= m_decks; ++d) {
            const DeckRt& rt = m_d[d];
            const Clip* c = clipOf(d);
            Json o = Json::object();
            o.set("deck_id", d);
            o.set("clip_id", c && rt.st != DIdle ? Json(c->id) : Json());
            o.set("next_clip", c && rt.st == DIdle ? Json(c->id) : Json());
            o.set("load_generation", m_be.loaded(d));
            const bool looping = c && rt.st == DPlaying && c->segs[static_cast<size_t>(rt.seg)].loop > 0.0;
            static const char* names[] = {"idle", "loading", "ready", "playing", "stopped", "error"};
            o.set("state", looping ? "looping" : names[rt.st]);
            o.set("source_sec", m_be.position(d));
            o.set("global_beat", g);
            o.set("phase_ms", rt.phaseMs);
            if (c && rt.st == DPlaying) {
                const Seg& sg = c->segs[static_cast<size_t>(rt.seg)];
                o.set("seg", sg.id);
                if (sg.loop > 0.0) {
                    o.set("loop_lap", std::floor((g - sg.g + (sg.src - sg.lsrc) / c->spb()) / sg.loop) + 1.0);
                }
            }
            Json ctl = Json::object(), man = Json::array();
            for (int p = 0; p < P_COUNT; ++p) {
                ctl.set(paramName(p), p == P_VOL ? static_cast<double>(rt.volSm)
                                                 : (rt.have[p] ? rt.applied[p] : paramNeutral(p)));
                if (rt.manual[p]) {
                    man.push(paramName(p));
                }
            }
            o.set("controls", ctl);
            o.set("manual", man);
            o.set("gate", static_cast<double>(rt.gate));
            o.set("level", dbfs(rt.preMs));
            o.set("out", dbfs(rt.postMs));
            // 「出不出声」看的是这一路开没开（在放、门开着、推子没拉到底、没有被 Release 收成静音），不看这一刻信号有多大：
            // 曲子自己安静的地方不算「没出声」
            bool muted = false;
            if (c && m_plan) {
                const int ci = m_plan->clipIndex.at(c->id);
                for (const auto& b : m_buses) {
                    const auto& ids = b->spec().clipIdx;
                    muted = muted || (b->muted() && std::find(ids.begin(), ids.end(), ci) != ids.end());
                }
            }
            o.set("audible", c != nullptr && rt.st == DPlaying && rt.gate > 0.5f && rt.volSm > 1e-3f && !muted);
            o.set("muted", muted);
            decks.push(o);
        }
        s.set("decks", decks);
        Json fx = Json::array();
        for (int k = 0; k < kFxSlots; ++k) {
            const Slot& sl = m_slots[k];
            if (sl.fx < 0 || !m_plan) {
                continue;
            }
            const FxSpec& f = m_plan->fx[static_cast<size_t>(sl.fx)];
            Json o = Json::object();
            o.set("id", f.id).set("kind", f.kind).set("clip", f.clip).set("slot", k);
            o.set("send", static_cast<double>(sl.prevSend)).set("wet", static_cast<double>(sl.prevWet));
            o.set("tail", sl.prevSend <= 0.0f); // 送出已经关了（或者源片段停了），只剩尾音
            o.set("level", dbfs(sl.ms));
            fx.push(o);
        }
        if (timed) {
            const double lat = m_be.delayFrames();
            const double gU = beatAtFrame(static_cast<double>(m_frame) - lat);
            const double dg = beatAtFrame(static_cast<double>(m_frame) - lat + 1.0) - gU;
            for (const auto& b : m_buses) {
                const bool isM = b->spec().route == R_MASTER;
                b->report(gU - (m_latA + (isM ? m_latM : 0)) * dg, fx);
            }
        }
        s.set("fx", fx);
        s.set("latency", m_latA + m_latM);
        Json master = Json::object();
        master.set("level", dbfs(m_masterMs)).set("peak", m_masterPeak > 1e-5 ? 20.0 * std::log10(m_masterPeak) : -100.0);
        master.set("over", m_over).set("over_now", m_overHold > 0);
        // 限幅器：开没开、最近压了多少 dB（0 = 没在压）。over 数的是进限幅器之前超过满刻度的采样——开着限幅器时它们被压住了，没有削波
        master.set("limiter", m_limit).set("limit_db", m_limitLow < 0.9999 ? 20.0 * std::log10(m_limitLow) : 0.0);
        s.set("master", master);
        return s;
    }

    // ------------------------------------------------------------ 控制步（每块音频之前一次）

    void controlStep() {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        if (m_stepped && m_steppedFrame == m_frame) {
            return;
        }
        m_stepped = true;
        m_steppedFrame = m_frame;
        if (!m_plan) {
            return;
        }
        if (m_state == Preparing) {
            stepPreparing();
        }
        if (m_state == Prerun || m_state == Running || m_state == Pausing || m_state == Stopping) {
            stepRunning();
        }
    }
    // 处理一块音频之前调（拿着 gate）：控制步还没跑（主线程卡了一下）就在这里补上。countLate：出声时算一次「晚了」
    void preProcess(bool countLate) {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        if (!(m_stepped && m_steppedFrame == m_frame)) {
            if (countLate && m_plan && (m_state == Running || m_state == Pausing || m_state == Stopping)) {
                ++m_lateSteps;
            }
            controlStep();
        }
    }

    // ------------------------------------------------------------ 混音（每块音频之后；拿着 gate）

    void render(float* out) {
        std::lock_guard<std::recursive_mutex> lock(m_gate);
        const int N = m_n;
        std::fill(out, out + 2 * N, 0.0f);
        const bool timed = m_plan && (m_state == Prerun || m_state == Running || m_state == Pausing || m_state == Stopping);
        const double a300 = 1.0 - std::exp(-static_cast<double>(N) / m_sr / 0.3);
        if (!timed) {
            for (int d = 1; d <= m_decks; ++d) {
                m_d[d].preMs += (0.0 - m_d[d].preMs) * a300;
                m_d[d].postMs += (0.0 - m_d[d].postMs) * a300;
            }
            m_masterMs += (0.0 - m_masterMs) * a300;
            m_frame += N;
            return;
        }
        const Plan& pl = *m_plan;
        const double lat = m_be.delayFrames();
        const double g0 = beatAtFrame(static_cast<double>(m_frame) - lat);
        const double g1 = beatAtFrame(static_cast<double>(m_frame + N) - lat);
        const double dgs = (g1 - g0) / N;
        const double bpm = pl.tempo.bpmAt(0.5 * (g0 + g1));
        const double gateIn = kGateInMs / 1000.0 * bpm / 60.0; // 拍
        const double gateOut = kGateOutMs / 1000.0 * bpm / 60.0;
        const float upStep = static_cast<float>(1000.0 / (kGateInMs * m_sr));
        const float downStep = static_cast<float>(1000.0 / (kGateOutMs * m_sr));
        const float volA = static_cast<float>(1.0 - std::exp(-1000.0 / (kVolSmoothMs * m_sr)));

        // 三条时间轴：deck 出来的声音（g0 起）→ 过了片段 / 组这一级（晚 m_latA 帧）→ 过了总输出这一级（再晚 m_latM 帧）。
        // 计划里没有分频的件时两个延迟都是 0，三条轴是同一条。
        const double gD0 = g0 - m_latA * dgs, gD1 = g1 - m_latA * dgs;
        const double gM0 = gD0 - m_latM * dgs;
        const size_t bytes = static_cast<size_t>(N) * 2 * sizeof(float);

        // 1. 每台 deck：门、推子（落在采样上）→ u = 过了门、没过推子；v = 过了推子；vd = 晚 m_latA 帧的推子
        for (int d = 1; d <= m_decks; ++d) {
            DeckRt& rt = m_d[d];
            const Clip* c = clipOf(d);
            const float* ch = m_be.channel(d);
            float* u = m_u[static_cast<size_t>(d)].data();
            float* v = m_v[static_cast<size_t>(d)].data();
            float* vd = m_vd[static_cast<size_t>(d)].data();
            std::vector<float>& ring = m_volRing[d];
            size_t& rp = m_volPos[d];
            const bool can = c && ch && rt.st == DPlaying;
            // 不用无穷大 / NaN 当「没有」：声音引擎跟着 Mixxx 用 -ffast-math 编，那样的比较会被优化掉
            const double openFrom = can ? rt.effStart - gateIn : 1e300;
            const double closeAt = can ? c->stop : -1e300;
            double pre = 0.0;
            for (int n = 0; n < N; ++n) {
                const double g = g0 + n * dgs;
                const float target = (g >= openFrom && g < closeAt - gateOut) ? 1.0f : 0.0f;
                if (rt.gate < target) {
                    rt.gate = std::min(target, rt.gate + upStep);
                } else if (rt.gate > target) {
                    rt.gate = std::max(target, rt.gate - downStep);
                }
                const float vt = can ? static_cast<float>(control(rt, c, P_VOL, g)) : 0.0f;
                if (rt.gate <= 0.0f) {
                    rt.volSm = vt; // 门关着听不见：推子直接摆到位（起播那一帧推子已经在谱上的位置，不软掉第一下）
                } else {
                    rt.volSm += (vt - rt.volSm) * volA;
                }
                const float l = ch ? ch[2 * n] : 0.0f, r = ch ? ch[2 * n + 1] : 0.0f;
                u[2 * n] = l * rt.gate;
                u[2 * n + 1] = r * rt.gate;
                v[2 * n] = u[2 * n] * rt.volSm;
                v[2 * n + 1] = u[2 * n + 1] * rt.volSm;
                if (m_latA > 0) {
                    vd[n] = ring[rp];
                    ring[rp] = rt.volSm;
                    rp = rp + 1 < ring.size() ? rp + 1 : 0;
                } else {
                    vd[n] = rt.volSm;
                }
                pre += static_cast<double>(l) * l + static_cast<double>(r) * r;
            }
            rt.preMs += ((ch && rt.st == DPlaying ? pre / (2.0 * N) : 0.0) - rt.preMs) * a300;
        }

        // 2. 片段 / 组这一级。默认的路：v 过一条等长的延迟线。插入组：u 进组，出来再乘（晚了同样多的）推子。
        //    组插入：成员的 v 加起来进组，成员不再各自直通（它们的发送照旧取各自延迟后的推子后信号）。
        //    组提前喂（延迟线 / 分频器里先有历史），喂热了、到了窗口边上才把这一路换过去：换的那一刻两边出的是同一个信号。
        bool viaInsert[kMaxDecks + 1] = {}, viaGroup[kMaxDecks + 1] = {};
        const double armB = kArmBuffers * N * dgs, liveB = kLiveBuffers * N * dgs;
        for (auto& bp : m_buses) {
            BusRt& bus = *bp;
            const BusSpec& bs = bus.spec();
            if (bs.route == R_MASTER) {
                continue;
            }
            if (!bus.wants(gD0, gD1, armB)) {
                if (bus.fed() > 0) {
                    bus.reset();
                }
                continue;
            }
            if (bus.fed() == 0 && m_runBuffers == 0) {
                bus.markSinceStart();
            }
            // 只收自己的目标片段：同一台 deck 上别的片段进不来
            bool anyIn = false;
            float* in = m_busIn.data();
            std::fill(in, in + 2 * N, 0.0f);
            for (int ci : bs.clipIdx) {
                const Clip& c = pl.clips[static_cast<size_t>(ci)];
                if (clipOf(c.deck) != &c || m_d[c.deck].st != DPlaying) {
                    continue;
                }
                const float* src = (bs.route == R_INSERT ? m_u : m_v)[static_cast<size_t>(c.deck)].data();
                for (int i = 0; i < 2 * N; ++i) {
                    in[i] += src[i];
                }
                anyIn = true;
            }
            (void)anyIn;
            bus.process(in, m_busOut.data(), gD0, dgs, bpm);
            if (!(bus.warm() && bus.wants(gD0, gD1, liveB))) {
                continue;
            }
            if (bs.route == R_INSERT) {
                const Clip& c = pl.clips[static_cast<size_t>(bs.clipIdx[0])];
                if (clipOf(c.deck) == &c && !viaInsert[c.deck] && !viaGroup[c.deck]) {
                    viaInsert[c.deck] = true;
                    float* post = m_post[static_cast<size_t>(c.deck)].data();
                    const float* vd = m_vd[static_cast<size_t>(c.deck)].data();
                    for (int n = 0; n < N; ++n) {
                        post[2 * n] = m_busOut[static_cast<size_t>(2 * n)] * vd[n];
                        post[2 * n + 1] = m_busOut[static_cast<size_t>(2 * n + 1)] * vd[n];
                    }
                }
            } else {
                for (int ci : bs.clipIdx) {
                    const Clip& c = pl.clips[static_cast<size_t>(ci)];
                    if (clipOf(c.deck) == &c && !viaInsert[c.deck]) {
                        viaGroup[c.deck] = true;
                    }
                }
                for (int i = 0; i < 2 * N; ++i) {
                    out[i] += m_busOut[static_cast<size_t>(i)];
                }
            }
        }
        for (int d = 1; d <= m_decks; ++d) {
            DeckRt& rt = m_d[d];
            float* post = m_post[static_cast<size_t>(d)].data();
            // 默认的那条延迟线一直走着（哪怕这一块走的是组）：换回来的时候里面的历史是对的
            if (m_latA > 0) {
                m_alignD[d].process(m_v[static_cast<size_t>(d)].data(), viaInsert[d] ? m_busIn.data() : post, N);
            } else if (!viaInsert[d]) {
                std::memcpy(post, m_v[static_cast<size_t>(d)].data(), bytes);
            }
            double pst = 0.0;
            for (int i = 0; i < 2 * N; ++i) {
                pst += static_cast<double>(post[i]) * post[i];
            }
            if (!viaGroup[d]) {
                for (int i = 0; i < 2 * N; ++i) {
                    out[i] += post[i];
                }
            }
            rt.postMs += (pst / (2.0 * N) - rt.postMs) * a300;
        }

        // 3. 发送效果：取这一级之后的推子后信号；送出的开 / 关也落在采样上
        renderFx(out, gD0, gD1, bpm, a300);

        // 4. 采样：到点触发（落在采样上），直接进主混音
        while (m_hitNext < pl.hits.size() && pl.hits[m_hitNext].beat < gD1) {
            const HitSpec& h = pl.hits[m_hitNext];
            const int off = std::min(N - 1, std::max(0, static_cast<int>(std::ceil((h.beat - gD0) / dgs - 1e-6))));
            if (h.stop) {
                m_sampler.stopAll(off);
            } else {
                m_sampler.trigger(h.slot, off, static_cast<float>(h.gain));
                if (!h.id.empty() && m_state != Prerun) {
                    receipt(h.id, "ok", nullptr, h.beat, gD0 + off * dgs);
                }
            }
            ++m_hitNext;
        }
        m_sampler.process(out, N);

        // 处理组里的件到点了：回执（目标片段没在放的 = 跳过）
        while (m_markNext < m_marks.size() && m_marks[m_markNext].beat < gD1) {
            const Mark& mk = m_marks[m_markNext];
            if (m_state != Prerun) {
                const BusSpec& bs = pl.buses[static_cast<size_t>(mk.bus)];
                bool playing = bs.route == R_MASTER;
                for (int ci : bs.clipIdx) {
                    const Clip& c = pl.clips[static_cast<size_t>(ci)];
                    playing = playing || (clipOf(c.deck) == &c && m_d[c.deck].st == DPlaying);
                }
                receipt(mk.id, playing ? "ok" : "skipped", nullptr, mk.beat, mk.beat, playing ? "" : "目标片段没在放");
            }
            ++m_markNext;
        }

        // 5. 总输出这一级：整个主混音（含效果返回、采样）进总输出的处理组；没有的时候过等长的延迟线
        {
            bool viaMaster = false;
            for (auto& bp : m_buses) {
                BusRt& bus = *bp;
                if (bus.spec().route != R_MASTER) {
                    continue;
                }
                const double gM1 = gM0 + N * dgs;
                if (!bus.wants(gM0, gM1, armB)) {
                    if (bus.fed() > 0) {
                        bus.reset();
                    }
                    continue;
                }
                if (bus.fed() == 0 && m_runBuffers == 0) {
                    bus.markSinceStart();
                }
                bus.process(out, m_busOut.data(), gM0, dgs, bpm);
                if (!viaMaster && bus.warm() && bus.wants(gM0, gM1, liveB)) {
                    viaMaster = true;
                    std::memcpy(m_busIn.data(), m_busOut.data(), bytes);
                }
            }
            if (m_latM > 0) {
                m_alignM.process(out, m_busOut.data(), N);
                std::memcpy(out, viaMaster ? m_busIn.data() : m_busOut.data(), bytes);
            } else if (viaMaster) {
                std::memcpy(out, m_busIn.data(), bytes);
            }
        }
        ++m_runBuffers;

        // 主输出：开演 / 定位后到「听得见」的那一拍才放出来；全场暂停 / 停止时淡出
        double sum = 0.0;
        float peak = 0.0f;
        int over = 0;
        for (int n = 0; n < N; ++n) {
            const double g = gM0 + n * dgs;
            const float target = g >= m_audibleBeat - gateIn ? m_masterTarget : 0.0f;
            if (m_master < target) {
                m_master = std::min(target, m_master + m_masterStep);
            } else if (m_master > target) {
                m_master = std::max(target, m_master - m_masterStep);
            }
            float l = out[2 * n] * m_master, r = out[2 * n + 1] * m_master;
            out[2 * n] = l;
            out[2 * n + 1] = r;
            const float al = std::fabs(l), ar = std::fabs(r);
            peak = std::max(peak, std::max(al, ar));
            if (al > 1.0f || ar > 1.0f) {
                ++over;
            }
            sum += static_cast<double>(l) * l + static_cast<double>(r) * r;
        }
        m_masterMs += (sum / (2.0 * N) - m_masterMs) * a300;
        m_masterPeak = std::max(static_cast<double>(peak), m_masterPeak * std::pow(10.0, -static_cast<double>(N) / m_sr));
        // 最后一级：限幅（电平表、过载计数量的是限幅之前的——看得出这套谱排得满不满）
        if (m_limit) {
            const double low = m_limiter.process(out, N);
            m_limitLow = std::min(low, m_limitLow + (1.0 - m_limitLow) * a300);
        } else {
            m_limitLow = 1.0;
        }
        if (over > 0) {
            m_over += over;
            m_overHold = static_cast<int>(1.5 * m_sr / N); // 过载指示亮 1.5 秒
        } else if (m_overHold > 0) {
            --m_overHold;
        }
        m_frame += N;

        if (m_state == Pausing && m_master <= 0.0f) {
            m_state = Paused;
            event("paused");
        } else if (m_state == Stopping && m_master <= 0.0f) {
            halt();
            m_state = Idle;
            event("stopped");
        }
    }

  private:
    enum DeckSt { DIdle = 0, DLoading, DReady, DPlaying, DDone, DFailed };

    struct DeckRt {
        size_t pos = 0; // 这个 deck 上第几个片段
        DeckSt st = DIdle;
        int seg = 0;
        int settle = 0;
        double effStart = 0.0;
        double phaseMs = 0.0;
        double applied[P_COUNT];
        bool have[P_COUNT]; // applied 里是不是已经写给 deck 的值（刚装好 = 还没有）
        bool manual[P_COUNT];
        double manualVal[P_COUNT];
        bool retOn[P_COUNT];
        double retG0[P_COUNT], retG1[P_COUNT];
        float gate = 0.0f, volSm = 0.0f;
        double preMs = 0.0, postMs = 0.0;
        DeckRt() {
            for (int p = 0; p < P_COUNT; ++p) {
                applied[p] = 0.0;
                have[p] = manual[p] = retOn[p] = false;
                manualVal[p] = retG0[p] = retG1[p] = 0.0;
            }
        }
    };
    struct Slot {
        int fx = -1;
        DelayLine delay;
        std::unique_ptr<Reverb> reverb;
        float prevSend = 0.0f, prevWet = 0.0f;
        float gate = 0.0f; // 送出的门（0..1）
        double ms = 0.0;
    };

    // ---------------------------------------------------------------- 时钟

    double beatAtFrame(double f) const {
        return m_plan->tempo.beatAt(m_originSec + (f - static_cast<double>(m_originFrame)) / m_sr);
    }
    double frameAtBeat(double g) const {
        return static_cast<double>(m_originFrame) + (m_plan->tempo.secAt(g) - m_originSec) * m_sr;
    }
    float masterStep(double ms) const { return static_cast<float>(1000.0 / (std::max(0.5, ms) * m_sr)); }

    const Clip* clipOf(int d) const {
        if (!m_plan) {
            return nullptr;
        }
        const auto& v = m_plan->byDeck[d];
        return m_d[d].pos < v.size() ? &m_plan->clips[static_cast<size_t>(v[m_d[d].pos])] : nullptr;
    }

    // 一个旋钮现在该在哪：谱上的值；被手动接管了 = 手上的值；正在回谱 = 从手上的值走一条短斜线到那一刻的谱值
    static double control(const DeckRt& rt, const Clip* c, int p, double g) {
        const double score = c ? c->value(p, g) : (p == P_VOL ? 0.0 : paramNeutral(p));
        if (!rt.manual[p]) {
            return score;
        }
        if (rt.retOn[p] && g >= rt.retG0[p]) {
            const double k = rt.retG1[p] > rt.retG0[p] ? (g - rt.retG0[p]) / (rt.retG1[p] - rt.retG0[p]) : 1.0;
            return rt.manualVal[p] + (score - rt.manualVal[p]) * std::min(1.0, k);
        }
        return rt.manualVal[p];
    }

    // ---------------------------------------------------------------- 消息

    void receipt(const std::string& id, const char* st, const Clip* c, double planned, double actual, const std::string& msg = "") {
        Json r = Json::object();
        r.set("t", "rc").set("exec", m_plan ? m_plan->exec : std::string()).set("seq", static_cast<long long>(++m_seq));
        r.set("id", id).set("st", st);
        if (c) {
            r.set("deck", c->deck).set("clip", c->id).set("gen", c->gen);
        }
        r.set("beat", planned).set("at", actual).set("frame", m_frame);
        if (!msg.empty()) {
            r.set("msg", msg);
        }
        m_out.push_back(r.dump());
    }
    void event(const char* e, const std::string& msg = "") {
        Json r = Json::object();
        r.set("t", "ev5").set("exec", m_plan ? m_plan->exec : std::string()).set("seq", static_cast<long long>(++m_seq));
        r.set("e", e).set("frame", m_frame);
        if (m_plan && m_state != Idle && m_state != Preparing) {
            r.set("beat", beatAtFrame(static_cast<double>(m_frame)));
        }
        if (!msg.empty()) {
            r.set("msg", msg);
        }
        m_out.push_back(r.dump());
    }

    // ---------------------------------------------------------------- 计划与走带

    void loadPlan(const Json& j) {
        if (m_state != Idle && m_state != Done) {
            throw std::runtime_error("正在演出，先停下再换计划");
        }
        auto pl = std::make_shared<Plan>(Plan::parse(j, m_decks, kFxSlots));
        for (const FxSpec& f : pl->fx) {
            Slot& s = m_slots[f.slot];
            if (f.kind == "reverb" && !s.reverb) {
                s.reverb = m_reverbFactory(m_sr);
            }
        }
        // 处理组：这里分配（不在音频线程里）。有分频的计划，这一级所有的路都延迟同样的帧数（见 bus.hpp）
        m_buses.clear();
        m_latA = pl->splitA ? m_splitLatency : 0;
        m_latM = pl->splitM ? m_splitLatency : 0;
        for (const BusSpec& bs : pl->buses) {
            std::unique_ptr<BusRt> rt(new BusRt());
            rt->init(bs, m_sr, m_n, bs.route == R_MASTER ? m_latM : m_latA);
            m_buses.push_back(std::move(rt));
        }
        for (int d = 0; d <= kMaxDecks; ++d) {
            m_alignD[d].init(m_latA);
            m_volRing[d].assign(static_cast<size_t>(std::max(1, m_latA)), 0.0f);
            m_volPos[d] = 0;
        }
        m_alignM.init(m_latM);
        // 采样：Python 那边解码成引擎采样率的 float32 立体声裸数据
        for (int k = 0; k < Sampler::kSlots; ++k) {
            m_sampler.load(k, nullptr, 0);
        }
        for (size_t k = 0; k < pl->samples.size(); ++k) {
            const SampleSpec& sm = pl->samples[k];
            std::ifstream f(sm.path, std::ios::binary);
            std::vector<float> data(static_cast<size_t>(std::max(0, sm.frames)) * 2, 0.0f);
            if (!f || data.empty() || !f.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(data.size() * sizeof(float)))) {
                throw std::runtime_error("采样 " + sm.id + " 读不了：" + sm.path);
            }
            m_sampler.load(static_cast<int>(k), data.data(), sm.frames);
        }
        m_marks.clear();
        for (size_t i = 0; i < pl->buses.size(); ++i) {
            for (const UnitSpec& u : pl->buses[i].units) {
                m_marks.push_back({u.start, u.id, static_cast<int>(i)});
            }
        }
        std::sort(m_marks.begin(), m_marks.end(), [](const Mark& a, const Mark& b2) { return a.beat < b2.beat; });
        m_plan = pl;
        m_state = Idle;
    }

    void halt() {
        for (int d = 1; d <= m_decks; ++d) {
            m_be.play(d, false);
            m_be.loop(d, 0.0, 0.0, false);
            DeckRt& rt = m_d[d];
            rt.st = DIdle;
            rt.gate = 0.0f;
            rt.volSm = 0.0f;
        }
        for (auto& s : m_slots) {
            if (s.fx >= 0) {
                s.delay.reset();
                if (s.reverb) {
                    s.reverb->reset();
                }
            }
            s.fx = -1;
            s.prevSend = s.prevWet = 0.0f;
            s.ms = 0.0;
        }
        for (auto& b : m_buses) {
            b->reset();
        }
        for (int d = 0; d <= kMaxDecks; ++d) {
            m_alignD[d].reset();
            std::fill(m_volRing[d].begin(), m_volRing[d].end(), 0.0f);
            m_volPos[d] = 0;
        }
        m_alignM.reset();
        m_sampler.reset();
        m_runBuffers = 0;
        m_master = 0.0f;
    }

    // 从全场第 beat 拍开始（听得见的地方）；prerun < beat：先从 prerun 静音跑到 beat，把效果的历史补出来。
    // 跳转 / 重新开始会结束所有手动接管，旋钮回到那一拍谱上的位置。
    void start(double beat, double prerun) {
        if (!m_plan) {
            throw std::runtime_error("还没有执行计划");
        }
        if (m_state != Idle && m_state != Done && m_state != Paused) {
            throw std::runtime_error("正在演出，先停下");
        }
        halt();
        const Plan& pl = *m_plan;
        m_audibleBeat = beat;
        m_startBeat = std::min(beat, prerun);
        for (int d = 1; d <= m_decks; ++d) {
            DeckRt fresh;
            const auto& v = pl.byDeck[d];
            size_t pos = 0;
            while (pos < v.size() && pl.clips[static_cast<size_t>(v[pos])].stop <= m_startBeat) {
                ++pos;
            }
            fresh.pos = pos;
            m_d[d] = fresh;
        }
        m_fxNext = 0;
        m_hitNext = 0;
        while (m_hitNext < pl.hits.size() && pl.hits[m_hitNext].beat < m_startBeat) {
            ++m_hitNext;
        }
        m_markNext = 0;
        while (m_markNext < m_marks.size() && m_marks[m_markNext].beat < m_startBeat) {
            ++m_markNext;
        }
        m_master = 0.0f;
        m_masterTarget = 1.0f;
        m_masterStep = masterStep(kGateInMs);
        m_lateSteps = 0;
        m_over = 0;
        m_overHold = 0;
        m_limiter.reset();
        m_limitLow = 1.0;
        m_state = Preparing;
        m_stepped = false;
        event("preparing");
    }

    void stop(double fadeMs) {
        if (m_state == Idle || m_state == Done) {
            return;
        }
        if (m_state == Preparing || m_state == Paused || m_state == Prerun) {
            halt();
            m_state = Idle;
            event("stopped");
            return;
        }
        m_state = Stopping;
        m_masterTarget = 0.0f;
        m_masterStep = masterStep(fadeMs);
    }

    void takeover(int d, const std::string& param, double value) {
        const int p = paramIndex(param);
        if (d < 1 || d > m_decks || p < 0) {
            throw std::runtime_error("接管：没有这个 deck / 旋钮");
        }
        DeckRt& rt = m_d[d];
        rt.manual[p] = true;
        rt.retOn[p] = false;
        rt.manualVal[p] = value;
    }
    void giveBack(int d, const std::string& param, const std::string& when, double rampBeats) {
        const int p = paramIndex(param);
        if (d < 1 || d > m_decks || p < 0) {
            throw std::runtime_error("回谱：没有这个 deck / 旋钮");
        }
        DeckRt& rt = m_d[d];
        if (!rt.manual[p]) {
            return;
        }
        const bool timed = m_plan && (m_state == Running || m_state == Pausing || m_state == Paused);
        const double g = timed ? beatAtFrame(static_cast<double>(m_frame)) : 0.0;
        double from = g;
        if (when == "beat") {
            from = std::ceil(g - 1e-9);
        } else if (when == "bar") {
            from = std::ceil((g - 1e-9) / 4.0) * 4.0;
        }
        // 从现在的实际位置出发（上一次回谱走到一半又回谱的也接得上）
        rt.manualVal[p] = p == P_VOL ? static_cast<double>(rt.volSm) : (rt.have[p] ? rt.applied[p] : rt.manualVal[p]);
        rt.retOn[p] = true;
        rt.retG0[p] = from;
        rt.retG1[p] = from + std::max(0.05, rampBeats);
    }

    // ---------------------------------------------------------------- 控制步

    void nextClip(int d) {
        DeckRt& rt = m_d[d];
        ++rt.pos;
        rt.st = DIdle;
        rt.seg = 0;
        rt.settle = 0;
        rt.phaseMs = 0.0;
    }

    void applyControls(int d, const Clip* c, double g) {
        DeckRt& rt = m_d[d];
        for (int p = 0; p < P_VOL; ++p) {
            if (rt.manual[p] && rt.retOn[p] && g >= rt.retG1[p]) {
                rt.manual[p] = rt.retOn[p] = false;
            }
            const double v = control(rt, c, p, g);
            if (!rt.have[p] || std::fabs(v - rt.applied[p]) > 5e-5) {
                m_be.param(d, p, v);
                rt.applied[p] = v;
                rt.have[p] = true;
            }
        }
        if (rt.manual[P_VOL] && rt.retOn[P_VOL] && g >= rt.retG1[P_VOL]) {
            rt.manual[P_VOL] = rt.retOn[P_VOL] = false;
        }
    }

    void applyLoop(int d, const Clip& c, const Seg& s) {
        if (s.loop > 0.0) {
            m_be.loop(d, s.lsrc, s.lsrc + s.loop * c.spb(), true);
        } else {
            m_be.loop(d, 0.0, 0.0, false);
        }
    }

    // 装好以后：停在起播点前一点（读盘线程先把那一段读进缓存），旋钮摆到起播那一拍的位置
    void cue(int d, const Clip& c) {
        DeckRt& rt = m_d[d];
        const double at = std::max(c.start, m_startBeat);
        m_be.play(d, false);
        m_be.loop(d, 0.0, 0.0, false);
        m_be.keylock(d, c.keylock);
        m_be.seek(d, c.srcAt(at) - static_cast<double>(kPrerollBuffers * m_n) / m_sr);
        for (int p = 0; p < P_VOL; ++p) {
            rt.have[p] = false;
        }
        applyControls(d, &c, at);
        hintJumps(d, c, c.segIndex(at));
    }

    // 两段之间怎么接：出 loop（连续）/ 进 loop（连续）/ 跳段（不连续，deck 要重新定位）
    enum Join { JoinExit, JoinEnter, JoinJump };
    static Join joinOf(const Clip& c, int i, double* phaseOut = nullptr) {
        const Seg& cur = c.segs[static_cast<size_t>(i) - 1];
        const Seg& nx = c.segs[static_cast<size_t>(i)];
        const double spb = c.spb();
        double phase = 0.0; // loop 里出的那一刻转到这一遍的第几拍（正好转满 = 一整遍）
        double cont = cur.src + (nx.g - cur.g) * spb;
        if (cur.loop > 0.0) {
            phase = std::fmod(nx.g - cur.g + (cur.src - cur.lsrc) / spb, cur.loop);
            if (phase < 1e-6) {
                phase = cur.loop;
            }
            cont = cur.lsrc + phase * spb;
        }
        if (phaseOut) {
            *phaseOut = phase;
        }
        const bool continuous = std::fabs(nx.src - cont) < 0.002;
        if (cur.loop > 0.0 && nx.loop <= 0.0 && continuous) {
            return JoinExit;
        }
        if (cur.loop <= 0.0 && nx.loop > 0.0 && continuous && nx.src >= nx.lsrc - 1e-9 && nx.src < nx.lsrc + nx.loop * spb) {
            return JoinEnter;
        }
        return JoinJump;
    }
    // 把这个片段后面要跳到的地方登记成预读点（装好时、每跳过一次以后各登记一次）
    void hintJumps(int d, const Clip& c, int fromSeg) {
        int slot = 0;
        for (int i = fromSeg + 1; i < static_cast<int>(c.segs.size()) && slot < kHints; ++i) {
            if (joinOf(c, i) == JoinJump && c.segs[static_cast<size_t>(i)].src > 0.0) {
                m_be.hint(d, slot++, c.segs[static_cast<size_t>(i)].src, true);
            }
        }
        for (; slot < kHints; ++slot) {
            m_be.hint(d, slot, 0.0, false);
        }
    }

    void stepPreparing() {
        const Plan& pl = *m_plan;
        bool allReady = true;
        for (int d = 1; d <= m_decks; ++d) {
            DeckRt& rt = m_d[d];
            const Clip* c = clipOf(d);
            if (!c || c->load > m_startBeat) {
                continue; // 这个 deck 开演时还用不到
            }
            if (rt.st == DIdle) {
                if (m_be.loaded(d) != c->gen) {
                    m_be.load(d, c->asset, c->gen);
                }
                rt.st = DLoading;
            }
            if (rt.st == DLoading) {
                const int l = m_be.loaded(d);
                if (l == c->gen) {
                    cue(d, *c);
                    rt.st = DReady;
                    receipt(c->id + ".load", "ok", c, c->load, m_startBeat);
                } else if (l == -c->gen) {
                    rt.st = DFailed;
                    receipt(c->id + ".load", "failed", c, c->load, m_startBeat, "装载失败");
                } else {
                    allReady = false;
                }
            }
        }
        if (!allReady) {
            return;
        }
        // 走带从起点前一小段开始（给预卷留出块数）：这一块开头 = 起点 − lead
        const double leadSec = static_cast<double>((kPrerollBuffers + 2) * m_n) / m_sr;
        m_originSec = pl.tempo.secAt(m_startBeat) - leadSec;
        m_originFrame = m_frame;
        m_state = m_startBeat < m_audibleBeat - 1e-9 ? Prerun : Running;
        event(m_state == Prerun ? "prerun" : "running");
    }

    void stepRunning() {
        const Plan& pl = *m_plan;
        const double g0 = beatAtFrame(static_cast<double>(m_frame));
        const double g1 = beatAtFrame(static_cast<double>(m_frame + m_n));
        const double dg = g1 - g0;
        const double bpm = pl.tempo.bpmAt(0.5 * (g0 + g1));
        if (m_state == Prerun && g1 >= m_audibleBeat - 2.0 * dg) {
            m_state = Running; // 快到听得见的那一拍了：换回按声卡的时钟走
            event("running");
        }
        bool busy = false;
        for (int d = 1; d <= m_decks; ++d) {
            stepDeck(d, g0, g1, dg, bpm);
            busy = busy || clipOf(d) != nullptr;
        }
        if (!busy && m_state == Running) {
            bool fxBusy = m_fxNext < pl.fx.size();
            for (const auto& s : m_slots) {
                fxBusy = fxBusy || s.fx >= 0;
            }
            if (!fxBusy && g0 >= pl.len) {
                halt();
                m_state = Done;
                event("done");
            }
        }
    }

    void stepDeck(int d, double g0, double g1, double dg, double bpm) {
        DeckRt& rt = m_d[d];
        for (int guard = 0; guard < 8; ++guard) {
            const Clip* c = clipOf(d);
            if (!c) {
                return;
            }
            switch (rt.st) {
            case DIdle:
                if (g1 < c->load) {
                    return;
                }
                if (g0 >= c->stop) {
                    receipt(c->id + ".start", "skipped", c, c->start, g0, "已经过了停止点");
                    nextClip(d);
                    continue;
                }
                if (m_be.loaded(d) != c->gen) {
                    m_be.load(d, c->asset, c->gen);
                }
                rt.st = DLoading;
                return;
            case DLoading: {
                const int l = m_be.loaded(d);
                if (l == c->gen) {
                    cue(d, *c);
                    rt.st = DReady;
                    receipt(c->id + ".load", "ok", c, c->load, g0);
                    continue;
                }
                if (l == -c->gen || g0 >= c->stop) {
                    rt.st = DFailed;
                    receipt(c->id + ".load", "failed", c, c->load, g0, l == -c->gen ? "装载失败" : "到停止点还没装好");
                    receipt(c->id + ".start", "failed", c, c->start, g0, "片段没装上，这一段不放");
                    continue;
                }
                return;
            }
            case DReady: {
                double eff = std::max(c->start, m_startBeat);
                if (g0 >= c->stop) {
                    receipt(c->id + ".start", "skipped", c, c->start, g0, "已经过了停止点");
                    nextClip(d);
                    continue;
                }
                if (g0 + kPrerollBuffers * dg < eff) {
                    applyControls(d, c, eff);
                    return;
                }
                // 预卷不够一块了（装得晚 / 控制步晚了）：晚两块进，位置照样对在计划上
                const bool late = g0 > eff - dg;
                if (late) {
                    eff = g0 + 2.0 * dg;
                }
                rt.effStart = eff;
                rt.seg = c->segIndex(std::max(g0, c->start));
                m_be.seek(d, c->srcAt(g0));
                applyLoop(d, *c, c->segs[static_cast<size_t>(rt.seg)]);
                m_be.rate(d, bpm / c->bpm);
                m_be.play(d, true);
                rt.st = DPlaying;
                rt.settle = 3;
                receipt(c->id + ".start", late ? "late" : "ok", c, std::max(c->start, m_startBeat), eff);
                applyControls(d, c, eff);
                return;
            }
            case DPlaying: {
                if (g0 >= c->stop + 2.0 * dg) { // 门已经在停止点关完了：过两块再停 deck
                    m_be.play(d, false);
                    m_be.loop(d, 0.0, 0.0, false);
                    receipt(c->id + ".stop", "ok", c, c->stop, c->stop);
                    rt.st = DDone;
                    continue;
                }
                followSegs(d, *c, g0, g1);
                double rate = bpm / c->bpm;
                if (rt.settle > 0) {
                    --rt.settle;
                } else if (g0 < c->stop) {
                    const Seg& s = c->segs[static_cast<size_t>(c->segIndex(g0))];
                    double err = m_be.position(d) - c->srcAt(g0);
                    if (s.loop > 0.0) {
                        const double ls = s.loop * c->spb();
                        err -= ls * std::round(err / ls);
                    }
                    rt.phaseMs = err * 1000.0;
                    if (std::fabs(err) > kPhaseHardMs / 1000.0) {
                        m_be.seek(d, c->srcAt(g0));
                        rt.settle = 3;
                        receipt(c->id + ".resync", "late", c, g0, g0, "位置差 " + std::to_string(err * 1000.0) + " ms，重新定位");
                    } else if (std::fabs(err) > 2e-5) {
                        rate *= 1.0 - std::max(-kPhaseMaxNudge, std::min(kPhaseMaxNudge, err / kPhaseCorrSec));
                    }
                }
                m_be.rate(d, rate);
                applyControls(d, c, 0.5 * (g0 + g1));
                return;
            }
            case DFailed:
                if (g0 < c->stop) {
                    return;
                }
                nextClip(d);
                continue;
            case DDone:
                nextClip(d);
                continue;
            }
        }
    }

    // 片段里一段接一段：进 loop、出 loop（连续）在块边界上开 / 关 Mixxx 的 loop（回绕由 deck 自己做，落在采样上）；
    // 不连续 = 跳段：在那一拍之前的块开头定位到「新的一段往回推」的位置——早跳一到两块（≤ 23 ms），拍子不乱。
    void followSegs(int d, const Clip& c, double g0, double g1) {
        DeckRt& rt = m_d[d];
        const double spb = c.spb();
        while (rt.seg + 1 < static_cast<int>(c.segs.size())) {
            const Seg& cur = c.segs[static_cast<size_t>(rt.seg)];
            const Seg& nx = c.segs[static_cast<size_t>(rt.seg) + 1];
            const double gb = nx.g;
            double phase = 0.0;
            const Join join = joinOf(c, rt.seg + 1, &phase);
            if (join == JoinExit) {
                // 出 loop：最后一遍转到一半时关（正好在 loop 尾那一拍关的话，deck 可能已经跳回 loop 头，会多转一遍）
                if (g1 < gb - phase / 2.0) {
                    return;
                }
                m_be.loop(d, 0.0, 0.0, false);
            } else if (join == JoinEnter) {
                if (g1 < gb) {
                    return;
                }
                m_be.loop(d, nx.lsrc, nx.lsrc + nx.loop * spb, true);
            } else {
                // 跳段：deck 定位的那一块是旧位置渐出、新位置渐入（Mixxx 定位时的防爆音交叉淡化，enginebuffer.cpp:1100-1110）。
                // 提前一块跳：那一拍落在下一块里，到那一拍时新位置已经是满的（跳到的 kick 不会被削掉一截）。
                // 目标在装好时就登记成了预读点（hintJumps），跳过去不用等读盘
                if (g1 + (g1 - g0) < gb) {
                    return;
                }
                if (cur.loop > 0.0) {
                    m_be.loop(d, 0.0, 0.0, false);
                }
                m_be.seek(d, Clip::segSrc(nx, g0, spb));
                if (nx.loop > 0.0) {
                    m_be.loop(d, nx.lsrc, nx.lsrc + nx.loop * spb, true);
                }
                rt.settle = 3;
                hintJumps(d, c, rt.seg + 1);
            }
            ++rt.seg;
            if (!nx.id.empty()) {
                receipt(nx.id, g0 > gb ? "late" : "ok", &c, gb, std::max(g0, gb));
            }
        }
    }

    // ---------------------------------------------------------------- 发送效果

    void renderFx(float* out, double g0, double g1, double bpm, double a300) {
        const Plan& pl = *m_plan;
        const int N = m_n;
        // 到点的效果占上自己的实例（实例先清空：上一个效果的尾音不会漏进来）
        while (m_fxNext < pl.fx.size() && pl.fx[m_fxNext].start <= g1) {
            const FxSpec& f = pl.fx[m_fxNext];
            if (f.end + f.tail > g0) {
                Slot& s = m_slots[f.slot];
                s.delay.reset(1.0f);
                if (s.reverb) {
                    s.reverb->reset();
                }
                s.fx = static_cast<int>(m_fxNext);
                s.prevSend = s.prevWet = 0.0f;
                s.gate = 0.0f;
                s.ms = 0.0;
            }
            ++m_fxNext;
        }
        for (auto& s : m_slots) {
            if (s.fx < 0) {
                continue;
            }
            const FxSpec& f = pl.fx[static_cast<size_t>(s.fx)];
            if (g0 >= f.end + f.tail) {
                s.fx = -1;
                s.prevSend = s.prevWet = 0.0f;
                s.ms = 0.0;
                continue;
            }
            // 只收自己那个片段的声音：同一个 deck 后来装的新片段进不来（它没在放 = 输入是静音，尾音照样响完）
            const int ci = pl.clipIndex.at(f.clip);
            const Clip& c = pl.clips[static_cast<size_t>(ci)];
            const bool feeding = clipOf(c.deck) == &c && m_d[c.deck].st == DPlaying;
            const std::vector<float>& src = m_post[static_cast<size_t>(c.deck)];
            // 送出：从 start 开到 end（和 deck 的门一样落在采样上：start 那一帧已经全开，end 那一帧已经关完），
            // 量 = send_gain × 送出曲线。先乘进输入里，效果本身的送出固定是 1
            const double dgs = (g1 - g0) / N;
            const double gateIn = kGateInMs / 1000.0 * bpm / 60.0, gateOut = kGateOutMs / 1000.0 * bpm / 60.0;
            const float upStep = static_cast<float>(1000.0 / (kGateInMs * m_sr));
            const float downStep = static_cast<float>(1000.0 / (kGateOutMs * m_sr));
            const float sg = static_cast<float>(f.param("send_gain", 1.0));
            const float c0 = static_cast<float>(f.send.at(std::min(g0, f.end))), c1 = static_cast<float>(f.send.at(std::min(g1, f.end)));
            float last = 0.0f;
            for (int n = 0; n < N; ++n) {
                const double g = g0 + n * dgs;
                const float target = (feeding && g >= f.start - gateIn && g < f.end - gateOut) ? 1.0f : 0.0f;
                if (s.gate < target) {
                    s.gate = std::min(target, s.gate + upStep);
                } else if (s.gate > target) {
                    s.gate = std::max(target, s.gate - downStep);
                }
                last = s.gate * sg * (c0 + (c1 - c0) * static_cast<float>(n) / static_cast<float>(N));
                m_fxIn[static_cast<size_t>(2 * n)] = src[static_cast<size_t>(2 * n)] * last;
                m_fxIn[static_cast<size_t>(2 * n + 1)] = src[static_cast<size_t>(2 * n + 1)] * last;
            }
            const float send = last;
            // 返回量：关送出以后保持最后的值；尾音的最后 1/4 淡掉（实例要让给别人了）
            double wet = f.wet.at(std::min(g1, f.end));
            if (f.tail > 0.0 && g1 > f.end + 0.75 * f.tail) {
                wet *= std::max(0.0, (f.end + f.tail - g1) / (0.25 * f.tail));
            }
            if (f.kind == "reverb" && s.reverb) {
                s.reverb->process(m_fxIn.data(), m_wet.data(), N, static_cast<float>(f.param("decay", 0.5)),
                        static_cast<float>(f.param("bandwidth", 1.0)), static_cast<float>(f.param("damping", 0.0)), 1.0f, 1.0f);
            } else {
                const double beats = f.param("beats", 0.5);
                const int frames = static_cast<int>(beats * 60.0 / bpm * m_sr);
                s.delay.process(m_fxIn.data(), m_wet.data(), N, frames, 1.0f, static_cast<float>(f.param("feedback", 0.0)),
                        static_cast<float>(f.param("pingpong", 0.0)));
            }
            const float w0 = s.prevWet, w1 = static_cast<float>(wet);
            double sum = 0.0;
            for (int n = 0; n < N; ++n) {
                const float w = w0 + (w1 - w0) * static_cast<float>(n) / static_cast<float>(N);
                const float l = m_wet[static_cast<size_t>(2 * n)] * w, r = m_wet[static_cast<size_t>(2 * n + 1)] * w;
                out[2 * n] += l;
                out[2 * n + 1] += r;
                sum += static_cast<double>(l) * l + static_cast<double>(r) * r;
            }
            s.prevSend = send;
            s.prevWet = w1;
            s.ms += (sum / (2.0 * N) - s.ms) * a300;
        }
    }
    Backend& m_be;
    int m_sr, m_n, m_decks = kMaxDecks;
    ReverbFactory m_reverbFactory;
    std::recursive_mutex m_gate;
    std::shared_ptr<const Plan> m_plan;
    State m_state = Idle;
    long long m_frame = 0;
    long long m_steppedFrame = -1;
    bool m_stepped = false;
    long long m_originFrame = 0;
    double m_originSec = 0.0;
    double m_startBeat = 0.0, m_audibleBeat = 0.0;
    DeckRt m_d[kMaxDecks + 1];
    Slot m_slots[kFxSlots];
    size_t m_fxNext = 0;
    std::vector<std::vector<float>> m_post;
    std::vector<float> m_wet, m_fxIn;
    // 处理组（插入效果）、对齐延迟、采样
    struct Mark {
        double beat;
        std::string id;
        int bus;
    };
    std::vector<std::unique_ptr<BusRt>> m_buses;
    int m_latA = 0, m_latM = 0, m_splitLatency = 0;
    AlignDelay m_alignD[kMaxDecks + 1];
    AlignDelay m_alignM;
    std::vector<float> m_volRing[kMaxDecks + 1];
    size_t m_volPos[kMaxDecks + 1] = {};
    std::vector<std::vector<float>> m_u, m_v, m_vd;
    std::vector<float> m_busIn, m_busOut, m_zero;
    Sampler m_sampler;
    size_t m_hitNext = 0, m_markNext = 0;
    std::vector<Mark> m_marks;
    long long m_runBuffers = 0;
    float m_master = 0.0f, m_masterTarget = 1.0f, m_masterStep = 1.0f;
    double m_masterMs = 0.0, m_masterPeak = 0.0;
    long long m_over = 0;
    int m_overHold = 0;
    MasterLimiter m_limiter;
    bool m_limit = false;
    double m_limitLow = 1.0; // 最近（约 300 ms）限幅器压到的最低增益
    int m_lateSteps = 0;
    long long m_seq = 0;
    std::vector<std::string> m_out;
};

} // namespace djs5
