// v5 现场效果（插入效果）用的 DSP 积木：三段分频、Trans / Gate、Roll、滤波、Spiral、Noise、Flanger、Phaser、
// 刹车 / 回转、采样器。这里只有信号处理：哪一拍开哪个效果由调度器（engine5.hpp）按执行计划定，这里不做任何决策。
//
// 约定（每一块都一样）：
// - 缓冲 = 交错立体声 float。init() 里把内存分配完；process() 不分配、不加锁、不做 I/O；reset() 清状态（参数留着）。
//   有 in / out 两个缓冲的，in 和 out 可以是同一个（原地处理）。
// - 结果和块大小无关：同样的输入分成 64 / 512 / 不规则的块，和一次处理完逐个采样相同（test_dsp.cpp 里逐块核对）。
// - 跟拍的块收 beat0（这一块第一个采样的全场拍）和 dbeat（每个采样走多少拍，一块里不变）：相位一律从绝对拍算，
//   不靠内部自己数的计数器，所以定位到任意一拍重新开始，结果和一路放过来一样。
//   「到没到某一拍」统一留 kBeatEps 帧的余量：拍是浮点数，同一个采样从不同的块起点算过来会差 1e-9 量级，
//   不留余量的话恰好落在采样上的事件会时早时晚一个采样。
// - 引擎用 -ffast-math 编：不用 NaN / inf 当标记，不用 std::isnan / isfinite；只用标准库。
// - 开 / 关 / 回绕处都有短的平滑或交叉淡化，不出爆音。下面这些毫秒数是「版本化的行为」：
//   改了算法或这些数就把 kDspVersion 加一（谱保存时记下版本，升级后同一份谱不会悄悄变声）。
// - TransGate 的沿、Roll 的交叉淡化都在音乐上的那一点（周期起点 / 关的那一点 / 每一遍的格点 / 松开点）之前走完，
//   不是从那一点才开始（和 engine5 的门同一个习惯）：正好落在那一点上的鼓，要么完整出来，要么完全没有。
// - 只有 BandSplit3 有延迟（latency()）；别的块都是零延迟。分频之后再跟拍处理的那几路，
//   传进来的 beat0 要是「缓冲里这段声音」的拍（= 现在的拍减去分频器的延迟）。
// 验收：test_dsp.cpp（独立程序，逐项打 PASS / FAIL；g++ / g++ -ffast-math / clang++ 三种编法都要全过）。
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

namespace djs5 {

constexpr int kDspVersion = 1;

// ---- 版本化的固定参数 ----
constexpr double kXfadeMs = 2.0;       // 交叉淡化：Roll 的回绕 / 改长度 / 松开，BrakeSpin 的恢复，Sampler 的重触发 / 抢占 / 全停
constexpr double kGateEdgeMs = 1.5;    // TransGate 开 / 关沿的长度（升余弦）
constexpr double kParamSmoothMs = 2.0; // mix / feedback / level / send 一下跳变时的平滑：0 → 1 走这么久（线性；同 engine5 的推子）
constexpr double kSvfSmoothMs = 10.0;  // SvfFilter 系数（截止 / 共振 / 模式）的一阶平滑时间常数
constexpr double kModSmoothMs = 5.0;   // Flanger 的延迟、Phaser 的扫频位置的一阶平滑（周期 / 深度一下改了也不跳）
constexpr double kSpiralSmoothMs = 30.0; // Spiral 的延迟长度（帧）的一阶平滑（全场变速时延迟一块一跳，要抹平）
constexpr double kBeatEps = 1e-4;      // 「到没到这一拍」的余量（帧）
constexpr float kFbClamp = 2.0f;       // 反馈回路里写进延迟线的限幅（+6 dBFS：正常电平碰不到，只挡失控）

// BandSplit3：两个线性相位低通的半长（= 群延迟）和 Kaiser 窗的 β（β = 5.653 ↔ 阻带 60 dB）
constexpr double kSplitLowDelayMs = 6.0;  // LOW / MID 分频的低通：半长 6 ms（= 整个分频器的延迟）
constexpr double kSplitHighDelayMs = 1.0; // MID / HIGH 分频的低通：半长 1 ms
constexpr double kSplitKaiserBeta = 5.653;

constexpr double kFlangerMinMs = 0.25; // Flanger 的延迟在这两个数之间扫
constexpr double kFlangerMaxMs = 8.0;
constexpr float kFlangerMaxFeedback = 0.95f;
constexpr int kPhaserStages = 6; // 6 级一阶全通 = 3 个陷波
constexpr double kPhaserMinHz = 200.0;
constexpr double kPhaserMaxHz = 4000.0;
constexpr float kPhaserMaxFeedback = 0.9f;
constexpr float kSpiralMaxFeedback = 0.98f;
constexpr double kSvfMinHz = 10.0;
constexpr double kSvfMaxRatio = 0.45; // 截止最高到 0.45 × 采样率
constexpr double kSvfMinQ = 0.3;
constexpr double kSvfMaxQ = 40.0;
constexpr double kSpinMaxSpeed = 3.0;    // 回转：倒放最快到 3 倍速
constexpr double kReleaseFadePart = 0.2; // 刹车 / 回转：最后这一段（占时长的比例，最少 kXfadeMs）淡出

namespace dspd { // 内部小工具

constexpr double kPi = 3.14159265358979323846;
constexpr double kHuge = 1e300; // 「没有 / 无限远」（不用 inf）

inline int msToFrames(double ms, int sampleRate) {
    return std::max(1, static_cast<int>(std::lround(ms * sampleRate / 1000.0)));
}
// 一阶平滑每个采样走的比例（时间常数 ms）
inline double onePole(double ms, int sampleRate) {
    return 1.0 - std::exp(-1000.0 / (ms * sampleRate));
}
// 升余弦：x 从 0 到 1，值从 0 到 1，两头斜率为 0
inline double raisedCos(double x) {
    return x <= 0.0 ? 0.0 : (x >= 1.0 ? 1.0 : 0.5 - 0.5 * std::cos(kPi * x));
}
inline float clampf(float v, float lim) {
    return v > lim ? lim : (v < -lim ? -lim : v);
}
// 很小的数直接归零：反馈回路没输入时会一路衰减到非规格化数，那种数在 x86 上算得特别慢
inline float flushTiny(float v) {
    return (v < 1e-20f && v > -1e-20f) ? 0.0f : v;
}

// 4 点 3 阶 Lagrange 插值的权重：取 x[-1], x[0], x[1], x[2]，读 x[t]（0 ≤ t ≤ 1）。t = 0 时正好是 x[0]。
// 选 Lagrange 而不是 Hermite 之类：它对任何小数延迟的幅频都 ≤ 1（核对过：最大 1.0000），
// 放在反馈回路里（Spiral / Flanger）不会让某个频率的环路增益超过反馈量。
inline void lagrange4(double t, float* w) {
    const double a = t + 1.0, c = t - 1.0, d = t - 2.0;
    w[0] = static_cast<float>(-t * c * d / 6.0);
    w[1] = static_cast<float>(a * c * d / 2.0);
    w[2] = static_cast<float>(-a * t * d / 2.0);
    w[3] = static_cast<float>(a * t * c / 6.0);
}

// 参数的线性平滑：每个采样最多走 step。reset 之后的第一块直接跳到目标（没有「从默认值滑过来」的那一段）。
struct Slew {
    float cur = 0.0f, tgt = 0.0f, step = 1.0f;

    void init(int sampleRate, double ms) { step = static_cast<float>(1000.0 / (ms * sampleRate)); }
    void set(float v) { tgt = v; }
    void snap() { cur = tgt; }
    float next() {
        if (cur < tgt) {
            cur += step;
            if (cur > tgt) {
                cur = tgt;
            }
        } else if (cur > tgt) {
            cur -= step;
            if (cur < tgt) {
                cur = tgt;
            }
        }
        return cur;
    }
};

// 立体声环形缓冲（长度取 2 的幂）。位置用「一共写了多少帧」的绝对帧号，读的时候可以带小数。
class Ring {
  public:
    void init(int minFrames) {
        int n = 16;
        while (n < minFrames) {
            n <<= 1;
        }
        m_buf.assign(static_cast<size_t>(n) * 2, 0.0f);
        m_mask = n - 1;
        m_count = 0;
    }
    void clear() {
        std::fill(m_buf.begin(), m_buf.end(), 0.0f);
        m_count = 0;
    }
    long long count() const { return m_count; } // 下一帧要写的位置；最新的一帧 = count() - 1
    void write(float l, float r) {
        float* p = at(m_count);
        p[0] = l;
        p[1] = r;
        ++m_count;
    }
    const float* at(long long i) const { return &m_buf[static_cast<size_t>(i & m_mask) * 2]; }
    float* at(long long i) { return &m_buf[static_cast<size_t>(i & m_mask) * 2]; }
    // 读绝对位置 pos（帧）：用到 floor(pos) - 1 .. floor(pos) + 2 这 4 帧
    void read(double pos, float& l, float& r) const {
        const double fl = std::floor(pos);
        const long long i = static_cast<long long>(fl);
        float w[4];
        lagrange4(pos - fl, w);
        const float* a = at(i - 1);
        const float* b = at(i);
        const float* c = at(i + 1);
        const float* d = at(i + 2);
        l = w[0] * a[0] + w[1] * b[0] + w[2] * c[0] + w[3] * d[0];
        r = w[0] * a[1] + w[1] * b[1] + w[2] * c[1] + w[3] * d[1];
    }

  private:
    std::vector<float> m_buf;
    long long m_mask = 0;
    long long m_count = 0;
};

// 第一类零阶修正 Bessel 函数（Kaiser 窗用；libc++ 没有 std::cyl_bessel_i，自己按级数算）
inline double besselI0(double x) {
    const double q = x * x / 4.0;
    double sum = 1.0, term = 1.0;
    for (int k = 1; k < 200; ++k) {
        term *= q / (static_cast<double>(k) * k);
        sum += term;
        if (term < sum * 1e-17) {
            break;
        }
    }
    return sum;
}

// Kaiser 窗 sinc 低通，线性相位，长 2·half + 1；只存一半：h[0] = 中心，h[k] = 离中心 k 帧的系数。直流增益归一。
inline void designLowpass(int half, double cutoffHz, int sampleRate, double beta, std::vector<float>& h) {
    std::vector<double> d(static_cast<size_t>(half) + 1);
    const double f = cutoffHz / sampleRate;
    const double i0b = besselI0(beta);
    double sum = 0.0;
    for (int k = 0; k <= half; ++k) {
        const double s = k == 0 ? 2.0 * f : std::sin(2.0 * kPi * f * k) / (kPi * k);
        const double r = static_cast<double>(k) / half;
        const double w = besselI0(beta * std::sqrt(std::max(0.0, 1.0 - r * r))) / i0b;
        d[static_cast<size_t>(k)] = s * w;
        sum += (k == 0 ? 1.0 : 2.0) * s * w;
    }
    h.resize(d.size());
    for (size_t k = 0; k < d.size(); ++k) {
        h[k] = static_cast<float>(d[k] / sum);
    }
}

// 对称 FIR 的一帧：y = h[0]·x[c] + Σ h[k]·(x[c-k] + x[c+k])，c 指向中心那一帧（交错立体声）。
// 利用对称把两头先加起来，每个系数只乘一次；左右各 4 个独立的累加器，不开 -ffast-math 时 CPU 也能并行着加
// （只有一个累加器的话，每一项都得等上一项加完）。加的顺序固定，所以结果和块怎么分无关。
inline void symFir(const float* c, const float* h, int half, float& outL, float& outR) {
    float l0 = h[0] * c[0], r0 = h[0] * c[1];
    float l1 = 0.0f, r1 = 0.0f, l2 = 0.0f, r2 = 0.0f, l3 = 0.0f, r3 = 0.0f;
    const float* a = c - 2;
    const float* b = c + 2;
    int k = 1;
    for (; k + 3 <= half; k += 4) {
        l0 += h[k] * (a[0] + b[0]);
        r0 += h[k] * (a[1] + b[1]);
        l1 += h[k + 1] * (a[-2] + b[2]);
        r1 += h[k + 1] * (a[-1] + b[3]);
        l2 += h[k + 2] * (a[-4] + b[4]);
        r2 += h[k + 2] * (a[-3] + b[5]);
        l3 += h[k + 3] * (a[-6] + b[6]);
        r3 += h[k + 3] * (a[-5] + b[7]);
        a -= 8;
        b += 8;
    }
    for (; k <= half; ++k) {
        l0 += h[k] * (a[0] + b[0]);
        r0 += h[k] * (a[1] + b[1]);
        a -= 2;
        b += 2;
    }
    outL = (l0 + l1) + (l2 + l3);
    outR = (r0 + r1) + (r2 + r3);
}

} // namespace dspd

// 纯延迟 N 帧（立体声）：不过分频器的那一路用它对齐（延迟 = BandSplit3::latency()）。
class AlignDelay {
  public:
    void init(int delayFrames) {
        m_delay = std::max(0, delayFrames);
        m_buf.assign(static_cast<size_t>(m_delay) * 2, 0.0f);
        reset();
    }
    void reset() {
        std::fill(m_buf.begin(), m_buf.end(), 0.0f);
        m_pos = 0;
    }
    int delay() const { return m_delay; }
    void process(const float* in, float* out, int frames) {
        if (m_delay == 0) {
            if (in != out) {
                std::memcpy(out, in, static_cast<size_t>(frames) * 2 * sizeof(float));
            }
            return;
        }
        const size_t size = m_buf.size();
        for (int i = 0; i < frames * 2; i += 2) {
            const float l = in[i], r = in[i + 1];
            out[i] = m_buf[m_pos];
            out[i + 1] = m_buf[m_pos + 1];
            m_buf[m_pos] = l;
            m_buf[m_pos + 1] = r;
            m_pos += 2;
            if (m_pos >= size) {
                m_pos = 0;
            }
        }
    }

  private:
    std::vector<float> m_buf;
    size_t m_pos = 0;
    int m_delay = 0;
};

// 三段分频（LOW / MID / HIGH），线性相位、互补：low + mid + high = 延迟 latency() 帧的输入（只差 float 的舍入）。
//
// 做法（减法结构）：两个 Kaiser 窗 sinc 低通 LP1（截止 = 低分频点）、LP2（截止 = 高分频点），中心对齐在同一帧：
//   low = LP1(x)        mid = LP2(x) - LP1(x)        high = x(延迟) - LP2(x)
// 三段是从同一个延迟后的输入里一层层减出来的，所以不管滤波器长什么样，加回去一定是原样；滤波器只决定隔离度。
// 线性相位 = 三段之间没有相位差，分开处理（只 gate 高频、只 roll 低频）再加回去不会在分频点上梳状抵消。
//
// 设计数字（44.1 kHz；48 kHz 时半长按毫秒换算，频响按 Hz 不变）：
//   LP1：半长 264 帧（6.0 ms）→ 529 个系数；过渡带约 95–405 Hz（两头 -60 dB），250 Hz 处 -6 dB
//   LP2：半长 44 帧（1.0 ms）→ 89 个系数；过渡带约 1.57–3.41 kHz，2.5 kHz 处 -6 dB
//   隔离（理论值）：60 Hz 漏进 MID -57 dB / HIGH -89 dB；1 kHz 漏进 LOW -85 / HIGH -67；8 kHz 漏进 LOW -118 / MID -83
//   延迟 = LP1 的半长 = 264 帧（5.99 ms）；48 kHz 时 288 帧（6.00 ms）
// 取舍：低分频点才 250 Hz，过渡带的宽度（Hz）≈ 3.6 × 采样率 / 系数个数，想更陡只能更长 = 延迟更大、CPU 更多。
//   6 ms 时 100 Hz 以下（底鼓、sub bass）完整留在 LOW，400 Hz 以上从 LOW 里压到 -60 dB，够把底鼓和中频分干净；
//   再长一倍（上限 12 ms）过渡带只窄 150 Hz，延迟和 CPU 都翻倍，不值。预振铃 = 分频点附近、提前最多 6 ms、
//   只在两段处理得很不一样时才露出来（-60 dB 量级）。
// CPU：每帧每声道 264 + 44 = 308 次乘加（靠对称折了一半），实测见 test_dsp.cpp 打出来的 ns/帧（约占一个核的 1%）。
class BandSplit3 {
  public:
    static constexpr int kHistFrames = 2048; // 历史缓冲多留这么多帧：攒满了才往前搬一次

    void init(int sampleRate, double lowHz = 250.0, double highHz = 2500.0) {
        // 半长取 4 的倍数：symFir 的主循环一次 4 个系数，正好没有零头
        m_d1 = 4 * std::max(1, static_cast<int>(std::lround(kSplitLowDelayMs * sampleRate / 4000.0)));
        m_d2 = 4 * std::max(1, static_cast<int>(std::lround(kSplitHighDelayMs * sampleRate / 4000.0)));
        m_d2 = std::min(m_d2, m_d1);
        const double nyq = 0.5 * sampleRate;
        const double hi = std::min(std::max(highHz, 40.0), 0.9 * nyq);
        const double lo = std::min(std::max(lowHz, 20.0), hi);
        dspd::designLowpass(m_d1, lo, sampleRate, kSplitKaiserBeta, m_h1);
        dspd::designLowpass(m_d2, hi, sampleRate, kSplitKaiserBeta, m_h2);
        m_hist.assign(static_cast<size_t>(2 * m_d1 + kHistFrames) * 2, 0.0f);
        reset();
    }
    void reset() {
        std::fill(m_hist.begin(), m_hist.end(), 0.0f);
        m_fill = 2 * m_d1;
    }
    int latency() const { return m_d1; } // 帧
    int lowTaps() const { return 2 * m_d1 + 1; }
    int highTaps() const { return 2 * m_d2 + 1; }

    // in 可以和 low / mid / high 里的任意一个是同一个缓冲；low / mid / high 互相不能重叠
    void process(const float* in, float* low, float* mid, float* high, int frames) {
        const int cap = 2 * m_d1 + kHistFrames;
        while (frames > 0) {
            const int n = std::min(frames, cap - m_fill);
            std::memcpy(&m_hist[static_cast<size_t>(m_fill) * 2], in, static_cast<size_t>(n) * 2 * sizeof(float));
            const float* c = &m_hist[static_cast<size_t>(m_fill - m_d1) * 2]; // 窗的中心 = 延迟了 m_d1 帧的输入
            for (int i = 0; i < n; ++i, c += 2) {
                float ll, lr, ml, mr;
                dspd::symFir(c, m_h1.data(), m_d1, ll, lr);
                dspd::symFir(c, m_h2.data(), m_d2, ml, mr);
                low[2 * i] = ll;
                low[2 * i + 1] = lr;
                mid[2 * i] = ml - ll;
                mid[2 * i + 1] = mr - lr;
                high[2 * i] = c[0] - ml;
                high[2 * i + 1] = c[1] - mr;
            }
            m_fill += n;
            if (m_fill == cap) {
                std::memmove(m_hist.data(), &m_hist[static_cast<size_t>(cap - 2 * m_d1) * 2],
                        static_cast<size_t>(2 * m_d1) * 2 * sizeof(float));
                m_fill = 2 * m_d1;
            }
            in += 2 * n;
            low += 2 * n;
            mid += 2 * n;
            high += 2 * n;
            frames -= n;
        }
    }

  private:
    std::vector<float> m_h1, m_h2; // 半边系数（中心在 [0]）
    std::vector<float> m_hist;     // 最近 2·m_d1 帧输入 + 这一轮新进来的
    int m_d1 = 0, m_d2 = 0;
    int m_fill = 0; // m_hist 里已有多少帧
};

// Trans / Gate：跟拍的门，乘在某一段（或整路）上。
// 一个周期（period 拍）里前 duty 这一段开着（增益 1），其余关着（增益 1 - depth）。沿是 kGateEdgeMs 的升余弦，
// 和 engine5 的门同一个习惯：沿在那一点之前走完，不是从那一点才开始走——正好落在那一点上的鼓，要么完整出来，要么完全没有。
// 记 u = 在这个周期里走了多少拍，on = duty × period（关的那一点），沿 = kGateEdgeMs 换成拍：
//   [0, on - 沿)            全开，增益 1（u = 0 即周期起点 = 拍点，已经全开，不削鼓头）
//   [on - 沿, on)           关门的沿：1 → 1 - depth；on - 沿/2 处正好一半
//   [on, period - 沿)       关到底，增益 1 - depth（u = on 那个采样已经完全关上，不漏鼓头）
//   [period - 沿, period)   开门的沿：1 - depth → 1，到下一个周期起点走完
// 开着的 / 关着的那一段比一个沿还短时两条沿会叠在一起，增益仍然连续、不超过 1：
//   - on < 沿：开门的沿没走完，关门的沿就开始了 → 取两条沿里小的那个：一个开不到 1 的尖（尖顶在周期起点之前
//     (沿 - on) / 2 处），u = on 时照样关到底。
//   - period - on < 沿：关门的沿没走完，下一次开门的沿就开始了 → 两条沿相加（各自「开了多少」加起来，恒 ≤ 1）：
//     一个关不到底的凹口，周期起点照样全开；关着的那一段越短凹得越浅，duty → 1 时平滑地变成不关（接得上 duty ≥ 1）。
//     这种情况下 u = on 处不是 1 - depth（来不及关死再打开）。这里不取小的那个：取小的话开门的沿一开始就从
//     「还开着」跳到 0，不连续。
// duty ≥ 1 或 depth ≤ 0：增益恒为 1；duty ≤ 0：一直关着（恒为 1 - depth）。全场没在走（沿 = 0 拍）：方的，[0, on) 开。
// gain() 是绝对拍的纯函数。process() 另外给增益加了一道斜率限制（每个采样最多变 2 / 沿的帧数；正常的沿最陡
// 才 π/2 / 沿的帧数，不受影响）：周期 / 占空 / 深度 / 相位在块边界上一下改了，门的状态可能当场翻转，这样也不出爆音。
class TransGate {
  public:
    void init(int sampleRate) {
        m_edgeFrames = kGateEdgeMs * sampleRate / 1000.0;
        m_maxStep = 2.0 / m_edgeFrames;
        reset();
    }
    void reset() {
        m_g = 1.0;
        m_fresh = true;
    }
    // period：周期（拍，> 0）；duty：开着的比例 0..1；depth：关的时候压多深 0..1（0 = 不起作用）；phase：周期起点的偏移（拍）
    void set(double periodBeats, double duty, double depth, double phaseBeats = 0.0) {
        m_period = periodBeats > 1e-6 ? periodBeats : 1e-6;
        m_duty = std::min(std::max(duty, 0.0), 1.0);
        m_depth = std::min(std::max(depth, 0.0), 1.0);
        m_phase = phaseBeats;
    }
    // 沿的长度（拍）= kGateEdgeMs 换成帧 × dbeat
    static double edgeBeats(double dbeat, int sampleRate) {
        return kGateEdgeMs * sampleRate / 1000.0 * std::max(dbeat, 0.0);
    }
    // 纯函数：全场第 beat 拍上的增益（edgeBeats 见上）
    static double gainAt(double beat, double edgeBeats, double period, double duty, double depth, double phase) {
        if (depth <= 0.0 || duty >= 1.0) {
            return 1.0;
        }
        double open = 0.0; // 开了多少：0 = 关死，1 = 全开
        if (duty > 0.0) {
            const double x = beat - phase;
            const double on = duty * period;
            if (edgeBeats > 0.0) {
                // 一个周期的「窗」= 开门的沿 + 开着的那一段：从周期起点前一个沿升起，[0, on - 沿] 平着，到 on 降完；
                // 窗的正中在 (on - 沿) / 2。取正中不晚于这一拍的最近一个窗，v = 这一拍离它的周期起点多远
                // （(on - 沿) / 2 ≤ v < (on - 沿) / 2 + period）：这一拍在这个窗的后半边（看它关门的沿），
                // 同时在下一个窗（起点在 v = period）的前半边（看它开门的沿）。两项相加：关着的那一段不比沿短时
                // 两项不会同时非零（= 上面那张表）；和 ≤ 1（两个升余弦的自变量加起来 = 1 - 关着的长度 / 沿 ≤ 1）。
                // 换窗的地方（v 从正中 + period 跳回正中）两边的值相同，所以对任何参数都连续。
                const double v = x - std::floor((x + 0.5 * (edgeBeats - on)) / period) * period;
                const double inv = 1.0 / edgeBeats;
                open = std::min(1.0, dspd::raisedCos((on - v) * inv) + dspd::raisedCos((v - period) * inv + 1.0));
            } else {
                open = x - std::floor(x / period) * period < on ? 1.0 : 0.0;
            }
        }
        return 1.0 - depth * (1.0 - open);
    }
    double gain(double beat, double dbeat) const {
        return gainAt(beat, m_edgeFrames * std::max(dbeat, 0.0), m_period, m_duty, m_depth, m_phase);
    }
    void process(const float* in, float* out, int frames, double beat0, double dbeat) {
        const double edge = m_edgeFrames * std::max(dbeat, 0.0);
        for (int i = 0; i < frames; ++i) {
            const double target = gainAt(beat0 + i * dbeat, edge, m_period, m_duty, m_depth, m_phase);
            if (m_fresh) {
                m_g = target;
                m_fresh = false;
            } else if (target > m_g) {
                m_g = std::min(target, m_g + m_maxStep);
            } else {
                m_g = std::max(target, m_g - m_maxStep);
            }
            const float g = static_cast<float>(m_g);
            out[2 * i] = in[2 * i] * g;
            out[2 * i + 1] = in[2 * i + 1] * g;
        }
    }

  private:
    double m_edgeFrames = 66.0, m_maxStep = 0.03;
    double m_period = 1.0, m_duty = 0.5, m_depth = 0.0, m_phase = 0.0;
    double m_g = 1.0;
    bool m_fresh = true;
};

// Roll：抓一小段，原地反复放。
// arm(captureBeat, lengthBeats)：从 captureBeat 起录 lengthBeats 拍，录完就一遍一遍重放这一段，每一遍的起点
// （下面叫「格点」）都对在 captureBeat + n × 长度 上。setLength(atBeat, 新长度)：从 atBeat 起改放这一段的前「新长度」拍
// （1 → 1/2 → 1/4 拍那种越切越碎），格点仍然对在 captureBeat + n × 新长度 上。release(atBeat)：回现场。
// mix：0 = 只有现场（和输入逐位相同），1 = 只有 roll。
// 用法：reset(); setMix(); arm(); setLength()…; release(); 在抓取点之前一次排好，之后每一块调 process()
// （beat0 = 这一块第一个采样的全场拍）。
//
// 每一遍从格点那个采样起就是满音量；交叉淡化在那一点之前走完（和门同一个习惯：抓在拍点上的底鼓，每一遍的鼓头都完整）：
// - 速度不变、mix = 1 时，每一遍的第一个采样和录下来的第一个采样逐位相同，往后一直到下一个交叉淡化区之前都是原样拷贝。
// - 不连续点 = 格点、改长度生效的那一点、松开点。交叉淡化在每个不连续点之前的 kXfadeMs 里：
//       输出 = 原来在放的 × (1 - t) + 引入段 × t        t = 升余弦，0 → 1，到那一点正好走完
//   「原来在放的」= 上一段接着往下放（上一遍的尾巴）；第一遍开始之前 = 现场，所以录制的最后 kXfadeMs 里
//   输出已经不是纯直通，而是现场淡到引入段。
//   「引入段」= 新的一段往回接出去的那一截：新的一段在那一点上读录音的第 p 帧，引入段就是第 p - kXfadeMs .. p 帧。
//   格点上 p = 0，引入段 = 第 -kXfadeMs .. -1 帧 = 抓取点之前的那 kXfadeMs（「预卷」）：淡进来的是这段录音本来的前文，
//   接到第 0 帧是天然连续的。改长度的那一点不在格点上时 p > 0，引入段是录音里紧挨着的前一截（不够的部分接预卷）。
//   松开点的引入段 = 现场：atBeat 那个采样起输出和输入逐位相同，从 roll 回现场的淡化在它之前的 kXfadeMs 里。
// - 预卷：arm 之后等抓取点的时候一直记着最近 kXfadeMs 的输入（全场没在走时也记），开始录的那一刻它就是预卷。
//   抓取点之前没调过 process()（没有历史）= 预卷是静音；调过但不够 kXfadeMs = 缺的那一截是静音。
// - 两个不连续点挨得比 kXfadeMs 还近（改长度的那一点不在格点上又紧挨着格点、刚开始放就松开）：一层套一层地淡——
//   先淡到前一个点的引入段，再把这个结果淡到后一个点的引入段，仍然连续。
// 淡化长度按帧格上的帧算，变速时略有伸缩。一遍不会短于 2 × kXfadeMs（太短的长度按这个算），所以从一个格点到下一个
// 格点，至少前 kXfadeMs 是原样的（除非中间紧跟着一个不在格点上的改长度 / 松开点）。
//
// 读的位置从绝对拍算（不是自己往前数）：录到离结尾还有 2 × kXfadeMs 时（第一遍的引入段开始之前）定下一张「帧格」
// （θ = (拍 - g0) / 当时的 dbeat，第 θ 帧 = 录下来的第 θ 帧），之后每个采样由它的拍算出 θ，再算出在第几遍、这一遍里的第几帧。
// 格点取整到帧（第一个「拍 ≥ 格点」的采样），所以速度不变时读的都是整数帧 = 原样拷贝，没有插值损失
// （离整数帧不到 kBeatEps 帧的都按整数帧读：拍是浮点数，算出来的位置带着 1e-8 帧量级的零头）；
// 格点是每一遍各自从拍算的：对拍的误差 < 1 帧，放多少遍都不累积。
// 变速（dbeat 在块之间变了）：θ 每个采样走 dbeat / 定帧格时的 dbeat 帧，按这个比例变速读（Lagrange 插值），
// 所以 roll 一直对在拍上；音高跟着速度变，和没开 keylock 的 deck 一样。录制中途变速：照常一帧一帧录，
// 按定帧格时的速度算（之前、之后录的那一点点速度差不管）。
// 录完之后还接着录一小段尾巴，录到「一遍按拍算有多少帧」+ 8 帧为止。交叉淡化用不着它（引入段在录音前面，不在后面）；
// 留着是因为：格点取整后某一遍可能比录的那一遍多 1 帧，变速时插值要多读 2 帧；录制中途慢下来时，按定帧格时的速度算
// 一遍比实际录到的帧数长（慢了 3% 就长 3%），多出来的这一截就接着录现场补上。缓冲装不下的（录了快 4 秒又慢下来
// 超过 1/64）和没录到的帧 = 静音。
// 最长 4 拍 @ 60 BPM = 4 秒，超过的按 4 秒录。全场没在走（dbeat ≤ 0）时直通。
// 松开点不晚于第一遍的格点（还没开始放）= 取消，全程直通。
// 定位到中间重新开始：从 captureBeat 之前至少 kXfadeMs 预跑过来，状态完全一样；从录制中途开始，没录到的部分
// （连同预卷）是静音。
class Roll {
  public:
    static constexpr int kMaxSeconds = 4;
    static constexpr int kMaxLenEvents = 16;

    void init(int sampleRate) {
        m_xf = dspd::msToFrames(kXfadeMs, sampleRate);
        m_maxFrames = static_cast<long long>(kMaxSeconds) * sampleRate;
        m_cap = m_maxFrames + m_maxFrames / 64 + 8;
        m_buf.assign(static_cast<size_t>(m_xf + m_cap) * 2, 0.0f); // 预卷 m_xf 帧 + 录音（含尾巴）
        m_mix.init(sampleRate, kParamSmoothMs);
        m_mix.set(1.0f);
        reset();
    }
    void reset() {
        m_phase = Idle;
        m_nEvt = 0;
        m_hasRel = false;
        m_recFrom = m_recTo = 0;
        m_histPos = m_histN = 0;
        m_started = false;
        m_segValid = false;
        m_nextBeat = -dspd::kHuge;
        m_mix.snap();
    }
    void setMix(float mix) { m_mix.set(std::min(std::max(mix, 0.0f), 1.0f)); }
    // 重新 arm 会丢掉上一次的（正在放的话是硬切；正常用法是先 release）
    void arm(double captureBeat, double lengthBeats) {
        m_phase = lengthBeats > 0.0 ? Wait : Idle;
        m_capBeat = captureBeat;
        m_len0 = lengthBeats;
        m_nEvt = 0;
        m_hasRel = false;
        m_recFrom = m_recTo = 0;
        m_histPos = m_histN = 0;
        m_started = false;
        m_segValid = false;
    }
    // 新长度 ≤ 录下来的长度（长了按录下来的算）；atBeat 早于录完的那一拍 = 录完起生效。最多 kMaxLenEvents 次。
    // 帧格定了以后才调、而 atBeat 已经过去或者离现在不到 kXfadeMs：推迟到「现在 + kXfadeMs」（淡化要在那一点之前走完）
    void setLength(double atBeat, double newLengthBeats) {
        if (m_phase == Idle || m_nEvt >= kMaxLenEvents || !(newLengthBeats > 0.0)) {
            return;
        }
        if (m_phase == Rolling) {
            atBeat = std::max(atBeat, m_nextBeat + m_xf * m_capDbeat);
        }
        int i = m_nEvt;
        while (i > 0 && m_evt[i - 1].atBeat > atBeat) {
            m_evt[i] = m_evt[i - 1];
            --i;
        }
        m_evt[i].atBeat = atBeat;
        m_evt[i].len = newLengthBeats;
        ++m_nEvt;
        if (m_phase == Rolling) {
            grid();
        }
    }
    // atBeat 不晚于第一遍的格点（还没开始放）= 取消。帧格定了以后才调：同 setLength，最早「现在 + kXfadeMs」
    void release(double atBeat) {
        if (m_phase == Idle) {
            return;
        }
        if (m_phase == Rolling) {
            atBeat = std::max(atBeat, m_nextBeat + m_xf * m_capDbeat);
        }
        m_hasRel = true;
        m_relBeat = atBeat;
        if (m_phase == Rolling) {
            grid();
        }
    }
    bool active() const { return m_phase != Idle; }                  // arm 了，还没 release 完
    bool rolling() const { return m_phase == Rolling && m_started; } // 录完了，在重放

    void process(const float* in, float* out, int frames, double beat0, double dbeat) {
        const double xf = m_xf;
        const double fixAhead = 2.0 * m_xf; // 离录完还有这么多帧时定帧格（也是一段最短的长度）
        for (int i = 0; i < frames; ++i) {
            const float l = in[2 * i], r = in[2 * i + 1];
            float ol = l, orr = r;
            bool rolled = false;
            if (m_phase != Idle && dbeat > 0.0) {
                const double b = beat0 + i * dbeat;
                if (m_phase == Wait) {
                    const double x = (b - m_capBeat) / dbeat;
                    if (m_hasRel && (b - m_relBeat) / dbeat >= -kBeatEps) {
                        m_phase = Idle;
                    } else if (x >= -kBeatEps) {
                        if (m_len0 / dbeat > static_cast<double>(m_maxFrames)) {
                            m_len0 = static_cast<double>(m_maxFrames) * dbeat;
                        }
                        // 正常是 0；从录制中途才开始处理时 = 已经错过的帧数（那一段是静音）
                        const double k0 = std::min(std::floor(x + kBeatEps), static_cast<double>(m_cap));
                        beginCapture(static_cast<long long>(k0));
                    }
                }
                if (m_phase == Capture) {
                    // y = 按现在的速度，离录完还差多少帧（负的）
                    const double y = (b - m_capBeat) / dbeat - std::max(m_len0 / dbeat, fixAhead);
                    if (m_hasRel && (b - m_relBeat) / dbeat >= -kBeatEps) {
                        m_phase = Idle;
                    } else if (y >= -fixAhead - kBeatEps || m_recTo >= m_maxFrames - 2 * m_xf) {
                        if (y < -fixAhead - kBeatEps) {
                            m_len0 = (b - m_capBeat) + fixAhead * dbeat; // 录制中途慢下来、缓冲快满了：再录 2 × kXfadeMs 就收
                        }
                        // 帧格定在这里：这个采样的 θ = 已经录的帧数。第一遍的格点还在 2 × kXfadeMs 之后，
                        // 它的引入段（最后 kXfadeMs）开始时帧格已经有了；这之后再变速，读的位置也是连续的。
                        m_capDbeat = dbeat;
                        m_g0 = b - static_cast<double>(m_recTo) * dbeat;
                        m_phase = Rolling;
                        m_started = false;
                        grid(); // 松开点不晚于第一遍的格点时在这里取消（→ Idle）
                    }
                }
                if (m_phase == Capture || m_phase == Rolling) {
                    const long long limit = m_phase == Capture ? m_cap : m_recLimit;
                    if (m_recTo < limit) {
                        float* d = frame(m_recTo);
                        d[0] = l;
                        d[1] = r;
                        ++m_recTo;
                    }
                }
                if (m_phase == Rolling) {
                    const double th = (b - m_g0) / m_capDbeat;
                    const double the = th + kBeatEps;
                    if (m_hasRel && the >= m_relTheta) {
                        m_phase = Idle; // 松开点（第一个「拍 ≥ atBeat」的采样）：从这个采样起就是现场
                    } else {
                        rolled = true;
                        if (the >= static_cast<double>(m_capFrames)) {
                            m_started = true;
                        }
                        const float w = m_mix.next();
                        if (w > 0.0f) {
                            if (!m_segValid || the < m_seg.jd || the >= m_seg.end) {
                                findSeg(the, m_seg);
                                m_segValid = true;
                                m_leadValid = false;
                            }
                            // v = 这个采样上 roll 这一路出什么。先是这一段本身（第一遍之前 = 现场）
                            bool wet = !m_seg.live;
                            float vl = l, vr = r;
                            if (wet) {
                                readSlice(th - m_seg.R, vl, vr);
                            }
                            // 再看后面的不连续点 J：进了它之前的 kXfadeMs，就往它的引入段淡。
                            // 通常只有一个；挨得很近时接着看再后面的一个（那一段不缓存，现算）。
                            double J = m_seg.end;
                            if (the >= J - xf) {
                                wet = true;
                                const Seg* nx = nullptr;
                                Seg deep;
                                for (;;) {
                                    const float t = static_cast<float>(dspd::raisedCos((th - J) / xf + 1.0));
                                    const bool rel = m_hasRel && J >= m_relC;
                                    float pl = l, pr = r; // 松开点的引入段 = 现场
                                    if (!rel) {
                                        if (nx == nullptr) {
                                            if (!m_leadValid) {
                                                findSeg(J, m_lead);
                                                m_leadValid = true;
                                            }
                                            nx = &m_lead;
                                        } else {
                                            findSeg(J, deep);
                                            nx = &deep;
                                        }
                                        readSlice(th - nx->R, pl, pr);
                                    }
                                    vl = vl * (1.0f - t) + pl * t;
                                    vr = vr * (1.0f - t) + pr * t;
                                    if (rel) {
                                        break;
                                    }
                                    J = nx->end;
                                    if (the < J - xf) {
                                        break;
                                    }
                                }
                            }
                            if (!wet) {
                                // 还是纯现场（第一遍的引入段之前）：原样
                            } else if (w >= 1.0f) {
                                ol = vl; // 不写成 l × 0 + v × 1：-ffast-math 下那样不保证逐位相同
                                orr = vr;
                            } else {
                                ol = l * (1.0f - w) + vl * w;
                                orr = r * (1.0f - w) + vr * w;
                            }
                        }
                    }
                }
            }
            if (m_phase == Wait) {
                // 还在等抓取点：把这一帧记进预卷的环（开始录的那个采样不进来，它是第 0 帧）
                float* d = &m_buf[static_cast<size_t>(m_histPos) * 2];
                d[0] = l;
                d[1] = r;
                if (++m_histPos == m_xf) {
                    m_histPos = 0;
                }
                if (m_histN < m_xf) {
                    ++m_histN;
                }
            }
            if (!rolled) {
                m_mix.snap(); // 帧格还没定 / 已经松开：mix 听不见，不用滑
            }
            out[2 * i] = ol;
            out[2 * i + 1] = orr;
        }
        m_nextBeat = beat0 + frames * dbeat;
    }

  private:
    enum Phase { Idle, Wait, Capture, Rolling }; // Rolling = 帧格定了（第一遍开始之前 2 × kXfadeMs 起）
    struct Evt {
        double atBeat = 0.0, len = 0.0;
        double c = 0.0; // 从帧格的第几帧起生效
        double l = 0.0; // 长度（帧格上的帧数，可以带小数）
    };
    // 一段连续的重放：θ 在 [jd, end) 里读第 θ - R 帧（R = 这一遍的格点）。end = 下一个不连续点。
    // 它的引入段 = θ 在 [jd - kXfadeMs, jd) 里也读第 θ - R 帧。
    struct Seg {
        double jd = 0.0, end = 0.0, R = 0.0;
        bool live = false; // 第一遍开始之前：这一段 = 现场
    };

    // 第 n 遍的格点：第一个「拍 ≥ captureBeat + n × 长度」的帧
    double passStart(double n, double len) const { return std::ceil(m_rho0 + n * len - kBeatEps); }

    // 录音的第 j 帧（-m_xf ≤ j < m_cap；负的 = 预卷）
    float* frame(long long j) { return &m_buf[static_cast<size_t>(j + m_xf) * 2]; }
    const float* frame(long long j) const { return &m_buf[static_cast<size_t>(j + m_xf) * 2]; }

    // 开始录：k0 = 已经错过的帧数（正常是 0）。等的时候预卷记在缓冲最前面 m_xf 帧里，当环用；
    // 这里把它按时间顺序摆好（最新的一帧 = 第 -1 帧）。原地转，不分配。
    void beginCapture(long long k0) {
        if (k0 == 0) {
            std::rotate(m_buf.begin(), m_buf.begin() + 2 * m_histPos, m_buf.begin() + 2 * m_xf);
            m_recFrom = -static_cast<long long>(m_histN);
        } else {
            m_recFrom = k0; // 中间缺了一截：之前记的不是紧挨着的前文，不用
        }
        m_recTo = k0;
        m_phase = Capture;
    }

    // 定帧格 / 改了长度 / 定了松开点之后：把拍换成帧格上的帧
    void grid() {
        const double minLen = 2.0 * m_xf;
        m_rho0 = (m_capBeat - m_g0) / m_capDbeat;
        m_len0Theta = std::max(m_len0 / m_capDbeat, minLen);
        m_capFrames = static_cast<long long>(passStart(1.0, m_len0Theta));
        // 录到哪为止：一遍最多读到第 ceil(长度) - 1 帧，插值再往后 2 帧
        m_recLimit = std::min(m_cap, std::max(m_capFrames, static_cast<long long>(std::ceil(m_len0Theta))) + 8);
        for (int i = 0; i < m_nEvt; ++i) {
            Evt& e = m_evt[i];
            e.c = std::max(static_cast<double>(m_capFrames), std::ceil((e.atBeat - m_g0) / m_capDbeat - kBeatEps));
            e.l = std::min(std::max(e.len / m_capDbeat, minLen), m_len0Theta);
        }
        if (m_hasRel) {
            // 松开点在帧格上的位置。淡化按取整到帧的 m_relC 算（速度不变时采样都在整数帧上，两个数是一回事）；
            // 回现场按不取整的 m_relTheta 判：变速后采样落在帧和帧之间，也是第一个到了 atBeat 的采样就回，不多等一帧
            m_relTheta = (m_relBeat - m_g0) / m_capDbeat;
            m_relC = std::ceil(m_relTheta - kBeatEps);
            if (m_relC <= static_cast<double>(m_capFrames)) {
                m_phase = Idle; // 还没开始放就松开 = 取消（这时输出还是纯现场：引入段最早从格点前 kXfadeMs 开始）
            }
        }
        m_segValid = false;
    }
    void findCore(double the, double& r, double& jd, double& end) const {
        int e = -1;
        for (int i = 0; i < m_nEvt && m_evt[i].c <= the; ++i) {
            e = i;
        }
        const double len = e < 0 ? m_len0Theta : m_evt[e].l;
        const double c = e < 0 ? static_cast<double>(m_capFrames) : m_evt[e].c;
        const double nextC = e + 1 < m_nEvt ? m_evt[e + 1].c : dspd::kHuge;
        double n = std::floor((the - m_rho0) / len);
        r = passStart(n, len);
        while (r > the) {
            n -= 1.0;
            r = passStart(n, len);
        }
        double rn = passStart(n + 1.0, len);
        while (rn <= the) {
            n += 1.0;
            r = rn;
            rn = passStart(n + 1.0, len);
        }
        jd = std::max(r, c);
        end = std::min(rn, nextC);
    }
    // θ = the 所在的那一段（the < 松开点）。传不连续点 J 本身 = 从 J 开始的那一段
    void findSeg(double the, Seg& s) const {
        if (the < static_cast<double>(m_capFrames)) {
            s.live = true;
            s.R = 0.0;
            s.jd = -dspd::kHuge;
            s.end = static_cast<double>(m_capFrames);
        } else {
            s.live = false;
            findCore(the, s.R, s.jd, s.end);
        }
        if (m_hasRel) {
            s.end = std::min(s.end, m_relC);
        }
    }
    // 读录音的第 p 帧（可以带小数；负的 = 预卷）；没录到的帧 = 0。
    // 小数部分离整数不到 kBeatEps 的按整数帧读（原样拷贝，不过插值）；中间的小数相应拉开一点，位置仍然连续。
    void readSlice(double p, float& l, float& r) const {
        const double fl = std::floor(p);
        long long i = static_cast<long long>(fl);
        double t = p - fl;
        if (t <= kBeatEps || t >= 1.0 - kBeatEps) {
            if (t > 0.5) {
                ++i;
            }
            if (i >= m_recFrom && i < m_recTo) {
                const float* s = frame(i);
                l = s[0];
                r = s[1];
            } else {
                l = 0.0f;
                r = 0.0f;
            }
            return;
        }
        t = (t - kBeatEps) / (1.0 - 2.0 * kBeatEps);
        float w[4];
        dspd::lagrange4(t, w);
        float sl = 0.0f, sr = 0.0f;
        for (int k = 0; k < 4; ++k) {
            const long long j = i - 1 + k;
            if (j >= m_recFrom && j < m_recTo) {
                const float* s = frame(j);
                sl += w[k] * s[0];
                sr += w[k] * s[1];
            }
        }
        l = sl;
        r = sr;
    }

    std::vector<float> m_buf; // [0, m_xf) = 预卷（第 -m_xf .. -1 帧），后面 = 录音的第 0 帧起
    int m_xf = 88;
    long long m_maxFrames = 0, m_cap = 0; // m_cap = 最多录多少帧（不算预卷）
    Phase m_phase = Idle;
    double m_capBeat = 0.0, m_len0 = 0.0;
    Evt m_evt[kMaxLenEvents];
    int m_nEvt = 0;
    bool m_hasRel = false;
    double m_relBeat = 0.0, m_relTheta = 0.0, m_relC = 0.0;
    long long m_recFrom = 0, m_recTo = 0; // 已经有了 [m_recFrom, m_recTo) 这些帧（m_recFrom < 0 = 带着预卷）
    int m_histPos = 0, m_histN = 0;       // 等抓取点时的预卷环：下一帧写在哪、已经有几帧
    // 帧格
    double m_g0 = 0.0, m_capDbeat = 1.0, m_rho0 = 0.0, m_len0Theta = 1.0;
    long long m_capFrames = 0; // 第一遍的格点 = 录的那一遍有多少帧
    long long m_recLimit = 0;  // 连尾巴一共录到第几帧为止
    bool m_started = false;    // 过了第一遍的格点
    Seg m_seg, m_lead;         // 现在这一段；它后面那一段（进了引入段才算）
    bool m_segValid = false, m_leadValid = false;
    double m_nextBeat = 0.0;
    dspd::Slew m_mix;
};

// 状态变量滤波（TPT / 梯形积分的 SVF）：LPF / HPF / BPF，12 dB / 倍频程。
// 选 TPT：截止频率随便扫（每个采样都在变）也稳定，频率一直准到 0.45 × 采样率，低频下 float 输入也不丢精度。
// 系数每个采样做一阶平滑（kSvfSmoothMs）：外面一块才给一次截止，里面抹平，扫频 / 换模式都不出爆音。
// 截止是在对数频率上平滑的（平滑 ln g，g = tan(π·fc / fs)）：按音程匀着走。直接平滑 g 的话，从低往高跳时
// 头几个采样就窜上去好几个倍频程，低通的相位滞后一下子放掉，等于一声轻的爆音（实测每采样的跳变是输入的 4 倍）。
// 共振（k = 1 / Q）和三种模式的混合比例是线性平滑。q = 品质因数（0.7071 = 没有峰的 Butterworth）。
// BPF 取峰值增益 1 的那种（中心频率上不增不减），所以 LPF + HPF + BPF 三种输出加起来正好是输入。
class SvfFilter {
  public:
    enum Mode { LPF = 0, HPF = 1, BPF = 2 };

    void init(int sampleRate) {
        m_sr = sampleRate;
        m_a = dspd::onePole(kSvfSmoothMs, sampleRate);
        set(LPF, 1000.0, 0.7071);
        reset();
    }
    void reset() {
        m_s[0] = m_s[1] = m_s[2] = m_s[3] = 0.0;
        m_fresh = true; // 下一个采样系数直接到目标
    }
    void set(Mode mode, double cutoffHz, double q) {
        setMode(mode);
        setCutoff(cutoffHz);
        setQ(q);
    }
    void setMode(Mode mode) {
        m_wT[0] = mode == LPF ? 1.0 : 0.0;
        m_wT[1] = mode == HPF ? 1.0 : 0.0;
        m_wT[2] = mode == BPF ? 1.0 : 0.0;
    }
    void setCutoff(double hz) {
        hz = std::min(std::max(hz, kSvfMinHz), kSvfMaxRatio * m_sr);
        m_gT = std::tan(dspd::kPi * hz / m_sr);
        m_lgT = std::log(m_gT);
    }
    void setQ(double q) { m_kT = 1.0 / std::min(std::max(q, kSvfMinQ), kSvfMaxQ); }

    // 一帧（原地）
    void tick(float& l, float& r) {
        if (m_fresh) {
            m_lg = m_lgT;
            m_g = m_gT;
            m_k = m_kT;
            m_w[0] = m_wT[0];
            m_w[1] = m_wT[1];
            m_w[2] = m_wT[2];
            m_fresh = false;
        } else {
            // 截止在动的时候才算 exp；差到 1e-7（听不出的频率差）以内就直接落到目标，之后不用再算
            const double d = m_lgT - m_lg;
            if (d > 1e-7 || d < -1e-7) {
                m_lg += m_a * d;
                m_g = std::exp(m_lg);
            } else if (d != 0.0) {
                m_lg = m_lgT;
                m_g = m_gT;
            }
            m_k += m_a * (m_kT - m_k);
            m_w[0] += m_a * (m_wT[0] - m_w[0]);
            m_w[1] += m_a * (m_wT[1] - m_w[1]);
            m_w[2] += m_a * (m_wT[2] - m_w[2]);
        }
        const double a1 = 1.0 / (1.0 + m_g * (m_g + m_k));
        const double a2 = m_g * a1;
        const double a3 = m_g * a2;
        l = one(l, m_s[0], m_s[1], a1, a2, a3);
        r = one(r, m_s[2], m_s[3], a1, a2, a3);
    }
    void process(const float* in, float* out, int frames) {
        for (int i = 0; i < frames; ++i) {
            float l = in[2 * i], r = in[2 * i + 1];
            tick(l, r);
            out[2 * i] = l;
            out[2 * i + 1] = r;
        }
    }

  private:
    float one(float x, double& ic1, double& ic2, double a1, double a2, double a3) const {
        const double v0 = static_cast<double>(x) + 1e-18; // 垫一点直流：没输入时状态不会衰减成非规格化数
        const double v3 = v0 - ic2;
        const double v1 = a1 * ic1 + a2 * v3; // 带通
        const double v2 = ic2 + a2 * ic1 + a3 * v3; // 低通
        ic1 = 2.0 * v1 - ic1;
        ic2 = 2.0 * v2 - ic2;
        return static_cast<float>(m_w[0] * v2 + m_w[1] * (v0 - m_k * v1 - v2) + m_w[2] * (m_k * v1));
    }

    int m_sr = 44100;
    double m_a = 0.01;
    double m_gT = 0.07, m_lgT = -2.66, m_kT = 1.414, m_wT[3] = {1.0, 0.0, 0.0}; // 目标
    double m_g = 0.07, m_lg = -2.66, m_k = 1.414, m_w[3] = {1.0, 0.0, 0.0};     // 现在（平滑后）
    double m_s[4] = {0.0, 0.0, 0.0, 0.0};
    bool m_fresh = true;
};

// Spiral：延迟时间会滑的反馈延迟，一圈一圈的回声越来越高（或越来越低）。
// 延迟（拍）= base × 2^(-semitones × u / 12)，u 从 glide 的 startBeat 起在 duration 拍里从 0 走到 1，之后停在 1。
// 延迟在缩短 = 读得比写得快 = 回声升调；一直在环路里转的声音，到滑完时累计升了约 semitones 个半音
// （严格说是延迟长度变了这么多；滑得越慢，回声的音高越接近这个数）。semitones < 0 = 降调（延迟变长，最长 4 秒）。
// 延迟从绝对拍算（纯函数 delayBeats()），换成帧 = 拍 / dbeat，再做一阶平滑（kSpiralSmoothMs）：
// 全场变速时 dbeat 一块一变，延迟帧数跟着一跳一跳，平滑后只是极轻微的音高波动，不出爆音。
// 小数延迟用 4 点 Lagrange 插值（幅频 ≤ 1，环路增益不会超过 feedback）；写进延迟线的值限幅到 ±kFbClamp。
// mix 是 DJ 混音台的那种：0 = 只有干声（和输入逐位相同），0.5 = 干声 + 满量的回声，1 = 只有回声（echo out）。
// send = 送进延迟线的量（默认 1；拉到 0 = 不再进新的，只留尾音）。
class Spiral {
  public:
    static constexpr int kMaxSeconds = 4;

    void init(int sampleRate) {
        m_maxDelay = static_cast<double>(kMaxSeconds) * sampleRate;
        m_ring.init(kMaxSeconds * sampleRate + 16);
        m_a = dspd::onePole(kSpiralSmoothMs, sampleRate);
        m_fb.init(sampleRate, kParamSmoothMs);
        m_mix.init(sampleRate, kParamSmoothMs);
        m_send.init(sampleRate, kParamSmoothMs);
        m_send.set(1.0f);
        m_d = 0.5 * sampleRate;
        reset();
    }
    void reset() {
        m_ring.clear();
        m_fresh = true;
    }
    void set(double baseDelayBeats, float feedback, float mix) {
        m_base = baseDelayBeats > 1e-6 ? baseDelayBeats : 1e-6;
        m_fb.set(std::min(std::max(feedback, 0.0f), kSpiralMaxFeedback));
        m_mix.set(std::min(std::max(mix, 0.0f), 1.0f));
    }
    // 从 startBeat 起 durationBeats 拍里滑 semitones 个半音（正 = 升）。semitones = 0 = 不滑
    void setGlide(double startBeat, double durationBeats, double semitones) {
        m_glideStart = startBeat;
        m_glideLen = durationBeats;
        m_semis = semitones;
    }
    void setSend(float send) { m_send.set(std::min(std::max(send, 0.0f), 1.0f)); }
    // 纯函数：全场第 beat 拍上的延迟（拍）
    double delayBeats(double beat) const {
        double u = m_glideLen > 0.0 ? (beat - m_glideStart) / m_glideLen : (beat >= m_glideStart ? 1.0 : 0.0);
        u = std::min(std::max(u, 0.0), 1.0);
        return m_base * std::exp2(-m_semis * u / 12.0);
    }
    void process(const float* in, float* out, int frames, double beat0, double dbeat) {
        for (int i = 0; i < frames; ++i) {
            if (dbeat > 0.0) { // 全场没在走：延迟留在原来的长度
                const double target = std::min(std::max(delayBeats(beat0 + i * dbeat) / dbeat, 4.0), m_maxDelay);
                if (m_fresh) {
                    m_d = target;
                } else {
                    m_d += m_a * (target - m_d);
                }
            }
            if (m_fresh) {
                m_fb.snap();
                m_mix.snap();
                m_send.snap();
                m_fresh = false;
            }
            const float fb = m_fb.next(), mix = m_mix.next(), send = m_send.next();
            const float l = in[2 * i], r = in[2 * i + 1];
            float wl, wr;
            m_ring.read(static_cast<double>(m_ring.count()) - m_d, wl, wr);
            m_ring.write(dspd::flushTiny(dspd::clampf(l * send + wl * fb, kFbClamp)),
                    dspd::flushTiny(dspd::clampf(r * send + wr * fb, kFbClamp)));
            const float dry = mix <= 0.5f ? 1.0f : 2.0f - 2.0f * mix;
            const float wet = mix >= 0.5f ? 1.0f : 2.0f * mix;
            out[2 * i] = l * dry + wl * wet;
            out[2 * i + 1] = r * dry + wr * wet;
        }
    }

  private:
    dspd::Ring m_ring;
    double m_maxDelay = 0.0, m_a = 0.01;
    double m_base = 0.5, m_glideStart = 0.0, m_glideLen = 0.0, m_semis = 0.0;
    double m_d = 22050.0; // 现在的延迟（帧，平滑后）
    dspd::Slew m_fb, m_mix, m_send;
    bool m_fresh = true;
};

// Noise：白噪声过一个带通，中心频率跟着拍扫（riser / 下行的「唰」）。
// 是「加出来的声音」：不看输入，process() 把噪声加进 out——调用的地方据此标成「不是曲子里的声音」。
// 噪声 = xorshift32，reset() 时重新播种，所以同样的调用顺序每次出来逐位相同；左右声道各取一个数（不相关，听着宽）。
// 中心频率是绝对拍的纯函数（cutoffAt()）：startBeat 起 lengthBeats 拍里从 fromHz 按指数（等音程）扫到 toHz，
// 两头之外停在端点。每 kCtlFrames 帧算一次（按 reset 以来的帧数对齐，和块怎么分无关），中间由 SvfFilter 的系数平滑接上。
// level = 线性增益（平滑 kParamSmoothMs）；白噪声本身约 -4.8 dBFS RMS，过了带通更小（Q 越高、中心越低越小）。
class Noise {
  public:
    static constexpr int kCtlFrames = 16;
    static constexpr std::uint32_t kDefaultSeed = 0x9E3779B9u;

    void init(int sampleRate) {
        m_svf.init(sampleRate);
        m_svf.setMode(SvfFilter::BPF);
        m_level.init(sampleRate, kParamSmoothMs);
        setQ(2.0);
        reset();
    }
    void reset() {
        m_rng = m_seed;
        m_count = 0;
        m_svf.reset();
        m_fresh = true;
    }
    void setSeed(std::uint32_t seed) { m_seed = seed != 0 ? seed : kDefaultSeed; } // 下次 reset() 起用
    void setLevel(float level) { m_level.set(std::max(level, 0.0f)); }
    void setQ(double q) { m_svf.setQ(q); }
    void setSweep(double startBeat, double lengthBeats, double fromHz, double toHz) {
        m_start = startBeat;
        m_len = lengthBeats;
        m_from = std::max(fromHz, kSvfMinHz);
        m_to = std::max(toHz, kSvfMinHz);
    }
    double cutoffAt(double beat) const {
        double u = m_len > 0.0 ? (beat - m_start) / m_len : (beat >= m_start ? 1.0 : 0.0);
        u = std::min(std::max(u, 0.0), 1.0);
        return m_from * std::pow(m_to / m_from, u);
    }
    void process(float* out, int frames, double beat0, double dbeat) {
        for (int i = 0; i < frames; ++i) {
            if ((m_count & (kCtlFrames - 1)) == 0) {
                m_svf.setCutoff(cutoffAt(beat0 + i * dbeat));
            }
            ++m_count;
            if (m_fresh) {
                m_level.snap();
                m_fresh = false;
            }
            float l = white(), r = white();
            m_svf.tick(l, r);
            const float g = m_level.next();
            out[2 * i] += l * g;
            out[2 * i + 1] += r * g;
        }
    }

  private:
    float white() {
        m_rng ^= m_rng << 13;
        m_rng ^= m_rng >> 17;
        m_rng ^= m_rng << 5;
        return static_cast<float>(static_cast<std::int32_t>(m_rng)) * (1.0f / 2147483648.0f);
    }

    SvfFilter m_svf;
    dspd::Slew m_level;
    std::uint32_t m_seed = kDefaultSeed, m_rng = kDefaultSeed;
    long long m_count = 0;
    double m_start = 0.0, m_len = 0.0, m_from = 1000.0, m_to = 1000.0;
    bool m_fresh = true;
};

// Flanger：一条很短的延迟（kFlangerMinMs..kFlangerMaxMs）被 LFO 来回扫，和干声相加 = 一排会动的梳状陷波。
// LFO 的相位从绝对拍算：位置 = 0.5 - 0.5·cos(2π·(拍 - phase) / period)，在周期起点延迟最短（陷波最高）。
// 延迟 = 最短 + depth × (最长 - 最短) × 位置，换成帧后做一阶平滑（kModSmoothMs），小数延迟用 Lagrange 插值。
// feedback（±kFlangerMaxFeedback）= 延迟的输出送回延迟线，陷波变尖、出「喷气」声；写回去的值限幅到 ±kFbClamp。
// mix：出 = 干 × (1 - mix/2) + 延迟 × mix/2。0 = 和输入逐位相同；1 = 干和延迟各一半（陷波最深，整体不变响）。
class Flanger {
  public:
    void init(int sampleRate) {
        m_sr = sampleRate;
        m_ring.init(static_cast<int>(kFlangerMaxMs * sampleRate / 1000.0) + 16);
        m_a = dspd::onePole(kModSmoothMs, sampleRate);
        m_fb.init(sampleRate, kParamSmoothMs);
        m_mix.init(sampleRate, kParamSmoothMs);
        m_d = kFlangerMinMs * sampleRate / 1000.0;
        reset();
    }
    void reset() {
        m_ring.clear();
        m_fresh = true;
    }
    void set(double periodBeats, float depth, float feedback, float mix, double phaseBeats = 0.0) {
        m_period = periodBeats > 1e-6 ? periodBeats : 1e-6;
        m_depth = std::min(std::max(static_cast<double>(depth), 0.0), 1.0);
        m_fb.set(dspd::clampf(feedback, kFlangerMaxFeedback));
        m_mix.set(std::min(std::max(mix, 0.0f), 1.0f));
        m_phase = phaseBeats;
    }
    void process(const float* in, float* out, int frames, double beat0, double dbeat) {
        const double minD = std::max(3.0, kFlangerMinMs * m_sr / 1000.0);
        const double span = std::max(0.0, kFlangerMaxMs * m_sr / 1000.0 - minD) * m_depth;
        for (int i = 0; i < frames; ++i) {
            const double ph = (beat0 + i * dbeat - m_phase) / m_period;
            const double target = minD + span * (0.5 - 0.5 * std::cos(2.0 * dspd::kPi * ph));
            if (m_fresh) {
                m_d = target;
                m_fb.snap();
                m_mix.snap();
                m_fresh = false;
            } else {
                m_d += m_a * (target - m_d);
            }
            const float fb = m_fb.next();
            const float wet = 0.5f * m_mix.next();
            const float l = in[2 * i], r = in[2 * i + 1];
            float dl, dr;
            m_ring.read(static_cast<double>(m_ring.count()) - m_d, dl, dr);
            m_ring.write(dspd::flushTiny(dspd::clampf(l + dl * fb, kFbClamp)),
                    dspd::flushTiny(dspd::clampf(r + dr * fb, kFbClamp)));
            out[2 * i] = l * (1.0f - wet) + dl * wet;
            out[2 * i + 1] = r * (1.0f - wet) + dr * wet;
        }
    }

  private:
    dspd::Ring m_ring;
    int m_sr = 44100;
    double m_a = 0.01, m_d = 11.0;
    double m_period = 4.0, m_depth = 1.0, m_phase = 0.0;
    dspd::Slew m_fb, m_mix;
    bool m_fresh = true;
};

// Phaser：kPhaserStages 级一阶全通串起来，和干声相加 = 3 个陷波，陷波的位置被 LFO 来回扫。
// LFO 同 Flanger，从绝对拍算；全通的转折频率 = 中心 × (最高 / 最低)^(depth × (位置 - 0.5))，
// 中心 = √(kPhaserMinHz × kPhaserMaxHz) ≈ 894 Hz：depth = 1 时在 200 Hz–4 kHz 之间按等音程扫，depth = 0 时停在中心
// （6 级全通在转折频率上正好转 3π，所以转折频率本身就是一个陷波）。扫频位置做一阶平滑（kModSmoothMs）。
// feedback（±kPhaserMaxFeedback）= 全通链的输出（上一个采样）送回输入，陷波变尖；mix 同 Flanger。
class Phaser {
  public:
    void init(int sampleRate) {
        m_sr = sampleRate;
        m_a = dspd::onePole(kModSmoothMs, sampleRate);
        m_fb.init(sampleRate, kParamSmoothMs);
        m_mix.init(sampleRate, kParamSmoothMs);
        reset();
    }
    void reset() {
        for (int c = 0; c < 2; ++c) {
            for (int s = 0; s < kPhaserStages; ++s) {
                m_z[c][s] = 0.0f;
            }
            m_last[c] = 0.0f;
        }
        m_fresh = true;
    }
    void set(double periodBeats, float depth, float feedback, float mix, double phaseBeats = 0.0) {
        m_period = periodBeats > 1e-6 ? periodBeats : 1e-6;
        m_depth = std::min(std::max(static_cast<double>(depth), 0.0), 1.0);
        m_fb.set(dspd::clampf(feedback, kPhaserMaxFeedback));
        m_mix.set(std::min(std::max(mix, 0.0f), 1.0f));
        m_phase = phaseBeats;
    }
    void process(const float* in, float* out, int frames, double beat0, double dbeat) {
        const double center = std::sqrt(kPhaserMinHz * kPhaserMaxHz);
        const double logRange = std::log(kPhaserMaxHz / kPhaserMinHz);
        const double maxHz = kSvfMaxRatio * m_sr;
        for (int i = 0; i < frames; ++i) {
            const double ph = (beat0 + i * dbeat - m_phase) / m_period;
            const double target = m_depth * (-0.5 * std::cos(2.0 * dspd::kPi * ph)); // -0.5..0.5：对数频率上离中心多远
            if (m_fresh) {
                m_pos = target;
                m_fb.snap();
                m_mix.snap();
                m_fresh = false;
            } else {
                m_pos += m_a * (target - m_pos);
            }
            const double t = std::tan(dspd::kPi * std::min(center * std::exp(logRange * m_pos), maxHz) / m_sr);
            const float a = static_cast<float>((t - 1.0) / (t + 1.0));
            const float fb = m_fb.next();
            const float wet = 0.5f * m_mix.next();
            for (int c = 0; c < 2; ++c) {
                const float x = in[2 * i + c];
                // 垫一点直流防非规格化数；限幅只是保险（全通不放大，环路增益 = |feedback| < 1）
                float u = dspd::clampf(x + m_last[c] * fb + 1e-18f, 2.0f * kFbClamp);
                float* z = m_z[c];
                for (int s = 0; s < kPhaserStages; ++s) {
                    const float y = a * u + z[s];
                    z[s] = u - a * y;
                    u = y;
                }
                m_last[c] = u;
                out[2 * i + c] = x * (1.0f - wet) + u * wet;
            }
        }
    }

  private:
    int m_sr = 44100;
    double m_a = 0.01, m_pos = 0.0;
    double m_period = 4.0, m_depth = 1.0, m_phase = 0.0;
    dspd::Slew m_fb, m_mix;
    float m_z[2][kPhaserStages] = {};
    float m_last[2] = {0.0f, 0.0f};
    bool m_fresh = true;
};

// 刹车 / 回转（Release 用）：平时直通，同时一直把输入记进一个环形缓冲（最近 kSeconds 秒）。
// brake(T)：黑胶刹车。播放速度在 T 帧里从 1 线性降到 0：读的位置比现场落后 n² / 2T 帧（n = 开始以来的帧数），
//   音高一路掉下去。开始那一帧落后 0 = 和现场接得上，不出爆音。
// backspin(T)：回转。速度在 T 帧里从 1 线性走到 -kSpinMaxSpeed（先一顿、掉头、越倒越快）：落后 (1 + 3)·n² / 2T 帧。
// 两种都在最后 kReleaseFadePart（至少 kXfadeMs）里升余弦淡出，放完之后出静音，直到 resume()：kXfadeMs 淡回现场
// （效果还没放完就 resume 也行：从效果交叉淡化到现场）。
// 读的位置是「开始以来的帧数」的闭式函数，不是一步步累加，所以和块怎么分无关。
// 时长上限：落后的帧数不能超出缓冲。刹车最多 2 × kSeconds 秒；回转最多 2 × kSeconds / 4 = 2 秒
// （所以缓冲取 4 秒而不是 3 秒：120 BPM 的一小节回转正好 2 秒）。超过的按上限算。
// 倒着 3 倍速读没有抗混叠（高频会折回来）：回转本来就是一声很脏的「唰」，不处理。
// 三个命令都排到下一次 process() 里、在 frameOffsetInBlock 那一帧生效（偏移超过这一块的顺延到后面的块）。
// brake / backspin 只在直通时接；别的时候（正在刹、静音着、正在淡回来）忽略。
class BrakeSpin {
  public:
    static constexpr int kSeconds = 4;
    static constexpr int kMaxCmds = 8;
    enum State { Live, Braking, Spinning, Silent };

    void init(int sampleRate) {
        m_xf = dspd::msToFrames(kXfadeMs, sampleRate);
        m_maxLag = static_cast<double>(kSeconds) * sampleRate;
        m_ring.init(kSeconds * sampleRate + 16);
        reset();
    }
    void reset() {
        m_ring.clear();
        m_fx = None;
        m_resume = -1;
        m_nCmd = 0;
    }
    void brake(int durationFrames, int frameOffsetInBlock = 0) { push(Brake, frameOffsetInBlock, durationFrames); }
    void backspin(int durationFrames, int frameOffsetInBlock = 0) { push(Spin, frameOffsetInBlock, durationFrames); }
    void resume(int frameOffsetInBlock = 0) { push(Resume, frameOffsetInBlock, 0); }
    int maxBrakeFrames() const { return static_cast<int>(2.0 * m_maxLag); }
    int maxSpinFrames() const { return static_cast<int>(2.0 * m_maxLag / (1.0 + kSpinMaxSpeed)); }
    // Live = 直通（含刚恢复完）；Braking / Spinning = 效果在放（含正在淡回现场）；Silent = 放完了，等 resume()
    State state() const {
        if (m_fx == None) {
            return Live;
        }
        return m_n >= m_T ? Silent : (m_fx == Brake ? Braking : Spinning);
    }

    void process(const float* in, float* out, int frames) {
        int ev = 0;
        for (int i = 0; i < frames; ++i) {
            while (ev < m_nCmd && m_cmd[ev].offset <= i) {
                apply(m_cmd[ev]);
                ++ev;
            }
            const float l = in[2 * i], r = in[2 * i + 1];
            m_ring.write(l, r);
            float ol = l, orr = r;
            if (m_fx != None) {
                float el = 0.0f, er = 0.0f;
                if (m_n < m_T) {
                    const double n = static_cast<double>(m_n);
                    const double lag = m_c * n * n / (2.0 * static_cast<double>(m_T));
                    const long long newest = m_ring.count() - 1;
                    if (lag < 1.0) {
                        // 离写的地方不到一帧：后面的帧还没来，没法 4 点插值，就在最新的两帧之间线性插
                        const float t = static_cast<float>(lag);
                        const float* a = m_ring.at(newest);
                        const float* b = m_ring.at(newest - 1);
                        el = a[0] * (1.0f - t) + b[0] * t;
                        er = a[1] * (1.0f - t) + b[1] * t;
                    } else {
                        m_ring.read(static_cast<double>(newest) - lag, el, er);
                    }
                    const long long fadeFrom = m_T - m_fade;
                    if (m_n >= fadeFrom) {
                        const float g = static_cast<float>(1.0 -
                                dspd::raisedCos(static_cast<double>(m_n - fadeFrom + 1) / static_cast<double>(m_fade + 1)));
                        el *= g;
                        er *= g;
                    }
                    ++m_n;
                }
                ol = el;
                orr = er;
                if (m_resume >= 0) {
                    const float f = static_cast<float>(
                            dspd::raisedCos(static_cast<double>(m_resume + 1) / static_cast<double>(m_xf + 1)));
                    ol = el * (1.0f - f) + l * f;
                    orr = er * (1.0f - f) + r * f;
                    if (++m_resume >= m_xf) {
                        m_resume = -1;
                        m_fx = None;
                    }
                }
            }
            out[2 * i] = ol;
            out[2 * i + 1] = orr;
        }
        // 这一块里没轮到的命令顺延
        int keep = 0;
        for (; ev < m_nCmd; ++ev) {
            m_cmd[keep] = m_cmd[ev];
            m_cmd[keep].offset -= frames;
            ++keep;
        }
        m_nCmd = keep;
    }

  private:
    enum Fx { None, Brake, Spin, Resume };
    struct Cmd {
        Fx type = None;
        int offset = 0;
        int duration = 0;
    };

    void push(Fx type, int offset, int duration) {
        if (m_nCmd >= kMaxCmds) {
            return;
        }
        offset = std::max(offset, 0);
        int i = m_nCmd;
        while (i > 0 && m_cmd[i - 1].offset > offset) {
            m_cmd[i] = m_cmd[i - 1];
            --i;
        }
        m_cmd[i].type = type;
        m_cmd[i].offset = offset;
        m_cmd[i].duration = duration;
        ++m_nCmd;
    }
    void apply(const Cmd& c) {
        if (c.type == Resume) {
            if (m_fx != None && m_resume < 0) {
                m_resume = 0;
            }
            return;
        }
        if (m_fx != None) {
            return;
        }
        m_fx = c.type;
        m_c = c.type == Brake ? 1.0 : 1.0 + kSpinMaxSpeed;
        const double maxT = 2.0 * m_maxLag / m_c;
        m_T = static_cast<long long>(std::min(std::max(static_cast<double>(c.duration), 1.0), maxT));
        m_fade = std::min(m_T, std::max(static_cast<long long>(m_xf),
                                      static_cast<long long>(std::llround(kReleaseFadePart * static_cast<double>(m_T)))));
        m_n = 0;
    }

    dspd::Ring m_ring;
    int m_xf = 88;
    double m_maxLag = 0.0;
    Fx m_fx = None;
    double m_c = 1.0;
    long long m_T = 1, m_n = 0, m_fade = 1;
    int m_resume = -1; // ≥ 0：正在淡回现场，已经走了这么多帧
    Cmd m_cmd[kMaxCmds];
    int m_nCmd = 0;
};

// 采样器：kSlots 个采样位，同时最多 kVoices 个声音。process() 把声音加进 out。
// load()：把采样拷进来（在 process 之外调，会分配内存；采样率要和引擎一样，这里不重采样）。
// trigger(slot, 偏移, gain)：排到下一次 process()，在这一块的第「偏移」帧起播（落在采样上；偏移超过这一块的顺延）。
// 规则（都是确定的，同样的调用顺序结果逐位相同）：
// - 同一个采样位再触发 = 从头重放：旧的那一下在 kXfadeMs 里淡出，新的从第一帧起就是满音量（不削鼓头）。
// - 4 个都在响时来第 5 个：抢掉「最早触发的那个」，被抢的 kXfadeMs 淡出。
// - 有空位时用编号最小的空位。
// - stopAll()：全部在 kXfadeMs 里淡出。
// 每个声音带一条「淡出尾巴」：被重触发 / 抢占 / 停掉的那一下挪到尾巴上放完 2 ms。
// 同一个声音 2 ms 内连着被顶两次时，前一条尾巴直接切掉（已经淡到一半，听不出来）。
// 采样放到头就停，不另外加淡出（采样自己的结尾该是干净的）。
class Sampler {
  public:
    static constexpr int kSlots = 8;
    static constexpr int kVoices = 4;
    static constexpr int kMaxEvents = 32;

    void init(int sampleRate) {
        m_xf = dspd::msToFrames(kXfadeMs, sampleRate);
        m_fade.resize(static_cast<size_t>(m_xf));
        for (int a = 0; a < m_xf; ++a) {
            m_fade[static_cast<size_t>(a)] = static_cast<float>(
                    1.0 - dspd::raisedCos(static_cast<double>(a + 1) / static_cast<double>(m_xf + 1)));
        }
        reset();
    }
    // 停掉所有声音、清掉排着的触发；采样留着
    void reset() {
        for (int v = 0; v < kVoices; ++v) {
            m_voice[v].slot = -1;
            m_tail[v].slot = -1;
        }
        m_nEvt = 0;
        m_seq = 0;
    }
    // stereo：交错立体声 frames 帧；nullptr / 0 = 清空这个位。正在放这个位的声音直接停
    void load(int slot, const float* stereo, int frames) {
        if (slot < 0 || slot >= kSlots) {
            return;
        }
        for (int v = 0; v < kVoices; ++v) {
            if (m_voice[v].slot == slot) {
                m_voice[v].slot = -1;
            }
            if (m_tail[v].slot == slot) {
                m_tail[v].slot = -1;
            }
        }
        if (stereo == nullptr || frames <= 0) {
            m_data[slot].clear();
            m_frames[slot] = 0;
            return;
        }
        m_data[slot].assign(stereo, stereo + static_cast<size_t>(frames) * 2);
        m_frames[slot] = frames;
    }
    int frames(int slot) const { return slot >= 0 && slot < kSlots ? m_frames[slot] : 0; }
    bool trigger(int slot, int frameOffsetInBlock, float gain = 1.0f) {
        if (slot < 0 || slot >= kSlots) {
            return false;
        }
        return push(slot, frameOffsetInBlock, gain);
    }
    void stopAll(int frameOffsetInBlock = 0) { push(-1, frameOffsetInBlock, 0.0f); }
    int activeVoices() const {
        int n = 0;
        for (int v = 0; v < kVoices; ++v) {
            n += m_voice[v].slot >= 0 ? 1 : 0;
        }
        return n;
    }

    void process(float* out, int frames) {
        int cur = 0, ev = 0;
        for (; ev < m_nEvt && m_evt[ev].offset < frames; ++ev) {
            const int off = std::max(m_evt[ev].offset, cur);
            render(out + 2 * cur, off - cur);
            cur = off;
            apply(m_evt[ev]);
        }
        render(out + 2 * cur, frames - cur);
        int keep = 0;
        for (; ev < m_nEvt; ++ev) {
            m_evt[keep] = m_evt[ev];
            m_evt[keep].offset -= frames;
            ++keep;
        }
        m_nEvt = keep;
    }

  private:
    struct Voice {
        int slot = -1; // -1 = 空
        int pos = 0;
        float gain = 1.0f;
        unsigned long long seq = 0; // 第几次触发（抢占时找最小的）
    };
    struct Tail {
        int slot = -1;
        int pos = 0;
        float gain = 1.0f;
        int age = 0; // 淡出走了多少帧
    };
    struct Evt {
        int slot = -1; // -1 = stopAll
        int offset = 0;
        float gain = 1.0f;
    };

    bool push(int slot, int offset, float gain) {
        if (m_nEvt >= kMaxEvents) {
            return false;
        }
        offset = std::max(offset, 0);
        int i = m_nEvt;
        while (i > 0 && m_evt[i - 1].offset > offset) {
            m_evt[i] = m_evt[i - 1];
            --i;
        }
        m_evt[i].slot = slot;
        m_evt[i].offset = offset;
        m_evt[i].gain = gain;
        ++m_nEvt;
        return true;
    }
    void toTail(int v) {
        if (m_voice[v].slot >= 0) {
            m_tail[v].slot = m_voice[v].slot;
            m_tail[v].pos = m_voice[v].pos;
            m_tail[v].gain = m_voice[v].gain;
            m_tail[v].age = 0;
            m_voice[v].slot = -1;
        }
    }
    void apply(const Evt& e) {
        if (e.slot < 0) {
            for (int v = 0; v < kVoices; ++v) {
                toTail(v);
            }
            return;
        }
        if (m_frames[e.slot] <= 0) {
            return;
        }
        int v = -1;
        for (int i = 0; i < kVoices && v < 0; ++i) {
            if (m_voice[i].slot == e.slot) {
                v = i; // 同一个位：重放
            }
        }
        for (int i = 0; i < kVoices && v < 0; ++i) {
            if (m_voice[i].slot < 0) {
                v = i; // 空位
            }
        }
        if (v < 0) {
            v = 0; // 抢最早触发的
            for (int i = 1; i < kVoices; ++i) {
                if (m_voice[i].seq < m_voice[v].seq) {
                    v = i;
                }
            }
        }
        toTail(v);
        m_voice[v].slot = e.slot;
        m_voice[v].pos = 0;
        m_voice[v].gain = e.gain;
        m_voice[v].seq = ++m_seq;
    }
    void render(float* out, int n) {
        if (n <= 0) {
            return;
        }
        for (int v = 0; v < kVoices; ++v) {
            Voice& vo = m_voice[v];
            if (vo.slot >= 0) {
                const int m = std::min(n, m_frames[vo.slot] - vo.pos);
                const float* d = &m_data[vo.slot][static_cast<size_t>(vo.pos) * 2];
                for (int j = 0; j < 2 * m; ++j) {
                    out[j] += d[j] * vo.gain;
                }
                vo.pos += m;
                if (vo.pos >= m_frames[vo.slot]) {
                    vo.slot = -1;
                }
            }
            Tail& ta = m_tail[v];
            if (ta.slot >= 0) {
                const int m = std::min(n, std::min(m_frames[ta.slot] - ta.pos, m_xf - ta.age));
                const float* d = &m_data[ta.slot][static_cast<size_t>(ta.pos) * 2];
                for (int j = 0; j < m; ++j) {
                    const float g = ta.gain * m_fade[static_cast<size_t>(ta.age + j)];
                    out[2 * j] += d[2 * j] * g;
                    out[2 * j + 1] += d[2 * j + 1] * g;
                }
                ta.pos += m;
                ta.age += m;
                if (ta.age >= m_xf || ta.pos >= m_frames[ta.slot]) {
                    ta.slot = -1;
                }
            }
        }
    }

    std::vector<float> m_data[kSlots];
    int m_frames[kSlots] = {};
    std::vector<float> m_fade; // 淡出曲线（kXfadeMs）
    int m_xf = 88;
    Voice m_voice[kVoices];
    Tail m_tail[kVoices];
    Evt m_evt[kMaxEvents];
    int m_nEvt = 0;
    unsigned long long m_seq = 0;
};

} // namespace djs5
