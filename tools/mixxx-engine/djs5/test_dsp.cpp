// dsp.hpp 的验收测试：独立程序，不用测试框架。每项检查打一行
//   PASS|FAIL <名字> <实测值> <限值>
// 有一项 FAIL 退出码就不是 0。INFO 开头的行只是数字（延迟、每块的 CPU），不算检查。
// 编译运行（仓库根目录）：
//   g++ -O2 -std=c++17 -Wall -Wextra tools/mixxx-engine/djs5/test_dsp.cpp -o /tmp/test_dsp && /tmp/test_dsp
// 再加 -ffast-math 编一遍、用 clang++ 编一遍，三种都要全过。
#include "dsp.hpp"

#include <chrono>
#include <cstdio>
#include <functional>
#include <string>
#include <utility>

using namespace djs5;

namespace {

using Buf = std::vector<float>; // 交错立体声
constexpr double kPi = 3.14159265358979323846;

int g_checks = 0, g_fail = 0;

// -ffast-math 下 isfinite 不可靠：直接看位（指数全 1 = inf / NaN）
bool finiteBits(double v) {
    uint64_t u;
    std::memcpy(&u, &v, sizeof(u));
    return (u & 0x7ff0000000000000ULL) != 0x7ff0000000000000ULL;
}
bool finiteBuf(const Buf& b) {
    for (float v : b) {
        uint32_t u;
        std::memcpy(&u, &v, sizeof(u));
        if ((u & 0x7f800000u) == 0x7f800000u) {
            return false;
        }
    }
    return true;
}

// op："<=" / ">=" / "=="
void check(const std::string& name, double measured, const char* op, double limit) {
    bool ok = finiteBits(measured);
    if (ok) {
        ok = op[0] == '<' ? measured <= limit : (op[0] == '>' ? measured >= limit : measured == limit);
    }
    ++g_checks;
    g_fail += ok ? 0 : 1;
    std::printf("%s %s %.6g %s%.6g\n", ok ? "PASS" : "FAIL", name.c_str(), measured, op, limit);
}
// 实测值在 [lo, hi] 里
void checkRange(const std::string& name, double measured, double lo, double hi) {
    check(name + ".min", measured, ">=", lo);
    check(name + ".max", measured, "<=", hi);
}

// ---------------------------------------------------------------- 信号

struct Rng {
    uint32_t s;
    float next() {
        s ^= s << 13;
        s ^= s >> 17;
        s ^= s << 5;
        return static_cast<float>(static_cast<int32_t>(s)) * (1.0f / 2147483648.0f);
    }
};
// 均匀分布白噪声，左右不相关；rms = 有效值（均匀分布：峰值 = rms × √3）
Buf noise(int frames, double rms, uint32_t seed) {
    Rng r{seed};
    Buf b(static_cast<size_t>(frames) * 2);
    const float amp = static_cast<float>(rms * std::sqrt(3.0));
    for (float& v : b) {
        v = r.next() * amp;
    }
    return b;
}
// 正弦；右声道提前四分之一周期（左右搞反了能测出来）
Buf sine(int frames, double hz, double amp, int sr) {
    Buf b(static_cast<size_t>(frames) * 2);
    for (int i = 0; i < frames; ++i) {
        const double ph = 2.0 * kPi * hz * i / sr;
        b[static_cast<size_t>(i) * 2] = static_cast<float>(amp * std::sin(ph));
        b[static_cast<size_t>(i) * 2 + 1] = static_cast<float>(amp * std::sin(ph + 0.5 * kPi));
    }
    return b;
}
Buf logSweep(int frames, double f0, double f1, double amp, int sr) {
    Buf b(static_cast<size_t>(frames) * 2);
    const double T = static_cast<double>(frames) / sr;
    const double k = std::log(f1 / f0);
    for (int i = 0; i < frames; ++i) {
        const double t = static_cast<double>(i) / sr;
        const double ph = 2.0 * kPi * f0 * T / k * (std::exp(k * t / T) - 1.0);
        b[static_cast<size_t>(i) * 2] = static_cast<float>(amp * std::sin(ph));
        b[static_cast<size_t>(i) * 2 + 1] = static_cast<float>(amp * std::cos(ph));
    }
    return b;
}
int framesOf(const Buf& b) {
    return static_cast<int>(b.size() / 2);
}
double rms(const Buf& b, int from, int to) {
    double acc = 0.0;
    for (int i = from * 2; i < to * 2; ++i) {
        acc += static_cast<double>(b[static_cast<size_t>(i)]) * b[static_cast<size_t>(i)];
    }
    return std::sqrt(acc / std::max(1, (to - from) * 2));
}
double db(double x) {
    return 20.0 * std::log10(std::max(x, 1e-12));
}
double maxAbs(const Buf& b, int from, int to) {
    double m = 0.0;
    for (int i = from * 2; i < to * 2; ++i) {
        m = std::max(m, static_cast<double>(std::fabs(b[static_cast<size_t>(i)])));
    }
    return m;
}
double maxDiff(const Buf& a, const Buf& b, int from, int to) {
    double m = 0.0;
    for (int i = from * 2; i < to * 2; ++i) {
        m = std::max(m, std::fabs(static_cast<double>(a[static_cast<size_t>(i)]) - b[static_cast<size_t>(i)]));
    }
    return m;
}
double maxDiff(const Buf& a, const Buf& b) {
    if (a.size() != b.size()) {
        return 1e9;
    }
    return maxDiff(a, b, 0, framesOf(a));
}
// 相邻两个采样之间最大跳多少（每个声道各看各的）：爆音 = 这个数突然很大
double maxStep(const Buf& b, int from, int to) {
    double m = 0.0;
    for (int i = std::max(from, 1); i < to; ++i) {
        for (int c = 0; c < 2; ++c) {
            m = std::max(m, std::fabs(static_cast<double>(b[static_cast<size_t>(i) * 2 + c]) -
                                    b[static_cast<size_t>(i - 1) * 2 + c]));
        }
    }
    return m;
}
double goertzel(const Buf& b, int ch, int from, int to, double hz, int sr) {
    const double c = 2.0 * std::cos(2.0 * kPi * hz / sr);
    double s1 = 0.0, s2 = 0.0;
    for (int i = from; i < to; ++i) {
        const double s0 = b[static_cast<size_t>(i) * 2 + ch] + c * s1 - s2;
        s2 = s1;
        s1 = s0;
    }
    return s1 * s1 + s2 * s2 - c * s1 * s2;
}
// [from, to) 这一段里最强的频率（fLo..fHi，步长 step Hz）
double peakHz(const Buf& b, int from, int to, double fLo, double fHi, double step, int sr) {
    double best = fLo, bestP = -1.0;
    for (double f = fLo; f <= fHi; f += step) {
        const double p = goertzel(b, 0, from, to, f, sr);
        if (p > bestP) {
            bestP = p;
            best = f;
        }
    }
    return best;
}
// [from, from + n) 这一段在 fLo..fHi 里的功率（按 DFT 的频点加起来）
double bandPower(const Buf& b, int from, int n, double fLo, double fHi, int sr) {
    double acc = 0.0;
    const double bin = static_cast<double>(sr) / n;
    for (int k = static_cast<int>(std::ceil(fLo / bin)); k * bin < fHi; ++k) {
        acc += goertzel(b, 0, from, from + n, k * bin, sr);
    }
    return acc;
}

// ---------------------------------------------------------------- 分块跑

using ProcFn = std::function<void(int start, int n, const float* in, float* out)>;

// 把 in 按 pattern（循环着用）的块大小喂给 fn；splits 里的帧号上一定断开（参数在那一帧改）
Buf runBlocks(const Buf& in, const std::vector<int>& pattern, const ProcFn& fn, const std::vector<int>& splits = {}) {
    const int total = framesOf(in);
    Buf out(in.size(), 0.0f);
    int pos = 0;
    size_t pi = 0;
    while (pos < total) {
        int n = std::min(pattern[pi++ % pattern.size()], total - pos);
        for (int s : splits) {
            if (s > pos && s < pos + n) {
                n = s - pos;
            }
        }
        fn(pos, n, &in[static_cast<size_t>(pos) * 2], &out[static_cast<size_t>(pos) * 2]);
        pos += n;
    }
    return out;
}

const std::vector<int> kOneCall = {1 << 30};
const std::vector<int> kIrregular = {1, 37, 4096, 3, 1000, 64, 2, 811, 4095, 129, 5, 2048, 333};

// 块大小无关：一次处理完 vs 64 / 512 / 不规则的块，最大差 ≤ 1e-6
void checkBlockIndep(const std::string& name, const std::function<Buf(const std::vector<int>&)>& run) {
    const Buf ref = run(kOneCall);
    double d = 0.0;
    d = std::max(d, maxDiff(ref, run({64})));
    d = std::max(d, maxDiff(ref, run({512})));
    d = std::max(d, maxDiff(ref, run(kIrregular)));
    check(name + ".blocksize_indep", finiteBuf(ref) ? d : 1e9, "<=", 1e-6);
}

// ---------------------------------------------------------------- CPU

// fn 处理 frames 帧；跑 5 遍取最快的一遍（别的进程抢 CPU 时不至于误判）
template <class F>
double nsPerFrame(int frames, F&& fn) {
    double best = 1e30;
    for (int rep = 0; rep < 5; ++rep) {
        const auto t0 = std::chrono::steady_clock::now();
        fn();
        const auto t1 = std::chrono::steady_clock::now();
        best = std::min(best, std::chrono::duration<double, std::nano>(t1 - t0).count());
    }
    return best / frames;
}
double cpuInfo(const std::string& name, double ns, int sr) {
    const double pct = ns * sr / 1e9 * 100.0;
    std::printf("INFO cpu %s %.1f ns/frame = %.2f%% of one core at %d Hz\n", name.c_str(), ns, pct, sr);
    return pct;
}

// 稳态正弦过一遍 proc，量输出比输入大多少 dB（跳过前 settle 帧）
template <class F>
double sineGainDb(int sr, double hz, int frames, int settle, F&& proc) {
    const Buf x = sine(frames, hz, 0.5, sr);
    Buf y(x.size(), 0.0f);
    proc(x.data(), y.data(), frames);
    return db(rms(y, settle, frames)) - db(rms(x, settle, frames));
}

// ================================================================ BandSplit3 / AlignDelay

struct Bands {
    Buf low, mid, high;
};
Bands split(BandSplit3& bs, const Buf& x, const std::vector<int>& pattern) {
    Bands b;
    b.low.assign(x.size(), 0.0f);
    b.mid.assign(x.size(), 0.0f);
    b.high.assign(x.size(), 0.0f);
    const int total = framesOf(x);
    int pos = 0;
    size_t pi = 0;
    while (pos < total) {
        const int n = std::min(pattern[pi++ % pattern.size()], total - pos);
        const size_t o = static_cast<size_t>(pos) * 2;
        bs.process(&x[o], &b.low[o], &b.mid[o], &b.high[o], n);
        pos += n;
    }
    return b;
}
// low + mid + high（按 float 加，和混音器里一样）减去延迟后的输入：残差的 RMS（dBFS）
double reconResidualDb(int sr, const Buf& x, double lowHz, double highHz) {
    BandSplit3 bs;
    bs.init(sr, lowHz, highHz);
    const Bands b = split(bs, x, {512});
    const int lat = bs.latency();
    double acc = 0.0;
    const int total = framesOf(x);
    for (int i = lat * 2; i < total * 2; ++i) {
        const size_t k = static_cast<size_t>(i);
        const float sum = b.low[k] + b.mid[k] + b.high[k];
        const double e = static_cast<double>(sum) - x[k - static_cast<size_t>(lat) * 2];
        acc += e * e;
    }
    return 10.0 * std::log10(std::max(acc / ((total - lat) * 2), 1e-30));
}

void testBandSplit(int sr) {
    const std::string p = "bandsplit3." + std::to_string(sr) + ".";
    BandSplit3 bs;
    bs.init(sr);
    const int lat = bs.latency();
    std::printf("INFO %slatency %d frames = %.3f ms (LP1 %d taps, LP2 %d taps)\n", p.c_str(), lat, lat * 1000.0 / sr,
            bs.lowTaps(), bs.highTaps());
    check(p + "latency_ms", lat * 1000.0 / sr, "<=", 12.0);

    // 重建：-12 dBFS RMS 的白噪声、20 Hz–20 kHz 的对数扫频
    const Buf wn = noise(4 * sr, std::pow(10.0, -12.0 / 20.0), 12345u);
    std::printf("INFO %srecon.noise_input_rms_dbfs %.2f\n", p.c_str(), db(rms(wn, 0, framesOf(wn))));
    check(p + "recon.noise_residual_dbfs", reconResidualDb(sr, wn, 250.0, 2500.0), "<=", -90.0);
    const Buf sw = logSweep(4 * sr, 20.0, 20000.0, std::pow(10.0, -12.0 / 20.0) * std::sqrt(2.0), sr);
    check(p + "recon.sweep_residual_dbfs", reconResidualDb(sr, sw, 250.0, 2500.0), "<=", -90.0);
    // 分频点可以在 init 里改：照样互补
    check(p + "recon.custom_xover_residual_dbfs", reconResidualDb(sr, wn, 400.0, 4000.0), "<=", -90.0);

    // 隔离：稳态正弦，各段的电平相对输入
    const int n = 2 * sr, settle = sr / 2;
    double g[3][3]; // [频率][段]
    const double freqs[3] = {60.0, 1000.0, 8000.0};
    for (int f = 0; f < 3; ++f) {
        bs.reset();
        const Buf x = sine(n, freqs[f], 0.5, sr);
        const Bands b = split(bs, x, {512});
        const double ref = db(rms(x, settle, n));
        g[f][0] = db(rms(b.low, settle, n)) - ref;
        g[f][1] = db(rms(b.mid, settle, n)) - ref;
        g[f][2] = db(rms(b.high, settle, n)) - ref;
    }
    check(p + "iso.60Hz_into_mid_db", g[0][1], "<=", -45.0);
    check(p + "iso.60Hz_into_high_db", g[0][2], "<=", -45.0);
    check(p + "iso.1kHz_into_low_db", g[1][0], "<=", -45.0);
    check(p + "iso.1kHz_into_high_db", g[1][2], "<=", -45.0);
    check(p + "iso.8kHz_into_low_db", g[2][0], "<=", -45.0);
    check(p + "iso.8kHz_into_mid_db", g[2][1], "<=", -45.0);
    // 自己那一段里不增不减
    check(p + "pass.60Hz_in_low_absdb", std::fabs(g[0][0]), "<=", 0.05);
    check(p + "pass.1kHz_in_mid_absdb", std::fabs(g[1][1]), "<=", 0.05);
    check(p + "pass.8kHz_in_high_absdb", std::fabs(g[2][2]), "<=", 0.05);
    // 分频点上两边各 -6 dB
    for (int k = 0; k < 2; ++k) {
        const double fx = k == 0 ? 250.0 : 2500.0;
        bs.reset();
        const Buf x = sine(n, fx, 0.5, sr);
        const Bands b = split(bs, x, {512});
        const double ref = db(rms(x, settle, n));
        const std::string tag = k == 0 ? "xover.250Hz_" : "xover.2500Hz_";
        checkRange(p + tag + (k == 0 ? "low_db" : "mid_db"), db(rms(k == 0 ? b.low : b.mid, settle, n)) - ref, -6.3, -5.7);
        checkRange(p + tag + (k == 0 ? "mid_db" : "high_db"), db(rms(k == 0 ? b.mid : b.high, settle, n)) - ref, -6.3,
                -5.7);
    }

    // 线性相位：冲激响应关于 latency 那一帧对称；左声道的冲激不漏到右声道
    {
        bs.reset();
        Buf x(static_cast<size_t>(4 * lat + 16) * 2, 0.0f);
        x[0] = 1.0f;
        const Bands b = split(bs, x, {512});
        double asym = 0.0, leak = 0.0;
        for (int k = 1; k <= lat; ++k) {
            for (const Buf* band : {&b.low, &b.mid, &b.high}) {
                asym = std::max(asym, std::fabs(static_cast<double>((*band)[static_cast<size_t>(lat + k) * 2]) -
                                               (*band)[static_cast<size_t>(lat - k) * 2]));
            }
        }
        for (size_t i = 1; i < x.size(); i += 2) {
            leak = std::max(leak, static_cast<double>(std::fabs(b.low[i]) + std::fabs(b.mid[i]) + std::fabs(b.high[i])));
        }
        check(p + "linear_phase.impulse_asymmetry", asym, "<=", 1e-7);
        check(p + "channel_leak", leak, "==", 0.0);
    }

    // 块大小无关（三段接成一条比）；in 和 low 用同一个缓冲也一样
    const Buf x = noise(20000, 0.25, 777u);
    checkBlockIndep(p.substr(0, p.size() - 1), [&](const std::vector<int>& pattern) {
        BandSplit3 s;
        s.init(sr);
        Bands b = split(s, x, pattern);
        b.low.insert(b.low.end(), b.mid.begin(), b.mid.end());
        b.low.insert(b.low.end(), b.high.begin(), b.high.end());
        return b.low;
    });
    {
        BandSplit3 a, c;
        a.init(sr);
        c.init(sr);
        const Bands ref = split(a, x, {512});
        Buf low = x, mid(x.size()), high(x.size());
        for (int pos = 0; pos < framesOf(x); pos += 512) {
            const int m = std::min(512, framesOf(x) - pos);
            const size_t o = static_cast<size_t>(pos) * 2;
            c.process(&low[o], &low[o], &mid[o], &high[o], m);
        }
        check(p + "inplace_diff", std::max(maxDiff(ref.low, low), std::max(maxDiff(ref.mid, mid), maxDiff(ref.high, high))),
                "==", 0.0);
        // reset 之后没有残留
        a.reset();
        const Bands z = split(a, Buf(8192, 0.0f), {512});
        check(p + "reset_residue", std::max(maxAbs(z.low, 0, 4096), std::max(maxAbs(z.mid, 0, 4096), maxAbs(z.high, 0, 4096))),
                "==", 0.0);
    }

    // CPU：一个立体声实例 ≤ 一个核的 3%
    {
        const Buf in = noise(2 * sr, 0.25, 99u);
        Buf lo(in.size()), mi(in.size()), hi(in.size());
        BandSplit3 s;
        s.init(sr);
        const double ns = nsPerFrame(framesOf(in), [&]() {
            for (int pos = 0; pos < framesOf(in); pos += 512) {
                const int m = std::min(512, framesOf(in) - pos);
                const size_t o = static_cast<size_t>(pos) * 2;
                s.process(&in[o], &lo[o], &mi[o], &hi[o], m);
            }
        });
        const double pct = cpuInfo("BandSplit3@" + std::to_string(sr), ns, sr);
        check(p + "cpu_ns_per_frame", ns, "<=", 0.03 * 1e9 / sr);
        check(p + "cpu_pct_of_core", pct, "<=", 3.0);
    }
}

void testAlignDelay() {
    const std::string p = "aligndelay.";
    const Buf x = noise(10000, 0.25, 5u);
    const int d = 264;
    AlignDelay a;
    a.init(d);
    const Buf y = runBlocks(x, {512}, [&](int, int n, const float* in, float* out) { a.process(in, out, n); });
    double e = maxAbs(y, 0, d);
    for (int i = d * 2; i < framesOf(x) * 2; ++i) {
        e = std::max(e, std::fabs(static_cast<double>(y[static_cast<size_t>(i)]) - x[static_cast<size_t>(i - d * 2)]));
    }
    check(p + "exact_delay_diff", e, "==", 0.0);
    checkBlockIndep("aligndelay", [&](const std::vector<int>& pattern) {
        AlignDelay s;
        s.init(d);
        return runBlocks(x, pattern, [&](int, int n, const float* in, float* out) { s.process(in, out, n); });
    });
    // 原地；延迟 0 = 直通
    AlignDelay b, z;
    b.init(d);
    z.init(0);
    Buf w = x;
    b.process(w.data(), w.data(), framesOf(w));
    check(p + "inplace_diff", maxDiff(w, y), "==", 0.0);
    Buf v(x.size());
    z.process(x.data(), v.data(), framesOf(x));
    check(p + "zero_delay_diff", maxDiff(v, x), "==", 0.0);
    const double ns = nsPerFrame(framesOf(x), [&]() { a.process(x.data(), w.data(), framesOf(x)); });
    cpuInfo("AlignDelay", ns, 44100);
}

// ================================================================ TransGate

void testTransGate() {
    const std::string p = "transgate.";
    const int sr = 44100;
    const double dbeat = 128.0 / 60.0 / sr;
    const double edgeFrames = kGateEdgeMs * sr / 1000.0;
    const double stepLimit = 2.0 / edgeFrames; // 斜率限制：每个采样最多这么多
    const int total = static_cast<int>(8.0 / dbeat);
    const Buf wn = noise(total, 0.25, 11u);
    const Buf dc(static_cast<size_t>(total) * 2, 1.0f);

    auto run = [&](TransGate& g, const Buf& in, const std::vector<int>& pattern, double beatStart = 0.0) {
        return runBlocks(in, pattern,
                [&](int start, int n, const float* i, float* o) { g.process(i, o, n, beatStart + start * dbeat, dbeat); });
    };

    // depth = 0：和输入逐位相同
    {
        TransGate g;
        g.init(sr);
        g.set(0.5, 0.5, 0.0);
        check(p + "depth0_identity_diff", maxDiff(run(g, wn, {512}), wn), "==", 0.0);
    }
    // 纯函数：开着 = 1，关着 = 1 - depth
    {
        TransGate g;
        g.init(sr);
        g.set(0.5, 0.5, 0.8);
        check(p + "gain_open", g.gain(0.1, dbeat), "==", 1.0);
        check(p + "gain_at_beat", g.gain(1.0, dbeat), "==", 1.0);
        check(p + "gain_closed_err", std::fabs(g.gain(0.4, dbeat) - 0.2), "<=", 1e-12);
        check(p + "gain_static_err",
                std::fabs(TransGate::gainAt(0.4, edgeFrames * dbeat, 0.5, 0.5, 0.8, 0.0) - g.gain(0.4, dbeat)), "==", 0.0);
    }
    // 直流过门 = 增益本身：和纯函数一致；沿的长度 1–2 ms；每个采样的变化有界
    {
        TransGate g;
        g.init(sr);
        g.set(0.5, 0.5, 1.0);
        const Buf y = run(g, dc, {512});
        double e = 0.0;
        int openCount = 0;
        for (int i = 0; i < total; ++i) {
            e = std::max(e, std::fabs(y[static_cast<size_t>(i) * 2] - g.gain(i * dbeat, dbeat)));
            openCount += y[static_cast<size_t>(i) * 2] > 0.5f ? 1 : 0;
        }
        check(p + "process_vs_pure_gain", e, "<=", 1e-6);
        // 第一个周期：0.25 拍处关、0.5 拍处开
        const int closeAt = static_cast<int>(0.25 / dbeat), openAt = static_cast<int>(0.5 / dbeat);
        int nClose = 0, nOpen = 0;
        for (int i = closeAt - 200; i < closeAt + 400; ++i) {
            const float v = y[static_cast<size_t>(i) * 2];
            nClose += (v > 1e-6f && v < 1.0f - 1e-6f) ? 1 : 0;
        }
        for (int i = openAt - 400; i < openAt + 200; ++i) {
            const float v = y[static_cast<size_t>(i) * 2];
            nOpen += (v > 1e-6f && v < 1.0f - 1e-6f) ? 1 : 0;
        }
        checkRange(p + "edge_close_ms", nClose * 1000.0 / sr, 1.0, 2.0);
        checkRange(p + "edge_open_ms", nOpen * 1000.0 / sr, 1.0, 2.0);
        // 开门的沿在拍点之前走完：拍点那个采样已经全开
        check(p + "open_at_beat", y[static_cast<size_t>(std::ceil(0.5 / dbeat)) * 2], "==", 1.0);
        check(p + "max_step", maxStep(y, 0, total), "<=", stepLimit);
        // 占空比：增益 > 0.5 的时间 = duty + 一个沿（关的沿的前一半 + 开的沿的后一半）
        const double expectDuty = 0.5 + edgeFrames * dbeat / 0.5;
        check(p + "duty_err", std::fabs(static_cast<double>(openCount) / total - expectDuty), "<=", 0.002);
        check(p + "bounded_gain", maxAbs(y, 0, total), "<=", 1.0);
    }
    // 参数在块边界上一下改了（深度 0 → 1 时正好在关着的那一段；周期 / 相位改了门当场翻转）：不出爆音
    {
        TransGate g;
        g.init(sr);
        g.set(0.5, 0.5, 0.0);
        const int f1 = static_cast<int>(0.4 / dbeat), f2 = static_cast<int>(2.1 / dbeat), f3 = static_cast<int>(4.3 / dbeat);
        const Buf y = runBlocks(dc, {512},
                [&](int start, int n, const float* i, float* o) {
                    if (start == f1) {
                        g.set(0.5, 0.5, 1.0);
                    } else if (start == f2) {
                        g.set(0.37, 0.3, 1.0, 0.11);
                    } else if (start == f3) {
                        g.set(0.37, 0.3, 0.0, 0.11);
                    }
                    g.process(i, o, n, start * dbeat, dbeat);
                },
                {f1, f2, f3});
        check(p + "param_jump_max_step", maxStep(y, 0, total), "<=", stepLimit * 1.000001);
    }
    // 块大小无关；从中间开始 = 一路放过来（相位只看绝对拍）
    auto mk = [&](TransGate& g) {
        g.init(sr);
        g.set(0.75, 0.3, 0.9, 0.123);
    };
    checkBlockIndep("transgate", [&](const std::vector<int>& pattern) {
        TransGate g;
        mk(g);
        return run(g, wn, pattern, 17.3);
    });
    {
        TransGate a, b;
        mk(a);
        mk(b);
        const Buf full = run(a, wn, {512});
        const int m = 123457;
        const Buf tail(wn.begin() + m * 2, wn.end());
        const Buf part = run(b, tail, {512}, m * dbeat);
        const Buf fullTail(full.begin() + m * 2, full.end());
        check(p + "restore_at_beat_diff", maxDiff(part, fullTail), "<=", 1e-6);
    }
    {
        TransGate g;
        mk(g);
        Buf o(wn.size());
        const double ns = nsPerFrame(total, [&]() {
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                g.process(&wn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], 512, pos * dbeat, dbeat);
            }
        });
        cpuInfo("TransGate", ns, sr);
    }
}

// ================================================================ Roll

// 独立算一遍「第 j 帧该读录音的第几帧」：只用拍和取整，不看实现（全场从 0 拍起，第 j 帧 = j × dbeat 拍）
struct RollRef {
    double dbeat = 0.0, capBeat = 0.0, len0 = 0.0;
    std::vector<std::pair<double, double>> lens; // (从哪一拍起, 新长度)，按拍排好

    long long frameOf(double beat) const { return static_cast<long long>(std::ceil(beat / dbeat - 1e-6)); }
    long long f0() const { return frameOf(capBeat); }
    long long rollStart() const { return frameOf(capBeat + len0); }
    // j ≥ rollStart()：u = 读录音的第几帧；since = 离上一个不连续点（回绕 / 改长度 / 进 roll）多少帧
    void at(long long j, long long& u, long long& since) const {
        double len = len0;
        long long c = rollStart();
        for (const auto& e : lens) {
            const long long ce = std::max(rollStart(), frameOf(e.first));
            if (ce <= j) {
                len = e.second;
                c = ce;
            }
        }
        long long n = static_cast<long long>(std::floor((j * dbeat - capBeat) / len));
        while (frameOf(capBeat + n * len) > j) {
            --n;
        }
        while (frameOf(capBeat + (n + 1) * len) <= j) {
            ++n;
        }
        const long long r = frameOf(capBeat + n * len);
        u = j - r;
        since = j - std::max(r, c);
    }
};

void testRoll() {
    const std::string p = "roll.";
    const int sr = 44100;
    const int xf = static_cast<int>(std::lround(kXfadeMs * sr / 1000.0));
    const double dbeat = 128.0 / 60.0 / sr; // 一拍 = 20671.875 帧：每一遍的起点都落在不同的小数上
    const int total = static_cast<int>(11.0 / dbeat);
    const Buf wn = noise(total, 0.25, 21u);

    auto run = [&](Roll& r, const Buf& in, const std::vector<int>& pattern, double db, const ProcFn& pre = nullptr,
                       const std::vector<int>& splits = {}) {
        return runBlocks(in, pattern,
                [&](int start, int n, const float* i, float* o) {
                    if (pre) {
                        pre(start, n, i, o);
                    }
                    r.process(i, o, n, start * db, db);
                },
                splits);
    };
    // 输出和「照参考读录音」差多少（只看离不连续点 ≥ xf 的帧）；passThrough = 进 roll 之前和输入差多少
    auto contentDiff = [&](const Buf& in, const Buf& out, const RollRef& ref, long long until, double& passThrough) {
        const long long rs = ref.rollStart(), f0 = ref.f0();
        passThrough = maxDiff(in, out, 0, static_cast<int>(rs));
        double d = 0.0;
        for (long long j = rs; j < until; ++j) {
            long long u, since;
            ref.at(j, u, since);
            if (since < xf) {
                continue;
            }
            for (int c = 0; c < 2; ++c) {
                d = std::max(d, std::fabs(static_cast<double>(out[static_cast<size_t>(j) * 2 + c]) -
                                        in[static_cast<size_t>(f0 + u) * 2 + c]));
            }
        }
        return d;
    };

    // 录 1 拍、原样重放 6 遍：录的时候直通，之后每一遍都是录下来的那一段，起点对在拍上
    {
        Roll r;
        r.init(sr);
        r.arm(4.0, 1.0);
        const Buf y = run(r, wn, {512}, dbeat);
        RollRef ref;
        ref.dbeat = dbeat;
        ref.capBeat = 4.0;
        ref.len0 = 1.0;
        double pass = 0.0;
        const double d = contentDiff(wn, y, ref, total, pass);
        check(p + "capture_passthrough_diff", pass, "==", 0.0);
        check(p + "repeat_content_diff", d, "<=", 1e-6);
        check(p + "bounded_excess", maxAbs(y, 0, total) - maxAbs(wn, 0, total), "<=", 1e-6);
    }
    // 1 → 1/2 → 1/4 → 1/8 拍，然后松开：每一段都读对，松开 2 ms 之后和输入逐位相同
    {
        Roll r;
        r.init(sr);
        r.arm(4.0, 1.0);
        r.setLength(6.0, 0.5);
        r.setLength(7.0, 0.25);
        r.setLength(7.75, 0.125);
        r.release(9.0);
        const Buf y = run(r, wn, {512}, dbeat);
        RollRef ref;
        ref.dbeat = dbeat;
        ref.capBeat = 4.0;
        ref.len0 = 1.0;
        ref.lens = {{6.0, 0.5}, {7.0, 0.25}, {7.75, 0.125}};
        const long long rel = ref.frameOf(9.0);
        double pass = 0.0;
        check(p + "setlength_content_diff", contentDiff(wn, y, ref, rel, pass), "<=", 1e-6);
        check(p + "release_passthrough_diff", maxDiff(wn, y, static_cast<int>(rel) + xf, total), "==", 0.0);
        check(p + "release_done_active", r.active() ? 1.0 : 0.0, "==", 0.0);
    }
    // mix = 0：全程和输入逐位相同
    {
        Roll r;
        r.init(sr);
        r.setMix(0.0f);
        r.arm(4.0, 1.0);
        r.setLength(6.0, 0.25);
        check(p + "mix0_identity_diff", maxDiff(run(r, wn, {512}, dbeat), wn), "==", 0.0);
    }
    // 不出爆音：111.3 Hz 正弦（一拍不是整数个周期，硬回绕会跳），回绕 / 改长度（含不在回绕点上的）/ mix 跳变 / 松开
    {
        const Buf s = sine(total, 111.3, 0.5, sr);
        Roll r;
        r.init(sr);
        r.arm(4.0, 1.0);
        r.setLength(7.0, 0.5);
        r.setLength(8.0, 0.25);
        r.setLength(8.6, 0.125);
        r.release(10.3);
        const int m1 = static_cast<int>(9.1 / dbeat), m2 = static_cast<int>(9.5 / dbeat);
        const Buf y = run(r, s, {512}, dbeat,
                [&](int start, int, const float*, float*) {
                    if (start == m1) {
                        r.setMix(0.3f);
                    } else if (start == m2) {
                        r.setMix(1.0f);
                    }
                },
                {m1, m2});
        std::printf("INFO %sinput_max_step %.5f (hard wrap would step up to ~1.0)\n", p.c_str(), maxStep(s, 0, total));
        check(p + "click_max_step", maxStep(y, 0, total), "<=", 0.04);
    }
    // 块大小无关（含改长度、松开、mix < 1）；全场拍从 1000.123 起、速度不是整数
    {
        const double db2 = 133.33 / 60.0 / sr, b0 = 1000.123;
        checkBlockIndep("roll", [&](const std::vector<int>& pattern) {
            Roll r;
            r.init(sr);
            r.setMix(0.8f);
            r.arm(b0 + 4.0, 1.0);
            r.setLength(b0 + 7.0, 0.5);
            r.setLength(b0 + 8.0, 0.25);
            r.release(b0 + 9.3);
            return runBlocks(wn, pattern,
                    [&](int start, int n, const float* i, float* o) { r.process(i, o, n, b0 + start * db2, db2); });
        });
    }
    // 从中间（录之前）开始 = 一路放过来
    {
        auto mk = [&](Roll& r) {
            r.init(sr);
            r.arm(4.0, 1.0);
            r.setLength(7.0, 0.5);
            r.release(9.3);
        };
        Roll a, b;
        mk(a);
        mk(b);
        const Buf full = run(a, wn, {512}, dbeat);
        const int m = static_cast<int>(3.2 / dbeat);
        const Buf tail(wn.begin() + m * 2, wn.end());
        const Buf part = runBlocks(tail, {512},
                [&](int start, int n, const float* i, float* o) { b.process(i, o, n, (m + start) * dbeat, dbeat); });
        const Buf fullTail(full.begin() + m * 2, full.end());
        check(p + "restore_at_beat_diff", maxDiff(part, fullTail), "<=", 1e-6);
    }
    // 录完之后全场变速（+3%）：roll 仍然对在拍上，放多少遍都不漂
    {
        const double db2 = dbeat * 1.03;
        const int fc = static_cast<int>(5.5 / dbeat); // 这一帧起变速
        auto beatOf = [&](double j) { return j < fc ? j * dbeat : fc * dbeat + (j - fc) * db2; };
        RollRef ref;
        ref.dbeat = dbeat;
        ref.capBeat = 4.0;
        ref.len0 = 1.0;
        const int burstAt = static_cast<int>(ref.f0()) + 5000;
        const int totalT = static_cast<int>(fc + (20.0 - 5.5) / db2);
        Buf x(static_cast<size_t>(totalT) * 2, 0.0f);
        for (int k = 0; k < 64; ++k) { // 64 帧的 Hann 脉冲
            const float v = static_cast<float>(0.8 * (0.5 - 0.5 * std::cos(2.0 * kPi * (k + 0.5) / 64.0)));
            x[static_cast<size_t>(burstAt + k) * 2] = v;
            x[static_cast<size_t>(burstAt + k) * 2 + 1] = v;
        }
        const double burstBeat = (burstAt + 31.5) * dbeat;
        Roll r;
        r.init(sr);
        r.arm(4.0, 1.0);
        const Buf y = runBlocks(x, {512},
                [&](int start, int n, const float* i, float* o) {
                    r.process(i, o, n, beatOf(start), start < fc ? dbeat : db2);
                },
                {fc});
        double worst = 0.0, last = 0.0;
        for (int n = 1; n <= 14; ++n) {
            const double expectBeat = burstBeat + n;
            // 这一遍的脉冲：在预期位置前后 0.05 拍里找能量的重心
            double e = 0.0, ej = 0.0;
            for (int j = static_cast<int>(ref.rollStart()); j < totalT; ++j) {
                const double bj = beatOf(j);
                if (bj < expectBeat - 0.05 || bj > expectBeat + 0.05) {
                    continue;
                }
                const double v = static_cast<double>(y[static_cast<size_t>(j) * 2]) * y[static_cast<size_t>(j) * 2];
                e += v;
                ej += v * j;
            }
            const double err = e > 0.0 ? (beatOf(ej / e) - expectBeat) / (ej / e < fc ? dbeat : db2) : 1e9; // 帧
            worst = std::max(worst, std::fabs(err));
            last = err;
        }
        // 每一遍的起点取整到帧：对拍的误差 < 1 帧，而且不随遍数累积
        std::printf("INFO %stempo_change last_pass_offset_frames %.3f\n", p.c_str(), last);
        check(p + "tempo_change_beat_align_frames", worst, "<=", 1.0);
        check(p + "tempo_change_bounded", maxAbs(y, 0, totalT), "<=", 1.0);
    }
    // 最长 4 拍 @ 60 BPM；要 8 拍的按 4 拍录
    {
        const double db60 = 60.0 / 60.0 / sr;
        const int t60 = 11 * sr;
        const Buf in = noise(t60, 0.25, 31u);
        for (int k = 0; k < 2; ++k) {
            Roll r;
            r.init(sr);
            r.arm(1.0, k == 0 ? 4.0 : 8.0);
            const Buf y = run(r, in, {512}, db60);
            RollRef ref;
            ref.dbeat = db60;
            ref.capBeat = 1.0;
            ref.len0 = 4.0;
            double pass = 0.0;
            const double d = contentDiff(in, y, ref, t60, pass);
            check(p + (k == 0 ? "max_length_4beats_60bpm_diff" : "over_length_clamped_diff"), std::max(d, pass), "<=", 1e-6);
        }
    }
    {
        Roll r;
        r.init(sr);
        r.arm(0.5, 0.25);
        Buf o(wn.size());
        const double ns = nsPerFrame(total, [&]() {
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                r.process(&wn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], 512, pos * dbeat, dbeat);
            }
        });
        cpuInfo("Roll(rolling)", ns, sr);
    }
}

// ================================================================ SvfFilter

void testSvf() {
    const std::string p = "svf.";
    const int sr = 44100;
    auto gainDb = [&](SvfFilter::Mode mode, double fc, double q, double hz) {
        SvfFilter f;
        f.init(sr);
        f.set(mode, fc, q);
        return sineGainDb(sr, hz, sr, sr / 2, [&](const float* in, float* out, int n) { f.process(in, out, n); });
    };
    // 频响和模拟原型（双线性、截止处预畸变）对得上
    check(p + "lpf_at_fc_err_db", std::fabs(gainDb(SvfFilter::LPF, 1000.0, 0.7071, 1000.0) + 3.0103), "<=", 0.05);
    check(p + "lpf_passband_err_db", std::fabs(gainDb(SvfFilter::LPF, 1000.0, 0.7071, 100.0)), "<=", 0.05);
    check(p + "lpf_stop_8fc_db", gainDb(SvfFilter::LPF, 1000.0, 0.7071, 8000.0), "<=", -36.0);
    check(p + "hpf_at_fc_err_db", std::fabs(gainDb(SvfFilter::HPF, 1000.0, 0.7071, 1000.0) + 3.0103), "<=", 0.05);
    check(p + "hpf_passband_err_db", std::fabs(gainDb(SvfFilter::HPF, 1000.0, 0.7071, 15000.0)), "<=", 0.05);
    check(p + "hpf_stop_fc10_db", gainDb(SvfFilter::HPF, 1000.0, 0.7071, 100.0), "<=", -39.5);
    check(p + "bpf_at_fc_err_db", std::fabs(gainDb(SvfFilter::BPF, 1000.0, 4.0, 1000.0)), "<=", 0.05);
    check(p + "bpf_octave_below_err_db", std::fabs(gainDb(SvfFilter::BPF, 1000.0, 4.0, 500.0) + 15.70), "<=", 0.1);
    check(p + "lpf_resonance_q8_err_db", std::fabs(gainDb(SvfFilter::LPF, 1000.0, 8.0, 1000.0) - 18.062), "<=", 0.05);
    // 截止在 0.45 × 采样率：频率照样准
    check(p + "lpf_at_0.45fs_err_db", std::fabs(gainDb(SvfFilter::LPF, 0.45 * sr, 0.7071, 0.45 * sr) + 3.0103), "<=", 0.1);

    // 三种模式的输出加起来 = 输入（TPT SVF 的恒等式：没有「旁路」，用这个当中性检查）
    {
        const Buf x = noise(sr, 0.25, 47u);
        Buf sum(x.size(), 0.0f);
        for (int m = 0; m < 3; ++m) {
            SvfFilter f;
            f.init(sr);
            f.set(static_cast<SvfFilter::Mode>(m), 700.0, 3.0);
            Buf y(x.size());
            f.process(x.data(), y.data(), framesOf(x));
            for (size_t i = 0; i < x.size(); ++i) {
                sum[i] += y[i];
            }
        }
        check(p + "lpf_hpf_bpf_sum_identity_diff", maxDiff(sum, x), "<=", 1e-6);
    }
    // 稳定：截止顶到 0.45 fs（再高会被限住）、Q 顶到最大，满幅白噪声；停了输入之后衰减干净
    {
        SvfFilter f;
        f.init(sr);
        f.set(SvfFilter::LPF, 0.6 * sr, 1000.0);
        Buf x = noise(6 * sr, 1.0 / std::sqrt(3.0), 41u);
        std::fill(x.begin() + static_cast<long>(5 * sr) * 2, x.end(), 0.0f);
        const Buf y = runBlocks(x, {512}, [&](int, int n, const float* in, float* out) { f.process(in, out, n); });
        check(p + "stable_0.45fs_qmax_peak", finiteBuf(y) ? maxAbs(y, 0, 5 * sr) : 1e9, "<=", 100.0);
        check(p + "stable_0.45fs_qmax_tail", maxAbs(y, 5 * sr + sr / 2, 6 * sr), "<=", 1e-6);
    }
    // 每 16 帧随机换截止（20 Hz–0.45 fs）、Q（0.5–20）、模式，满幅白噪声 10 秒：不发散
    {
        SvfFilter f;
        f.init(sr);
        Rng r{4242u};
        const Buf x = noise(10 * sr, 1.0 / std::sqrt(3.0), 43u);
        const Buf y = runBlocks(x, {16}, [&](int, int n, const float* in, float* out) {
            const double u = 0.5 + 0.5 * r.next(), v = 0.5 + 0.5 * r.next(), w = 0.5 + 0.5 * r.next();
            f.set(static_cast<SvfFilter::Mode>(std::min(2, static_cast<int>(w * 3.0))),
                    20.0 * std::pow(0.45 * sr / 20.0, u), 0.5 + 19.5 * v);
            f.process(in, out, n);
        });
        check(p + "random_modulation_peak", finiteBuf(y) ? maxAbs(y, 0, 10 * sr) : 1e9, "<=", 100.0);
    }
    // 扫频 / 换模式不出爆音：100 Hz 正弦，每 512 帧截止在两头之间跳一次
    {
        const Buf s = sine(2 * sr, 100.0, 0.5, sr);
        std::printf("INFO %sinput_max_step %.5f\n", p.c_str(), maxStep(s, 0, 2 * sr));
        const struct {
            const char* name;
            SvfFilter::Mode mode;
            double a, b, q;
        } cases[] = {{"lpf", SvfFilter::LPF, 150.0, 15000.0, 0.7071},
                {"hpf", SvfFilter::HPF, 30.0, 5000.0, 0.7071},
                {"bpf", SvfFilter::BPF, 100.0, 3000.0, 2.0}};
        for (const auto& c : cases) {
            SvfFilter f;
            f.init(sr);
            f.set(c.mode, c.a, c.q);
            const Buf y = runBlocks(s, {512}, [&](int start, int n, const float* in, float* out) {
                f.setCutoff((start / 512) % 2 == 0 ? c.a : c.b);
                f.process(in, out, n);
            });
            check(p + "sweep_click_max_step_" + c.name, maxStep(y, 0, 2 * sr), "<=", 0.03);
        }
        SvfFilter f;
        f.init(sr);
        const Buf y = runBlocks(s, {4096}, [&](int start, int n, const float* in, float* out) {
            f.set(static_cast<SvfFilter::Mode>((start / 4096) % 3), 400.0, 1.0);
            f.process(in, out, n);
        });
        check(p + "mode_switch_max_step", maxStep(y, 0, 2 * sr), "<=", 0.03);
    }
    // 块大小无关（中间改三次参数）；reset 清状态
    const Buf wn = noise(30000, 0.25, 45u);
    checkBlockIndep("svf", [&](const std::vector<int>& pattern) {
        SvfFilter f;
        f.init(sr);
        f.set(SvfFilter::LPF, 300.0, 2.0);
        return runBlocks(wn, pattern,
                [&](int start, int n, const float* in, float* out) {
                    if (start == 7000) {
                        f.set(SvfFilter::BPF, 5000.0, 6.0);
                    } else if (start == 15001) {
                        f.setCutoff(80.0);
                    } else if (start == 22222) {
                        f.set(SvfFilter::HPF, 12000.0, 0.7);
                    }
                    f.process(in, out, n);
                },
                {7000, 15001, 22222});
    });
    {
        SvfFilter f;
        f.init(sr);
        f.set(SvfFilter::LPF, 200.0, 10.0);
        Buf y(wn.size());
        f.process(wn.data(), y.data(), framesOf(wn));
        f.reset();
        const Buf z(2048, 0.0f);
        Buf o(2048);
        f.process(z.data(), o.data(), 1024);
        check(p + "reset_residue", maxAbs(o, 0, 1024), "<=", 1e-12);
        const double ns = nsPerFrame(framesOf(wn), [&]() { f.process(wn.data(), y.data(), framesOf(wn)); });
        cpuInfo("SvfFilter", ns, sr);
    }
}

// ================================================================ Spiral

void testSpiral() {
    const std::string p = "spiral.";
    const int sr = 44100;
    const double dbeat = 120.0 / 60.0 / sr; // 一拍 = 22050 帧
    auto run = [&](Spiral& s, const Buf& in, const std::vector<int>& pattern) {
        return runBlocks(in, pattern,
                [&](int start, int n, const float* i, float* o) { s.process(i, o, n, start * dbeat, dbeat); });
    };

    // mix = 0：和输入逐位相同（延迟线照样在转）
    {
        const Buf wn = noise(3 * sr, 0.25, 51u);
        Spiral s;
        s.init(sr);
        s.set(0.5, 0.9f, 0.0f);
        check(p + "mix0_identity_diff", maxDiff(run(s, wn, {512}), wn), "==", 0.0);
    }
    // 回声的位置和大小：0.5 拍 = 11025 帧；mix 0.5 = 干声 + 满量回声；每一圈 × feedback
    {
        Buf x(static_cast<size_t>(2 * sr) * 2, 0.0f);
        x[200] = x[201] = 0.5f; // 第 100 帧
        Spiral s;
        s.init(sr);
        s.set(0.5, 0.5f, 0.5f);
        const Buf y = run(s, x, {512});
        int pk = 0;
        for (int i = 5000; i < 20000; ++i) {
            if (std::fabs(y[static_cast<size_t>(i) * 2]) > std::fabs(y[static_cast<size_t>(pk) * 2])) {
                pk = i;
            }
        }
        check(p + "echo_position_frames", pk, "==", 100 + 11025);
        check(p + "dry_level_err", std::fabs(y[200] - 0.5), "<=", 1e-6);
        check(p + "echo1_level_err", std::fabs(y[static_cast<size_t>(100 + 11025) * 2] - 0.5), "<=", 1e-4);
        check(p + "echo2_feedback_err", std::fabs(y[static_cast<size_t>(100 + 22050) * 2] - 0.25), "<=", 1e-4);
    }
    // 延迟是绝对拍的纯函数：滑完 +12 个半音 = 延迟减半
    {
        Spiral s;
        s.init(sr);
        s.set(0.5, 0.9f, 1.0f);
        s.setGlide(1.0, 16.0, 12.0);
        check(p + "delay_before_glide_beats", s.delayBeats(0.3), "==", 0.5);
        check(p + "delay_mid_glide_err", std::fabs(s.delayBeats(9.0) - 0.5 / std::sqrt(2.0)), "<=", 1e-12);
        check(p + "delay_after_glide_err", std::fabs(s.delayBeats(40.0) - 0.25), "<=", 1e-12);
    }
    // 音高：环路里转着一小段 500 Hz（升）/ 500 Hz（降），滑完之后量回声的频率
    for (int dir = 0; dir < 2; ++dir) {
        const double semis = dir == 0 ? 12.0 : -12.0;
        const double base = dir == 0 ? 0.5 : 0.25;
        const int total = static_cast<int>(22.5 / dbeat);
        Buf x(static_cast<size_t>(total) * 2, 0.0f);
        const int burst = static_cast<int>(0.8 * base / dbeat);
        for (int i = 0; i < burst; ++i) { // 0.8 × 延迟 这么长的一段正弦，两头 5 ms 渐变
            const double env = std::min(1.0, std::min(i, burst - i) / (0.005 * sr));
            const float v = static_cast<float>(0.5 * env * std::sin(2.0 * kPi * 500.0 * i / sr));
            x[static_cast<size_t>(i) * 2] = v;
            x[static_cast<size_t>(i) * 2 + 1] = v;
        }
        Spiral s;
        s.init(sr);
        s.set(base, 0.97f, 1.0f);
        s.setGlide(1.0, 16.0, semis);
        const Buf y = run(s, x, {512});
        const double hz = peakHz(y, static_cast<int>(18.0 / dbeat), static_cast<int>(22.0 / dbeat), 150.0, 1500.0, 1.0, sr);
        const double st = 12.0 * std::log2(hz / 500.0);
        std::printf("INFO %spitch_%s measured %.1f Hz = %+.2f semitones (target %+.0f)\n", p.c_str(), dir == 0 ? "up" : "down",
                hz, st, semis);
        check(p + (dir == 0 ? "pitch_up_12_err_semitones" : "pitch_down_12_err_semitones"), std::fabs(st - semis), "<=", 1.0);
        check(p + (dir == 0 ? "pitch_up_level" : "pitch_down_level"),
                rms(y, static_cast<int>(18.0 / dbeat), static_cast<int>(22.0 / dbeat)), ">=", 0.005);
    }
    // 反馈顶到最大（给 1.5 也只到 kSpiralMaxFeedback），满幅白噪声 20 秒：有界；停了输入之后尾音在衰减
    for (int k = 0; k < 2; ++k) {
        Buf x = noise(30 * sr, 1.0 / std::sqrt(3.0), 53u);
        std::fill(x.begin() + static_cast<long>(20 * sr) * 2, x.end(), 0.0f);
        Spiral s;
        s.init(sr);
        s.set(0.2503, 1.5f, k == 0 ? 0.5f : 1.0f);
        if (k == 1) {
            s.setGlide(4.0, 8.0, 7.0);
        }
        const Buf y = run(s, x, {512});
        // 干声 ≤ 1，回声 ≤ kFbClamp × 插值的最大过冲 1.25
        check(p + (k == 0 ? "max_feedback_peak_mix0.5" : "max_feedback_peak_glide_mix1"),
                finiteBuf(y) ? maxAbs(y, 0, 30 * sr) : 1e9, "<=", (k == 0 ? 1.0 : 0.0) + 1.25 * kFbClamp);
        if (k == 0) {
            const double early = rms(y, 21 * sr, 22 * sr), late = rms(y, 29 * sr, 30 * sr);
            check(p + "max_feedback_tail_decay_ratio", late / early, "<=", 0.6);
        }
    }
    // 不出爆音：200 Hz 正弦；全场一下快 2%（延迟帧数跳 216 帧）、mix / feedback 跳变
    {
        const int total = 6 * sr;
        const Buf sn = sine(total, 200.0, 0.5, sr);
        const int fc = 3 * sr - 100, m1 = sr, m2 = 2 * sr;
        Spiral s;
        s.init(sr);
        s.set(0.5, 0.5f, 0.0f);
        const Buf y = runBlocks(sn, {512},
                [&](int start, int n, const float* i, float* o) {
                    if (start == m1) {
                        s.set(0.5, 0.5f, 1.0f);
                    } else if (start == m2) {
                        s.set(0.5, 0.2f, 0.5f);
                    }
                    const double b = start < fc ? start * dbeat : fc * dbeat + (start - fc) * dbeat * 1.02;
                    s.process(i, o, n, b, start < fc ? dbeat : dbeat * 1.02);
                },
                {fc, m1, m2});
        std::printf("INFO %sinput_max_step %.5f\n", p.c_str(), maxStep(sn, 0, total));
        check(p + "click_max_step", maxStep(y, 0, total), "<=", 0.06);
    }
    // 块大小无关（正在滑）
    const Buf wn = noise(static_cast<int>(8.0 / dbeat), 0.25, 55u);
    checkBlockIndep("spiral", [&](const std::vector<int>& pattern) {
        Spiral s;
        s.init(sr);
        s.set(0.37, 0.9f, 0.6f);
        s.setGlide(2.0, 4.0, 7.0);
        return run(s, wn, pattern);
    });
    {
        Spiral s;
        s.init(sr);
        s.set(0.37, 0.9f, 0.6f);
        s.setGlide(2.0, 4.0, 7.0);
        Buf o(wn.size());
        const int total = framesOf(wn);
        const double ns = nsPerFrame(total, [&]() {
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                s.process(&wn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], 512, pos * dbeat, dbeat);
            }
        });
        cpuInfo("Spiral", ns, sr);
    }
}

// ================================================================ Noise

void testNoise() {
    const std::string p = "noise.";
    const int sr = 44100;
    const double dbeat = 120.0 / 60.0 / sr;
    const int total = static_cast<int>(8.0 / dbeat);
    auto gen = [&](Noise& nz, const Buf& base, const std::vector<int>& pattern) {
        return runBlocks(base, pattern, [&](int start, int n, const float* i, float* o) {
            std::memcpy(o, i, static_cast<size_t>(n) * 2 * sizeof(float));
            nz.process(o, n, start * dbeat, dbeat);
        });
    };
    auto mk = [&](Noise& nz) {
        nz.init(sr);
        nz.setQ(4.0);
        nz.setSweep(0.0, 8.0, 500.0, 8000.0);
        nz.setLevel(1.0f);
    };
    const Buf zero(static_cast<size_t>(total) * 2, 0.0f);

    // 确定：两个实例逐位相同；reset 之后重新来一遍也相同；换种子就不同
    Noise a, b;
    mk(a);
    mk(b);
    const Buf ya = gen(a, zero, {512});
    check(p + "deterministic_diff", maxDiff(ya, gen(b, zero, {512})), "==", 0.0);
    a.reset();
    check(p + "reset_reseeds_diff", maxDiff(ya, gen(a, zero, {512})), "==", 0.0);
    b.setSeed(12345u);
    b.reset();
    check(p + "other_seed_differs", maxDiff(ya, gen(b, zero, {512})), ">=", 0.01);
    // 加出来的：out 里原来的东西原样留着，加上去的和输入无关
    {
        const Buf base = sine(total, 300.0, 0.4, sr);
        a.reset();
        Buf y = gen(a, base, {512});
        for (size_t i = 0; i < y.size(); ++i) {
            y[i] -= base[i];
        }
        check(p + "additive_ignores_input_diff", maxDiff(y, ya), "<=", 1e-6);
        Noise z;
        mk(z);
        z.setLevel(0.0f);
        check(p + "level0_adds_nothing_diff", maxDiff(gen(z, base, {512}), base), "==", 0.0);
    }
    check(p + "bounded_peak", finiteBuf(ya) ? maxAbs(ya, 0, total) : 1e9, "<=", 4.0);
    check(p + "audible_rms", rms(ya, 0, total), ">=", 0.05);
    // level 从 0 一下开到 1：2 ms 里滑上去，前 8 个采样还很小
    {
        Noise z;
        mk(z);
        z.setLevel(0.0f);
        Buf o(static_cast<size_t>(4096) * 2, 0.0f);
        z.process(o.data(), 1024, 0.0, dbeat);
        z.setLevel(1.0f);
        z.process(o.data() + 2048, 1024, 1024 * dbeat, dbeat);
        // 一直开着的那一个：噪声序列相同，所以滑完（2 ms）之后两边逐位相同；头 8 个采样最多到它的 8/88
        Noise full;
        mk(full);
        Buf of(static_cast<size_t>(4096) * 2, 0.0f);
        full.process(of.data(), 1024, 0.0, dbeat);
        full.process(of.data() + 2048, 1024, 1024 * dbeat, dbeat);
        const int ramp = static_cast<int>(std::ceil(kParamSmoothMs * sr / 1000.0));
        check(p + "level_on_first_samples_ratio", maxAbs(o, 1024, 1032) / maxAbs(of, 1024, 1032), "<=", 8.0 / (ramp - 1));
        check(p + "level_on_full_after_ramp_diff", maxDiff(o, of, 1024 + ramp, 2048), "==", 0.0);
    }
    // 频谱：中心频率是拍的纯函数（指数扫）；带通之后能量集中在中心附近
    check(p + "cutoff_start_hz", a.cutoffAt(-1.0), "==", 500.0);
    check(p + "cutoff_mid_err_hz", std::fabs(a.cutoffAt(4.0) - 2000.0), "<=", 1e-6);
    check(p + "cutoff_end_err_hz", std::fabs(a.cutoffAt(99.0) - 8000.0), "<=", 1e-6);
    {
        // 固定 1 kHz、Q 4：中心那个倍频程比上下各隔两个倍频程的至少高 10 dB
        Noise z;
        z.init(sr);
        z.setQ(4.0);
        z.setSweep(0.0, 0.0, 1000.0, 1000.0);
        z.setLevel(1.0f);
        const Buf y = gen(z, zero, {512});
        const int n = 65536, from = 4096;
        const double mid = bandPower(y, from, n, 707.0, 1414.0, sr);
        check(p + "bandpass_vs_2oct_below_db", 10.0 * std::log10(mid / bandPower(y, from, n, 177.0, 354.0, sr)), ">=", 10.0);
        check(p + "bandpass_vs_2oct_above_db", 10.0 * std::log10(mid / bandPower(y, from, n, 2828.0, 5657.0, sr)), ">=", 10.0);
        // 扫频：第 2 拍附近中心 1 kHz，第 6 拍附近中心 4 kHz
        const int w = 16384;
        const int at2 = static_cast<int>(2.0 / dbeat) - w / 2, at6 = static_cast<int>(6.0 / dbeat) - w / 2;
        const double lo2 = bandPower(ya, at2, w, 707.0, 1414.0, sr), hi2 = bandPower(ya, at2, w, 2828.0, 5657.0, sr);
        const double lo6 = bandPower(ya, at6, w, 707.0, 1414.0, sr), hi6 = bandPower(ya, at6, w, 2828.0, 5657.0, sr);
        check(p + "sweep_at_beat2_1k_over_4k_db", 10.0 * std::log10(lo2 / hi2), ">=", 6.0);
        check(p + "sweep_at_beat6_4k_over_1k_db", 10.0 * std::log10(hi6 / lo6), ">=", 6.0);
    }
    // 块大小无关（扫着，中间改 level）
    checkBlockIndep("noise", [&](const std::vector<int>& pattern) {
        Noise z;
        mk(z);
        z.setLevel(0.3f);
        return runBlocks(zero, pattern,
                [&](int start, int n, const float*, float* o) {
                    if (start == 50001) {
                        z.setLevel(1.0f);
                    }
                    z.process(o, n, 33.7 + start * dbeat, dbeat);
                },
                {50001});
    });
    {
        Buf o(static_cast<size_t>(total) * 2, 0.0f);
        const double ns = nsPerFrame(total, [&]() {
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                a.process(&o[static_cast<size_t>(pos) * 2], 512, pos * dbeat, dbeat);
            }
        });
        cpuInfo("Noise", ns, sr);
    }
}

// ================================================================ Flanger / Phaser（同一套检查）

template <class Fx>
void testMod(const std::string& name, double notchHz, double passHz, double boundLimit) {
    const std::string p = name + ".";
    const int sr = 44100;
    const double dbeat = 120.0 / 60.0 / sr;
    auto run = [&](Fx& f, const Buf& in, const std::vector<int>& pattern, int startFrame = 0) {
        return runBlocks(in, pattern, [&](int start, int n, const float* i, float* o) {
            f.process(i, o, n, (startFrame + start) * dbeat, dbeat);
        });
    };
    const Buf wn = noise(static_cast<int>(3.0 / dbeat), 0.25, 61u);

    // mix = 0：和输入逐位相同
    {
        Fx f;
        f.init(sr);
        f.set(2.0, 1.0f, 0.8f, 0.0f);
        check(p + "mix0_identity_diff", maxDiff(run(f, wn, {512}), wn), "==", 0.0);
    }
    // depth 0、无反馈、mix 1：静止的陷波在算出来的频率上，陷波之间不增不减
    {
        auto g = [&](double hz) {
            Fx f;
            f.init(sr);
            f.set(4.0, 0.0f, 0.0f, 1.0f);
            return sineGainDb(sr, hz, sr, sr / 2,
                    [&](const float* in, float* out, int n) { f.process(in, out, n, 0.0, dbeat); });
        };
        check(p + "static_notch_db", g(notchHz), "<=", -40.0);
        check(p + "static_pass_abs_db", std::fabs(g(passHz)), "<=", 0.1);
    }
    // LFO 的相位只看绝对拍：从中间开始，过了延迟线 / 平滑的建立时间（200 ms）之后和一路放过来一样
    {
        Fx a, b;
        a.init(sr);
        b.init(sr);
        a.set(2.0, 1.0f, 0.5f, 1.0f, 0.3);
        b.set(2.0, 1.0f, 0.5f, 1.0f, 0.3);
        const Buf full = run(a, wn, {512});
        const int m = 40001, warm = sr / 5;
        const Buf tail(wn.begin() + m * 2, wn.end());
        const Buf part = run(b, tail, {512}, m);
        const Buf fullTail(full.begin() + m * 2, full.end());
        check(p + "restore_at_beat_diff", maxDiff(part, fullTail, warm, framesOf(part)), "<=", 1e-5);
    }
    // 反馈顶到最大（给 ±2 也只到上限），mix 1：满幅白噪声 + 满幅正弦各 10 秒，有界；停了输入 1 秒后衰减干净
    for (int sign = 0; sign < 2; ++sign) {
        double peak = 0.0, tail = 0.0;
        for (int k = 0; k < 2; ++k) {
            Buf x = k == 0 ? noise(11 * sr, 1.0 / std::sqrt(3.0), 63u) : sine(11 * sr, passHz, 1.0, sr);
            std::fill(x.begin() + static_cast<long>(10 * sr) * 2, x.end(), 0.0f);
            Fx f;
            f.init(sr);
            f.set(1.0, 1.0f, sign == 0 ? 2.0f : -2.0f, 1.0f);
            const Buf y = run(f, x, {512});
            peak = std::max(peak, finiteBuf(y) ? maxAbs(y, 0, 11 * sr) : 1e9);
            tail = std::max(tail, maxAbs(y, 11 * sr - sr / 100, 11 * sr));
        }
        const std::string tag = sign == 0 ? "max_feedback_pos" : "max_feedback_neg";
        check(p + tag + "_peak", peak, "<=", boundLimit);
        check(p + tag + "_tail", tail, "<=", 1e-6);
    }
    // 不出爆音：100 Hz 正弦；mix 0 → 1、周期 / 相位 / 深度 / 反馈在块边界上跳
    {
        const int total = 4 * sr;
        const Buf sn = sine(total, 100.0, 0.5, sr);
        Fx f;
        f.init(sr);
        f.set(4.0, 1.0f, 0.5f, 0.0f);
        const int m1 = sr, m2 = 2 * sr + 77, m3 = 3 * sr;
        const Buf y = runBlocks(sn, {512},
                [&](int start, int n, const float* i, float* o) {
                    if (start == m1) {
                        f.set(4.0, 1.0f, 0.5f, 1.0f);
                    } else if (start == m2) {
                        f.set(0.5, 0.2f, -0.5f, 1.0f, 0.17);
                    } else if (start == m3) {
                        f.set(0.5, 1.0f, 0.0f, 0.0f, 0.17);
                    }
                    f.process(i, o, n, start * dbeat, dbeat);
                },
                {m1, m2, m3});
        std::printf("INFO %sinput_max_step %.5f\n", p.c_str(), maxStep(sn, 0, total));
        check(p + "click_max_step", maxStep(y, 0, total), "<=", 0.03);
    }
    checkBlockIndep(name, [&](const std::vector<int>& pattern) {
        Fx f;
        f.init(sr);
        f.set(1.5, 0.8f, 0.7f, 0.9f, 0.2);
        return run(f, wn, pattern, 12345);
    });
    {
        Fx f;
        f.init(sr);
        f.set(1.5, 0.8f, 0.7f, 0.9f, 0.2);
        Buf o(wn.size());
        const int total = framesOf(wn);
        const double ns = nsPerFrame(total, [&]() {
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                f.process(&wn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], 512, pos * dbeat, dbeat);
            }
        });
        cpuInfo(name, ns, sr);
    }
}

// ================================================================ BrakeSpin

void testBrakeSpin() {
    const std::string p = "brakespin.";
    const int sr = 44100;
    const int xf = static_cast<int>(std::lround(kXfadeMs * sr / 1000.0));
    const double hz = 441.0, amp = 0.5;
    const int total = 150000;
    const Buf sn = sine(total, hz, amp, sr);
    const int f0 = 30000, t1 = 22050; // 刹车
    const int f1 = 70000;             // 恢复
    const int f2 = 100000, t2 = 16000; // 回转
    const int f3 = 130000;            // 恢复

    // 命令按绝对帧给：落在哪一块就在那一块之前排进去，带块内偏移
    auto run = [&](const Buf& in, const std::vector<int>& pattern) {
        BrakeSpin bs;
        bs.init(sr);
        return runBlocks(in, pattern, [&](int start, int n, const float* i, float* o) {
            auto in_block = [&](int f) { return f >= start && f < start + n; };
            if (in_block(f0)) {
                bs.brake(t1, f0 - start);
            }
            if (in_block(f1)) {
                bs.resume(f1 - start);
            }
            if (in_block(f2)) {
                bs.backspin(t2, f2 - start);
            }
            if (in_block(f3)) {
                bs.resume(f3 - start);
            }
            bs.process(i, o, n);
        });
    };
    const Buf y = run(sn, {512});

    // 预期：读的位置落后 c·n² / 2T 帧，最后 20% 升余弦淡出
    auto expectDiff = [&](int from, int T, double c) {
        const int fade = std::max(xf, static_cast<int>(std::llround(kReleaseFadePart * T)));
        double d = 0.0;
        for (int n = 0; n < T; ++n) {
            const double pos = from + n - c * n * static_cast<double>(n) / (2.0 * T);
            double g = 1.0;
            if (n >= T - fade) {
                g = 0.5 + 0.5 * std::cos(kPi * (n - (T - fade) + 1) / (fade + 1.0));
            }
            const double el = g * amp * std::sin(2.0 * kPi * hz * pos / sr);
            const double er = g * amp * std::sin(2.0 * kPi * hz * pos / sr + 0.5 * kPi);
            d = std::max(d, std::fabs(y[static_cast<size_t>(from + n) * 2] - el));
            d = std::max(d, std::fabs(y[static_cast<size_t>(from + n) * 2 + 1] - er));
        }
        return d;
    };
    check(p + "idle_passthrough_diff", maxDiff(y, sn, 0, f0), "==", 0.0);
    check(p + "brake_speed_ramp_err", expectDiff(f0, t1, 1.0), "<=", 1e-3);
    check(p + "brake_then_silence_peak", maxAbs(y, f0 + t1, f1), "==", 0.0);
    check(p + "resume_passthrough_diff", maxDiff(y, sn, f1 + xf, f2), "==", 0.0);
    check(p + "backspin_reverse_ramp_err", expectDiff(f2, t2, 1.0 + kSpinMaxSpeed), "<=", 1e-3);
    check(p + "backspin_then_silence_peak", maxAbs(y, f2 + t2, f3), "==", 0.0);
    check(p + "resume2_passthrough_diff", maxDiff(y, sn, f3 + xf, total), "==", 0.0);
    // 回转真的在倒着读：中间那一点的读位置比开始时还早
    {
        const double n = 0.75 * t2;
        check(p + "backspin_reads_backwards_frames", n - (1.0 + kSpinMaxSpeed) * n * n / (2.0 * t2), "<=", 0.0);
    }
    // 不出爆音：刹车 / 恢复那一带每个采样的变化不超过正弦自己的（加淡化）；回转最快 3 倍速
    const double sineStep = maxStep(sn, 0, total);
    std::printf("INFO %sinput_max_step %.5f\n", p.c_str(), sineStep);
    check(p + "brake_click_max_step", maxStep(y, 0, f2), "<=", sineStep * 1.3);
    check(p + "backspin_click_max_step", maxStep(y, f2, total), "<=", sineStep * (kSpinMaxSpeed + 0.3));

    // 状态、上限、没放完就 resume、不在直通时的命令被忽略
    {
        BrakeSpin bs;
        bs.init(sr);
        check(p + "max_brake_seconds", static_cast<double>(bs.maxBrakeFrames()) / sr, "==", 8.0);
        check(p + "max_backspin_seconds", static_cast<double>(bs.maxSpinFrames()) / sr, "==", 2.0);
        Buf o(sn.size());
        int pos = 0;
        auto adv = [&](int n) {
            bs.process(&sn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], n);
            pos += n;
        };
        adv(4096);
        check(p + "state_live", bs.state(), "==", BrakeSpin::Live);
        bs.brake(20000);
        adv(8192);
        check(p + "state_braking", bs.state(), "==", BrakeSpin::Braking);
        bs.backspin(5000); // 正在刹：忽略
        bs.resume(100);    // 没放完就恢复：从效果交叉淡化到现场
        adv(4096);
        check(p + "state_live_after_early_resume", bs.state(), "==", BrakeSpin::Live);
        check(p + "early_resume_passthrough_diff", maxDiff(o, sn, 4096 + 8192 + 100 + xf, pos), "==", 0.0);
        check(p + "early_resume_click_max_step", maxStep(o, 0, pos), "<=", sineStep * 1.3);
        bs.backspin(1000000); // 太长：按上限 2 秒
        adv(2 * sr + 4096);
        check(p + "state_silent", bs.state(), "==", BrakeSpin::Silent);
        bs.brake(1000); // 静音着：忽略
        adv(4096);
        check(p + "ignored_when_silent_peak", maxAbs(o, pos - 4096, pos), "==", 0.0);
        check(p + "long_spin_bounded", finiteBuf(o) ? maxAbs(o, 0, pos) : 1e9, "<=", amp * 1.3);
    }
    // 块大小无关（命令带偏移）；偏移超过这一块 = 顺延，和按绝对帧给一样
    checkBlockIndep("brakespin", [&](const std::vector<int>& pattern) { return run(sn, pattern); });
    {
        const Buf wn = noise(total, 0.25, 71u);
        const Buf ref = run(wn, {512});
        BrakeSpin bs;
        bs.init(sr);
        bs.brake(t1, f0);
        bs.resume(f1);
        bs.backspin(t2, f2);
        bs.resume(f3);
        const Buf z = runBlocks(wn, {512}, [&](int, int n, const float* i, float* o) { bs.process(i, o, n); });
        check(p + "carried_offset_diff", maxDiff(z, ref), "==", 0.0);
    }
    {
        BrakeSpin bs;
        bs.init(sr);
        Buf o(sn.size());
        const double ns = nsPerFrame(total, [&]() {
            bs.resume();
            bs.brake(8 * sr, 600);
            for (int pos = 0; pos + 512 <= total; pos += 512) {
                bs.process(&sn[static_cast<size_t>(pos) * 2], &o[static_cast<size_t>(pos) * 2], 512);
            }
        });
        cpuInfo("BrakeSpin(braking)", ns, sr);
    }
}

// ================================================================ Sampler

void testSampler() {
    const std::string p = "sampler.";
    const int sr = 44100;
    const int xf = static_cast<int>(std::lround(kXfadeMs * sr / 1000.0));
    const int len = 30000;
    // 采样位 k：(200 + 50k) Hz 的正弦，从 0 相位起，结尾 5 ms 淡出；7 号位是一个冲激
    std::vector<Buf> smp(8);
    for (int k = 0; k < 7; ++k) {
        smp[static_cast<size_t>(k)] = sine(len, 200.0 + 50.0 * k, 0.3, sr);
        for (int i = 0; i < len; ++i) { // 两个声道都从 0 起（采样自己的开头不能带跳变），右声道小一点
            smp[static_cast<size_t>(k)][static_cast<size_t>(i) * 2 + 1] = 0.8f * smp[static_cast<size_t>(k)][static_cast<size_t>(i) * 2];
        }
        for (int i = 0; i < 220; ++i) {
            smp[static_cast<size_t>(k)][static_cast<size_t>(len - 1 - i) * 2] *= i / 220.0f;
            smp[static_cast<size_t>(k)][static_cast<size_t>(len - 1 - i) * 2 + 1] *= i / 220.0f;
        }
    }
    smp[7] = Buf(20, 0.0f);
    smp[7][0] = 1.0f;
    smp[7][1] = 0.5f;
    auto mk = [&](Sampler& s) {
        s.init(sr);
        for (int k = 0; k < 8; ++k) {
            s.load(k, smp[static_cast<size_t>(k)].data(), framesOf(smp[static_cast<size_t>(k)]));
        }
    };
    struct Ev {
        int frame, slot; // slot -1 = stopAll
        float gain;
    };
    // 事件按绝对帧给：落在哪一块就在那一块之前排进去，带块内偏移
    auto run = [&](const std::vector<Ev>& evs, int total, const std::vector<int>& pattern, float base = 0.0f) {
        Sampler s;
        mk(s);
        const Buf in(static_cast<size_t>(total) * 2, base);
        return runBlocks(in, pattern, [&](int start, int n, const float* i, float* o) {
            std::memcpy(o, i, static_cast<size_t>(n) * 2 * sizeof(float));
            for (const Ev& e : evs) {
                if (e.frame >= start && e.frame < start + n) {
                    if (e.slot < 0) {
                        s.stopAll(e.frame - start);
                    } else {
                        s.trigger(e.slot, e.frame - start, e.gain);
                    }
                }
            }
            s.process(o, n);
        });
    };
    // 采样位 slot 从第 at 帧起播、音量 g 时，第 j 帧的值
    auto val = [&](int slot, int at, float g, int j, int c) -> double {
        const Buf& d = smp[static_cast<size_t>(slot)];
        const int k = j - at;
        return (k >= 0 && k < framesOf(d)) ? static_cast<double>(d[static_cast<size_t>(k) * 2 + c]) * g : 0.0;
    };
    auto fade = [&](int age) { return 0.5 + 0.5 * std::cos(kPi * (age + 1) / (xf + 1.0)); };

    // 没触发的时候什么都不加
    {
        Sampler s;
        mk(s);
        Buf o(2048, 0.25f);
        s.process(o.data(), 1024);
        check(p + "idle_adds_nothing_diff", maxDiff(o, Buf(2048, 0.25f)), "==", 0.0);
    }
    // 落在采样上：冲激在偏移 37 起播；加进 out（原来的 0.1 还在）
    {
        const Buf y = run({{37, 7, 0.5f}}, 1024, {512}, 0.1f);
        check(p + "trigger_sample_accurate_L", y[37 * 2], "==", static_cast<double>(0.1f + 0.5f));
        check(p + "trigger_sample_accurate_R", y[37 * 2 + 1], "==", static_cast<double>(0.1f + 0.25f));
        check(p + "before_trigger_untouched", std::fabs(y[36 * 2] - 0.1f), "==", 0.0);
        check(p + "adds_into_out", std::fabs(y[60 * 2] - 0.1f), "==", 0.0);
        // 偏移超过这一块：顺延到后面的块
        Sampler s;
        mk(s);
        s.trigger(7, 1000, 1.0f);
        Buf o(static_cast<size_t>(2048) * 2, 0.0f);
        for (int pos = 0; pos < 2048; pos += 512) {
            s.process(&o[static_cast<size_t>(pos) * 2], 512);
        }
        check(p + "carried_offset_position", o[1000 * 2], "==", 1.0);
        check(p + "carried_offset_only_there", maxAbs(o, 0, 1000) + maxAbs(o, 1001, 2048), "==", 0.0);
    }
    // 4 个声音 + 第 5 个抢掉最早的（0 号位，2 ms 淡出）+ 同一个位重触发（2 号位）+ 全停
    const std::vector<Ev> evs = {{0, 0, 1.0f}, {1000, 1, 0.9f}, {2000, 2, 0.8f}, {3000, 3, 0.7f}, {5000, 4, 1.0f},
            {8000, 2, 0.6f}, {12000, -1, 0.0f}};
    const int total = 16000;
    const Buf y = run(evs, total, {512});
    {
        double d = 0.0;
        for (int j = 0; j < total; ++j) {
            for (int c = 0; c < 2; ++c) {
                double e = 0.0;
                // 0 号位：5000 被抢，淡出
                e += j < 5000 ? val(0, 0, 1.0f, j, c) : (j < 5000 + xf ? val(0, 0, 1.0f, j, c) * fade(j - 5000) : 0.0);
                // 1、3、4 号位：放到 12000 全停
                const int starts[3] = {1000, 3000, 5000};
                const int slots[3] = {1, 3, 4};
                const float gains[3] = {0.9f, 0.7f, 1.0f};
                for (int k = 0; k < 3; ++k) {
                    const double v = val(slots[k], starts[k], gains[k], j, c);
                    e += j < 12000 ? v : (j < 12000 + xf ? v * fade(j - 12000) : 0.0);
                }
                // 2 号位：8000 重触发（旧的淡出，新的从头满音量），12000 全停
                e += j < 8000 ? val(2, 2000, 0.8f, j, c) : (j < 8000 + xf ? val(2, 2000, 0.8f, j, c) * fade(j - 8000) : 0.0);
                const double v2 = val(2, 8000, 0.6f, j, c);
                e += j < 12000 ? v2 : (j < 12000 + xf ? v2 * fade(j - 12000) : 0.0);
                d = std::max(d, std::fabs(y[static_cast<size_t>(j) * 2 + c] - e));
            }
        }
        check(p + "polyphony_steal_retrigger_stop_err", d, "<=", 1e-6);
        check(p + "stopall_silence_peak", maxAbs(y, 12000 + xf, total), "==", 0.0);
        check(p + "deterministic_diff", maxDiff(y, run(evs, total, {512})), "==", 0.0);
    }
    // 不出爆音（单个声音看得最清楚）：200 Hz、幅度 0.3 的正弦，在波峰上重触发 / 全停；硬切会跳 0.3
    {
        const int peakAt = 55 + 10 * 2205 / 10; // 200 Hz：周期 220.5 帧，第 55 帧附近是波峰
        const Buf a = run({{0, 0, 1.0f}, {peakAt, 0, 1.0f}}, 8000, {512});
        const Buf b = run({{0, 0, 1.0f}, {peakAt, -1, 0.0f}}, 8000, {512});
        const double sineStep = maxStep(smp[0], 0, 2000);
        std::printf("INFO %ssample_max_step %.5f value_at_cut %.3f\n", p.c_str(), sineStep,
                smp[0][static_cast<size_t>(peakAt) * 2]);
        check(p + "retrigger_click_max_step", maxStep(a, 0, 8000), "<=", 2.0 * sineStep + 0.3 * 0.5 * kPi / xf * 1.05);
        check(p + "stopall_click_max_step", maxStep(b, 0, 8000), "<=", sineStep + 0.3 * 0.5 * kPi / xf * 1.05);
    }
    // 声音数：最多 4 个
    {
        Sampler s;
        mk(s);
        for (int k = 0; k < 6; ++k) {
            s.trigger(k, k * 10, 1.0f);
        }
        Buf o(2048, 0.0f);
        s.process(o.data(), 1024);
        check(p + "max_voices", s.activeVoices(), "==", 4.0);
    }
    checkBlockIndep("sampler", [&](const std::vector<int>& pattern) { return run(evs, total, pattern, 0.05f); });
    {
        Sampler s;
        mk(s);
        Buf o(static_cast<size_t>(len) * 2, 0.0f);
        const double ns = nsPerFrame(len - 512, [&]() {
            s.reset();
            for (int k = 0; k < 4; ++k) {
                s.trigger(k, 0, 1.0f);
            }
            for (int pos = 0; pos + 512 <= len - 512; pos += 512) {
                s.process(&o[static_cast<size_t>(pos) * 2], 512);
            }
        });
        cpuInfo("Sampler(4 voices)", ns, sr);
    }
}

} // namespace

int main() {
    std::printf("INFO dsp version %d\n", kDspVersion);
    testBandSplit(44100);
    testBandSplit(48000);
    testAlignDelay();
    testTransGate();
    testRoll();
    testSvf();
    testSpiral();
    testNoise();
    // Flanger：depth 0 时延迟 0.25 ms → 第一个陷波在 2 kHz，4 kHz 是峰。上限：干 0.5 + 延迟 0.5 × kFbClamp × 插值过冲 1.25
    testMod<Flanger>("flanger", 2000.0, 4000.0, 0.5 + 0.5 * 1.25 * kFbClamp);
    // Phaser：depth 0 时转折频率 = √(200 × 4000) = 894.43 Hz，那里正好是陷波；20 Hz 几乎不转相位
    // 上限：满幅正弦正好打在共振峰上的稳态值 = 干 0.5 + 全通链 0.5 / (1 - 反馈)
    testMod<Phaser>("phaser", std::sqrt(kPhaserMinHz * kPhaserMaxHz), 20.0, 0.5 + 0.5 / (1.0 - kPhaserMaxFeedback));
    testBrakeSpin();
    testSampler();
    std::printf("SUMMARY checks=%d failed=%d\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
