// DJ Studio v5 的「处理组」（插入效果的总线）——现场效果都在这里：
//   分频段 Trans / Roll、Filter、Flanger、Phaser、Spiral、插入式 Echo（Filter Echo 宏用）、Noise，
//   以及收尾用的 Release（Echo / 刹车 / 回转）。DSP 本身在 dsp.hpp，这里只管「什么时候开、接在哪、怎么收」。
//
// 一个处理组 = 一条路由 + 一组目标片段：
//   插入（insert）  ：一个片段。信号 = 那个片段过了通道 EQ / Filter、还没过推子的声音；处理完再过推子。干声不另外直通。
//   组插入（group） ：几个片段各自过完推子以后加在一起，整组处理；这几个片段不再各自直通到主输出。
//   总输出（master）：全部 Deck、效果返回、采样加起来以后的整个主混音。
// 同一时间一个片段最多在一个处理组里（编译时查）；处理组之外的发送效果（Delay / Echo / Reverb）照常另算。
//
// 组里的件按固定顺序走（kBusVersion 管这个顺序和下面这些时间常数，改了要加一）：
//   分频 → [选了频段的 Trans / Roll，各管各的频段] → 三段加回来 → 整路的 Trans → Roll → Filter → Flanger → Phaser
//   → Spiral → Echo → Noise → Release
//
// 延迟：分频器是线性相位的，有固定延迟 L（44.1 kHz 是 264 帧 ≈ 6 ms）。计划里只要有一个处理组用到分频，这一级的
// 所有路（不管在不在组里）都延迟同样的 L：没在分频的走一条等长的延迟线（AlignDelay）。分频器在各段都不动的时候
// 输出 = 输入延迟 L（残差 −160 dBFS 以下），所以「直通 ↔ 进组」在缓冲边界上换，听不出来、也量不出来。
// 计划里没有分频的件：这一级没有延迟，和 P0 的输出逐位相同。
//
// 时间：组里处理的是延迟之后的声音，件的开 / 关、跟拍的相位都按「延迟之后的时间轴」算（调用的人把拍换好再传进来），
// 开 / 关落在采样上（一个缓冲在边界处切成两段处理）。
//
// 从中间开始：所有状态在 start() 时清零，再从效果开始之前静音预跑过来（Roll 从抓取点之前、Release 从它开始之前），
// 和从头连续放到这里的状态一样。
#pragma once

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "core.hpp"
#include "dsp.hpp"
#include "fx.hpp"

namespace djs5 {

constexpr int kBusVersion = 1;
constexpr int kArmBuffers = 8;         // 提前这么多个缓冲开始往组里喂（延迟线 / 分频器里先要有历史）
constexpr int kWarmBuffers = 4;        // 喂够这么多个缓冲才能把这一路换到组里
constexpr double kLiveBuffers = 2.0;   // 第一件开始前 / 最后一件收完后这么多个缓冲，这一路走组里
constexpr double kUnitXfadeMs = 2.0;   // Filter 的干 / 湿、Release 的干声收 / 放
constexpr double kTransMaxStepMs = 0.75; // Trans 的门一个采样最多走 1 / 这么多毫秒（正常的沿比这慢，只挡窗口边上的跳变）

class BusRt {
  public:
    void init(const BusSpec& spec, int sampleRate, int bufferFrames, int latency) {
        m_spec = &spec;
        m_sr = sampleRate;
        m_n = bufferFrames;
        m_L = latency;
        m_align.init(latency);
        if (spec.split && latency > 0) {
            m_split.reset(new BandSplit3());
            m_split->init(sampleRate);
        }
        const size_t sz = static_cast<size_t>(bufferFrames) * 2;
        m_x.assign(sz, 0.0f);
        m_t1.assign(sz, 0.0f);
        m_t2.assign(sz, 0.0f);
        for (auto& b : m_band) {
            b.assign(m_split ? sz : 0, 0.0f);
        }
        m_units.clear();
        m_units.resize(spec.units.size());
        for (size_t i = 0; i < spec.units.size(); ++i) {
            UnitRt& u = m_units[i];
            const UnitSpec& s = spec.units[i];
            u.s = &s;
            switch (s.kind) {
            case U_ROLL:
                u.roll.reset(new Roll());
                u.roll->init(sampleRate);
                break;
            case U_FILTER:
                u.svf.reset(new SvfFilter());
                u.svf->init(sampleRate);
                break;
            case U_FLANGER:
                u.fl.reset(new Flanger());
                u.fl->init(sampleRate);
                break;
            case U_PHASER:
                u.ph.reset(new Phaser());
                u.ph->init(sampleRate);
                break;
            case U_SPIRAL:
                u.sp.reset(new Spiral());
                u.sp->init(sampleRate);
                break;
            case U_ECHO:
            case U_REL_ECHO:
                u.dl.reset(new DelayLine());
                u.dl->init(sampleRate);
                break;
            case U_NOISE:
                u.nz.reset(new Noise());
                u.nz->init(sampleRate);
                break;
            case U_BRAKE:
            case U_SPIN:
                u.bs.reset(new BrakeSpin());
                u.bs->init(sampleRate);
                break;
            default:
                break;
            }
        }
        reset();
    }

    // 全部状态清零（开始 / 停止 / 不再喂的时候）
    void reset() {
        m_align.reset();
        if (m_split) {
            m_split->reset();
        }
        for (UnitRt& u : m_units) {
            resetUnit(u);
        }
        m_fed = 0;
        m_sinceStart = false;
        m_muted = false;
    }

    const BusSpec& spec() const { return *m_spec; }
    int latency() const { return m_L; }

    // [gA, gB) 这段（两头各放宽 margin 拍）里有没有件在用（含尾音、退场后静音着的那一段）
    bool wants(double gA, double gB, double margin) const {
        for (const UnitSpec& s : m_spec->units) {
            if (gB > s.start - unitLead(s) - margin && gA < unitEnd(s) + margin) {
                return true;
            }
        }
        return false;
    }
    // 喂了一个缓冲之后调：这一路现在能不能换到组里
    bool warm() const { return m_sinceStart || m_fed >= kWarmBuffers; }
    int fed() const { return m_fed; }
    void markSinceStart() { m_sinceStart = true; } // 开始的第一个缓冲就在喂：大家的历史都是零，不用等
    bool muted() const { return m_muted; }         // Release 退场以后，这一组现在出的是静音

    // in：这一组的输入（没延迟的时间轴）；out：处理完的（延迟 L 之后的时间轴）。
    // g0 / dg：out 的第 0 帧在全场第几拍、每帧走多少拍（已经扣掉了延迟）。bpm：这一块的全场速度。
    void process(const float* in, float* out, double g0, double dg, double bpm) {
        const int N = m_n;
        float* x = m_x.data();
        const double g1 = g0 + N * dg;
        const double near = 2.0 * N * std::max(dg, 0.0);
        bool banded = false;
        if (m_L > 0) {
            m_align.process(in, x, N);
            if (m_split) {
                m_split->process(in, m_band[0].data(), m_band[1].data(), m_band[2].data(), N);
                for (const UnitRt& u : m_units) {
                    if (!u.s->allBands && (u.s->kind == U_TRANS || u.s->kind == U_ROLL) &&
                            g1 > u.s->start - near && g0 < u.s->end + near) {
                        banded = true;
                    }
                }
            }
        } else {
            std::memcpy(x, in, static_cast<size_t>(N) * 2 * sizeof(float));
        }
        if (banded) {
            for (UnitRt& u : m_units) {
                if (!u.s->allBands && u.s->kind == U_TRANS) {
                    runTransBands(u, g0, dg);
                }
            }
            for (UnitRt& u : m_units) {
                if (!u.s->allBands && u.s->kind == U_ROLL) {
                    runRollBands(u, g0, dg);
                }
            }
            const float* lo = m_band[0].data();
            const float* mid = m_band[1].data();
            const float* hi = m_band[2].data();
            for (int i = 0; i < 2 * N; ++i) {
                x[i] = lo[i] + mid[i] + hi[i];
            }
        } else {
            // 分频的件不在窗口里：它们的状态留在「没开」
            for (UnitRt& u : m_units) {
                if (!u.s->allBands && (u.s->kind == U_TRANS || u.s->kind == U_ROLL)) {
                    idleUnit(u, g0);
                }
            }
        }
        for (int kind = 0; kind < U_COUNT; ++kind) {
            for (UnitRt& u : m_units) {
                if (u.s->kind != kind || (!u.s->allBands && (kind == U_TRANS || kind == U_ROLL))) {
                    continue;
                }
                runUnit(u, x, g0, dg, bpm);
            }
        }
        m_muted = false;
        for (const UnitRt& u : m_units) {
            if (isRelease(u.s->kind) && u.dry <= 0.0f && g0 >= u.s->end && g0 < u.s->until) {
                m_muted = true;
            }
        }
        std::memcpy(out, x, static_cast<size_t>(N) * 2 * sizeof(float));
        ++m_fed;
    }

    // 给状态消息：现在哪些件开着 / 只剩尾音
    void report(double g, Json& out) const {
        for (const UnitRt& u : m_units) {
            const UnitSpec& s = *u.s;
            const bool on = g >= s.start && g < s.end;
            const bool tail = !on && g >= s.end && g < unitEnd(s);
            if (!on && !tail) {
                continue;
            }
            Json o = Json::object();
            o.set("id", s.id).set("kind", unitKindName(s.kind)).set("bus", m_spec->id).set("route", routeName(m_spec->route));
            o.set("on", on).set("tail", tail);
            if (isRelease(s.kind)) {
                o.set("muted", !on && !s.back);
            }
            out.push(o);
        }
    }

  private:
    struct UnitRt {
        const UnitSpec* s = nullptr;
        std::unique_ptr<Roll> roll;
        std::unique_ptr<SvfFilter> svf;
        std::unique_ptr<Flanger> fl;
        std::unique_ptr<Phaser> ph;
        std::unique_ptr<Spiral> sp;
        std::unique_ptr<DelayLine> dl;
        std::unique_ptr<Noise> nz;
        std::unique_ptr<BrakeSpin> bs;
        double gain = 1.0;   // Trans：门现在的增益
        float mix = 0.0f;    // Filter / Echo：干湿、送出的平滑
        float dry = 1.0f;    // Release：干声（这一组本来的输出）的增益
        bool armed = false;  // Roll / 刹车 / 回转：这一轮的命令排过了
        bool begun = false;  // 这一轮处理过（离开窗口时清一次状态）
        int stage = 0;       // 刹车 / 回转：0 没到，1 在放，2 放完
    };

    // 开始之前要先喂多久（拍）：Release Echo 要把开始前最后一个回声周期写进延迟线
    static double unitLead(const UnitSpec& s) {
        return s.kind == U_REL_ECHO ? s.param("beats", 0.5) : 0.0;
    }
    static double unitEnd(const UnitSpec& s) {
        if (isRelease(s.kind)) {
            return s.back ? s.end : std::max(s.end, s.until); // 退场：静音一直算到下一次显式启用
        }
        return s.end + s.tail;
    }
    int frameOf(double beat, double g0, double dg) const {
        if (!(dg > 0.0)) {
            return beat <= g0 ? 0 : m_n;
        }
        const double k = std::ceil((beat - g0) / dg - 1e-6);
        return k <= 0.0 ? 0 : (k >= m_n ? m_n : static_cast<int>(k));
    }
    void resetUnit(UnitRt& u) {
        if (u.roll) {
            u.roll->reset();
        }
        if (u.svf) {
            u.svf->reset();
        }
        if (u.fl) {
            u.fl->reset();
        }
        if (u.ph) {
            u.ph->reset();
        }
        if (u.sp) {
            u.sp->reset();
        }
        if (u.dl) {
            u.dl->reset(1.0f);
        }
        if (u.nz) {
            u.nz->setSeed(seedOf(u.s->id));
            u.nz->reset();
        }
        if (u.bs) {
            u.bs->reset();
        }
        u.gain = 1.0;
        u.mix = 0.0f;
        u.dry = 1.0f;
        u.armed = u.begun = false;
        u.stage = 0;
    }
    static std::uint32_t seedOf(const std::string& id) {
        std::uint32_t h = 2166136261u;
        for (unsigned char ch : id) {
            h = (h ^ ch) * 16777619u;
        }
        return h != 0 ? h : 1u;
    }
    // 窗口过了：清掉这一件的残余（下一次开是干净的），只清一次
    void idleUnit(UnitRt& u, double g0) {
        if (u.begun && g0 >= unitEnd(*u.s)) {
            resetUnit(u);
        }
    }
    static float slewTo(float cur, float target, float step) {
        return cur < target ? std::min(target, cur + step) : std::max(target, cur - step);
    }
    static float tailFade(const UnitSpec& s, double g) {
        if (s.tail <= 0.0 || g <= s.end + 0.75 * s.tail) {
            return 1.0f;
        }
        return static_cast<float>(std::max(0.0, (s.end + s.tail - g) / (0.25 * s.tail)));
    }

    // ---- Trans
    double transTarget(const UnitSpec& s, double g, double edge) const {
        if (g < s.start || g >= s.end) {
            return 1.0;
        }
        return TransGate::gainAt(g, edge, s.param("period", 0.5), s.param("duty", 0.5), s.param("depth", 1.0), s.start);
    }
    void runTransBands(UnitRt& u, double g0, double dg) {
        const UnitSpec& s = *u.s;
        const double edge = TransGate::edgeBeats(dg, m_sr);
        const double maxStep = 1000.0 / (kTransMaxStepMs * m_sr);
        u.begun = true;
        for (int n = 0; n < m_n; ++n) {
            const double t = transTarget(s, g0 + n * dg, edge);
            u.gain = t > u.gain ? std::min(t, u.gain + maxStep) : std::max(t, u.gain - maxStep);
            const float gn = static_cast<float>(u.gain);
            for (int b = 0; b < 3; ++b) {
                if (s.band[b]) {
                    m_band[b][static_cast<size_t>(2 * n)] *= gn;
                    m_band[b][static_cast<size_t>(2 * n + 1)] *= gn;
                }
            }
        }
    }
    void runTrans(UnitRt& u, float* x, double g0, double dg) {
        const UnitSpec& s = *u.s;
        const double g1 = g0 + m_n * dg;
        if (!(g1 > s.start && g0 < s.end) && u.gain >= 1.0) {
            idleUnit(u, g0);
            return;
        }
        const double edge = TransGate::edgeBeats(dg, m_sr);
        const double maxStep = 1000.0 / (kTransMaxStepMs * m_sr);
        u.begun = true;
        for (int n = 0; n < m_n; ++n) {
            const double t = transTarget(s, g0 + n * dg, edge);
            u.gain = t > u.gain ? std::min(t, u.gain + maxStep) : std::max(t, u.gain - maxStep);
            const float gn = static_cast<float>(u.gain);
            x[2 * n] *= gn;
            x[2 * n + 1] *= gn;
        }
    }

    // ---- Roll
    void armRoll(UnitRt& u) {
        const UnitSpec& s = *u.s;
        u.roll->reset();
        u.roll->setMix(static_cast<float>(s.param("mix", 1.0)));
        u.roll->arm(s.start, s.param("len", 1.0));
        for (const auto& e : s.ev) {
            u.roll->setLength(e.first, e.second);
        }
        u.roll->release(s.end);
        u.armed = true;
    }
    bool rollWanted(UnitRt& u, double g0, double g1, double near) {
        const UnitSpec& s = *u.s;
        if (g1 > s.start - near && g0 < s.end + near) {
            if (!u.armed) {
                armRoll(u);
            }
            u.begun = true;
            return true;
        }
        idleUnit(u, g0);
        return false;
    }
    void runRollBands(UnitRt& u, double g0, double dg) {
        const UnitSpec& s = *u.s;
        const int N = m_n;
        if (!rollWanted(u, g0, g0 + N * dg, 2.0 * N * std::max(dg, 0.0))) {
            return;
        }
        float* t = m_t1.data();
        std::fill(t, t + 2 * N, 0.0f);
        int first = -1;
        for (int b = 0; b < 3; ++b) {
            if (s.band[b]) {
                const float* src = m_band[b].data();
                for (int i = 0; i < 2 * N; ++i) {
                    t[i] += src[i];
                }
                if (first < 0) {
                    first = b;
                } else {
                    std::fill(m_band[b].begin(), m_band[b].end(), 0.0f);
                }
            }
        }
        if (first >= 0) {
            u.roll->process(t, m_band[first].data(), N, g0, dg);
        }
    }
    void runRoll(UnitRt& u, float* x, double g0, double dg) {
        const int N = m_n;
        if (!rollWanted(u, g0, g0 + N * dg, 2.0 * N * std::max(dg, 0.0))) {
            return;
        }
        std::memcpy(m_t1.data(), x, static_cast<size_t>(N) * 2 * sizeof(float));
        u.roll->process(m_t1.data(), x, N, g0, dg);
    }

    // ---- 整路的件
    void runUnit(UnitRt& u, float* x, double g0, double dg, double bpm) {
        const UnitSpec& s = *u.s;
        const int N = m_n;
        const double g1 = g0 + N * dg;
        const double near = 2.0 * N * std::max(dg, 0.0);
        const float step = static_cast<float>(1000.0 / (kUnitXfadeMs * m_sr));
        float* t1 = m_t1.data();
        float* t2 = m_t2.data();
        const size_t bytes = static_cast<size_t>(N) * 2 * sizeof(float);
        switch (s.kind) {
        case U_TRANS:
            runTrans(u, x, g0, dg);
            return;
        case U_ROLL:
            runRoll(u, x, g0, dg);
            return;
        case U_FILTER: {
            if (!(g1 > s.start && g0 < s.end) && u.mix <= 0.0f) {
                idleUnit(u, g0);
                return;
            }
            u.begun = true;
            const double gm = std::min(std::max(0.5 * (g0 + g1), s.start), s.end);
            const double hz = std::exp2(s.curve("cut", gm, std::log2(1000.0)));
            u.svf->set(static_cast<SvfFilter::Mode>(static_cast<int>(s.param("mode", 1.0))), hz, s.param("q", 0.707));
            u.svf->process(x, t1, N);
            for (int n = 0; n < N; ++n) {
                const double g = g0 + n * dg;
                u.mix = slewTo(u.mix, (g >= s.start && g < s.end) ? 1.0f : 0.0f, step);
                x[2 * n] += (t1[2 * n] - x[2 * n]) * u.mix;
                x[2 * n + 1] += (t1[2 * n + 1] - x[2 * n + 1]) * u.mix;
            }
            return;
        }
        case U_FLANGER:
        case U_PHASER: {
            // 提前两个缓冲开始喂（混合量 0 = 出来的就是干声），到点把混合量开上去；关了以后再走几个缓冲让它平滑回零
            if (!(g1 > s.start - near && g0 < s.end + 2.0 * near)) {
                idleUnit(u, g0);
                return;
            }
            u.begun = true;
            const int ka = frameOf(s.start, g0, dg), kb = frameOf(s.end, g0, dg);
            const int cut[4] = {0, std::min(ka, kb), kb, N};
            for (int seg = 0; seg < 3; ++seg) {
                const int a = cut[seg], b = cut[seg + 1];
                if (b <= a) {
                    continue;
                }
                const float mix = seg == 1 ? static_cast<float>(s.param("mix", 0.5)) : 0.0f;
                std::memcpy(t1, x + 2 * a, static_cast<size_t>(b - a) * 2 * sizeof(float));
                if (s.kind == U_FLANGER) {
                    u.fl->set(s.param("period", 4.0), static_cast<float>(s.param("depth", 0.7)),
                            static_cast<float>(s.param("feedback", 0.5)), mix, s.start);
                    u.fl->process(t1, x + 2 * a, b - a, g0 + a * dg, dg);
                } else {
                    u.ph->set(s.param("period", 4.0), static_cast<float>(s.param("depth", 0.7)),
                            static_cast<float>(s.param("feedback", 0.5)), mix, s.start);
                    u.ph->process(t1, x + 2 * a, b - a, g0 + a * dg, dg);
                }
            }
            return;
        }
        case U_SPIRAL: {
            if (!(g1 > s.start && g0 < s.end + s.tail)) {
                idleUnit(u, g0);
                return;
            }
            if (!u.begun) {
                u.sp->reset();
                u.sp->set(s.param("beats", 0.5), static_cast<float>(s.param("feedback", 0.7)), static_cast<float>(s.param("mix", 0.5)));
                u.sp->setGlide(s.start, s.param("glide", std::max(s.end - s.start, 1e-3)), s.param("semis", 12.0));
                u.begun = true;
            }
            const int ka = frameOf(s.start, g0, dg), kb = frameOf(s.end, g0, dg);
            const int cut[4] = {0, std::min(ka, kb), kb, N};
            std::memcpy(t1, x, bytes);
            for (int seg = 0; seg < 3; ++seg) {
                const int a = cut[seg], b = cut[seg + 1];
                if (b <= a) {
                    continue;
                }
                u.sp->setSend(seg == 1 ? 1.0f : 0.0f);
                u.sp->process(t1 + 2 * a, t2 + 2 * a, b - a, g0 + a * dg, dg);
            }
            for (int n = 0; n < N; ++n) {
                const float f = tailFade(s, g0 + n * dg);
                x[2 * n] = t1[2 * n] + (t2[2 * n] - t1[2 * n]) * f;
                x[2 * n + 1] = t1[2 * n + 1] + (t2[2 * n + 1] - t1[2 * n + 1]) * f;
            }
            return;
        }
        case U_ECHO: {
            if (!(g1 > s.start && g0 < s.end + s.tail)) {
                idleUnit(u, g0);
                return;
            }
            if (!u.begun) {
                u.dl->reset(1.0f);
                u.mix = 0.0f;
                u.begun = true;
            }
            const float upStep = static_cast<float>(1000.0 / (kGateInMs * m_sr)), downStep = static_cast<float>(1000.0 / (kGateOutMs * m_sr));
            const double gin = kGateInMs / 1000.0 * bpm / 60.0, gout = kGateOutMs / 1000.0 * bpm / 60.0;
            for (int n = 0; n < N; ++n) {
                const double g = g0 + n * dg;
                const float target = (g >= s.start - gin && g < s.end - gout) ? 1.0f : 0.0f;
                u.mix = u.mix < target ? std::min(target, u.mix + upStep) : std::max(target, u.mix - downStep);
                const float sg = u.mix * static_cast<float>(s.curve("send", std::min(g, s.end), 1.0));
                t1[2 * n] = x[2 * n] * sg;
                t1[2 * n + 1] = x[2 * n + 1] * sg;
            }
            const int frames = static_cast<int>(s.param("beats", 0.5) * 60.0 / bpm * m_sr);
            u.dl->process(t1, t2, N, frames, 1.0f, static_cast<float>(s.param("feedback", 0.5)), static_cast<float>(s.param("pingpong", 0.0)));
            const float wet = static_cast<float>(s.param("wet", 1.0));
            for (int n = 0; n < N; ++n) {
                const float f = wet * tailFade(s, g0 + n * dg);
                x[2 * n] += t2[2 * n] * f;
                x[2 * n + 1] += t2[2 * n + 1] * f;
            }
            return;
        }
        case U_NOISE: {
            if (!(g1 > s.start && g0 < s.end + near)) {
                idleUnit(u, g0);
                return;
            }
            if (!u.begun) {
                u.nz->setSeed(seedOf(s.id));
                u.nz->reset();
                u.nz->setQ(s.param("q", 1.5));
                u.nz->setSweep(s.start, std::max(s.end - s.start, 1e-3), s.param("from", 400.0), s.param("to", 8000.0));
                u.begun = true;
            }
            const int ka = frameOf(s.start, g0, dg), kb = frameOf(s.end, g0, dg);
            const int cut[4] = {0, std::min(ka, kb), kb, N};
            std::fill(t1, t1 + 2 * N, 0.0f);
            for (int seg = 1; seg < 3; ++seg) { // 开始之前不出声、也不走随机数：从开始那一帧起的噪声序列是固定的
                const int a = cut[seg], b = cut[seg + 1];
                if (b <= a) {
                    continue;
                }
                const double gm = g0 + 0.5 * (a + b) * dg;
                u.nz->setLevel(seg == 1 ? static_cast<float>(s.curve("level", std::min(gm, s.end), 0.25)) : 0.0f);
                u.nz->process(t1 + 2 * a, b - a, g0 + a * dg, dg);
            }
            for (int i = 0; i < 2 * N; ++i) {
                x[i] += t1[i];
            }
            return;
        }
        case U_REL_ECHO:
            runReleaseEcho(u, x, g0, dg, bpm, step);
            return;
        case U_BRAKE:
        case U_SPIN:
            runBrake(u, x, g0, dg, step);
            return;
        default:
            return;
        }
    }

    // ---- Release
    // 干声（这一组本来的输出）什么时候收、什么时候放回来：到开始那一拍已经收完（那一拍上的鼓头不漏出来）；
    // 「回原声」到结束那一拍已经放回来；「退场」一直静音到下一次显式启用（until）。lead = 沿的长度（拍）。
    static float dryTarget(const UnitSpec& s, double g, double lead) {
        if (g < s.start - lead) {
            return 1.0f;
        }
        const double back = s.back ? s.end : s.until;
        return g >= back - lead ? 1.0f : 0.0f;
    }
    void runReleaseEcho(UnitRt& u, float* x, double g0, double dg, double bpm, float step) {
        const UnitSpec& s = *u.s;
        const int N = m_n;
        const double g1 = g0 + N * dg;
        const double beats = s.param("beats", 0.5);
        const double pre = beats + 4.0 * N * std::max(dg, 0.0); // 提前一个回声周期多一点开始往延迟线里写（见 unitLead）
        if (!(g1 > s.start - pre && g0 < unitEnd(s) + 2.0 * N * std::max(dg, 0.0))) {
            idleUnit(u, g0);
            return;
        }
        const double lead = kUnitXfadeMs / 1000.0 * bpm / 60.0;
        if (!u.begun) {
            u.dl->reset(1.0f);
            u.dry = dryTarget(s, g0, lead);
            u.stage = 0;
            u.begun = true;
        }
        float* t1 = m_t1.data();
        float* t2 = m_t2.data();
        const int frames = static_cast<int>(beats * 60.0 / bpm * m_sr);
        const float fb = static_cast<float>(s.param("feedback", 0.6));
        const int ka = frameOf(s.start, g0, dg);
        // 开始之前：只往延迟线里写（反馈 0）；开始那一帧起：不再写新的，延迟线里最后那一个周期带着反馈转下去
        const int cut[3] = {0, ka, N};
        for (int seg = 0; seg < 2; ++seg) {
            const int a = cut[seg], b = cut[seg + 1];
            if (b <= a) {
                continue;
            }
            const bool before = seg == 0;
            if (before) {
                std::memcpy(t1 + 2 * a, x + 2 * a, static_cast<size_t>(b - a) * 2 * sizeof(float));
            } else {
                std::fill(t1 + 2 * a, t1 + 2 * b, 0.0f);
                if (u.stage == 0) { // 开始那一帧：反馈直接到位（从 0 爬上去会丢掉那一帧上的鼓头）
                    u.dl->snapFeedback(fb);
                    u.stage = 1;
                }
            }
            u.dl->process(t1 + 2 * a, t2 + 2 * a, b - a, frames, 1.0f, before ? 0.0f : fb, 0.0f);
        }
        const double span = std::max(s.end - s.start, 1e-6);
        for (int n = 0; n < N; ++n) {
            const double g = g0 + n * dg;
            u.dry = slewTo(u.dry, dryTarget(s, g, lead), step);
            float w = 0.0f;
            if (g >= s.start && g < s.end) { // 回声只在声明的时长里响，最后 1/4 淡掉（尾音有上限）
                const double left = (s.end - g) / span;
                w = left < 0.25 ? static_cast<float>(left / 0.25) : 1.0f;
            }
            x[2 * n] = x[2 * n] * u.dry + t2[2 * n] * w;
            x[2 * n + 1] = x[2 * n + 1] * u.dry + t2[2 * n + 1] * w;
        }
    }
    void runBrake(UnitRt& u, float* x, double g0, double dg, float step) {
        const UnitSpec& s = *u.s;
        const int N = m_n;
        const double g1 = g0 + N * dg;
        const double near = 2.0 * N * std::max(dg, 0.0);
        if (!(g1 > s.start - near && g0 < unitEnd(s) + near)) {
            idleUnit(u, g0);
            return;
        }
        if (!u.begun) {
            u.bs->reset();
            u.stage = 0;
            u.dry = 1.0f;
            u.begun = true;
        }
        // 刹车 / 回转自己出的就是「慢下来的那一组」：放完以后它出静音；回原声 = resume() 淡回现场
        if (u.stage == 0 && g1 > s.start) {
            const int ka = frameOf(s.start, g0, dg);
            const int dur = std::max(1, static_cast<int>(std::lround(s.param("frames", 0.5 * m_sr))));
            if (s.kind == U_BRAKE) {
                u.bs->brake(std::min(dur, u.bs->maxBrakeFrames()), ka);
            } else {
                u.bs->backspin(std::min(dur, u.bs->maxSpinFrames()), ka);
            }
            u.stage = 1;
        }
        // 淡回现场要 kXfadeMs：提前这么久开始，到「回来」的那一拍现场已经是满的（那一拍上的鼓头不被淡入削掉）
        const double back = (s.back ? s.end : s.until) - dspd::msToFrames(kXfadeMs, m_sr) * std::max(dg, 0.0);
        if (u.stage == 1 && g1 > back) {
            u.bs->resume(frameOf(back, g0, dg));
            u.stage = 2;
        }
        std::memcpy(m_t1.data(), x, static_cast<size_t>(N) * 2 * sizeof(float));
        u.bs->process(m_t1.data(), x, N);
        // 给状态用：放完、还没回来 = 静音着
        u.dry = (u.stage == 1 && g0 >= s.end) ? 0.0f : 1.0f;
        (void)step;
    }

    const BusSpec* m_spec = nullptr;
    int m_sr = 44100, m_n = 512, m_L = 0;
    AlignDelay m_align;
    std::unique_ptr<BandSplit3> m_split;
    std::vector<UnitRt> m_units;
    std::vector<float> m_x, m_t1, m_t2;
    std::vector<float> m_band[3];
    int m_fed = 0;
    bool m_sinceStart = false;
    bool m_muted = false;
};

} // namespace djs5
