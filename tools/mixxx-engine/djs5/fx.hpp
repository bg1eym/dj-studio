// v5 的发送效果（DJ 常用的 Delay / Echo / Reverb）：每个实例有自己的缓冲、尾音和参数，互不影响。
// 效果版本 FX_VERSION 跟着算法和固定参数走：改了算法 / 默认值就加一（谱保存时记下版本，升级后同一份谱不会悄悄变声）。
#pragma once

#include <algorithm>
#include <cmath>
#include <functional>
#include <memory>
#include <vector>

namespace djs5 {

constexpr int kFxVersion = 1;

// Delay / Echo：和 Mixxx 2.5.6 的 Echo 同一个算法（effects/backends/builtin/echoeffect.cpp:120-255）——
// 环形缓冲，写入 = 输入 × 送出 + 读出 × 反馈（限幅到 ±1）；延迟长度变了（全场升速）时一块里从旧读点渐变到新读点，
// 不硬跳。输出只有效果声（干声由通道自己出，这里是「发送 / 返回」）。
// Delay 和 Echo 的区别只在默认反馈：Delay 反馈 0（只回一次），Echo 反馈 -3 dB（一串越来越小的回声）。
class DelayLine {
  public:
    static constexpr int kMaxSeconds = 3; // 同 Mixxx 的 EchoGroupState::kMaxDelaySeconds

    void init(int sampleRate) {
        m_buf.assign(static_cast<size_t>(kMaxSeconds) * static_cast<size_t>(sampleRate) * 2, 0.0f);
        reset();
    }
    // send：之后每块用的送出量（调度器把送出的开关和大小先乘进输入里，这里固定是 1，不再从 0 爬上来）
    void reset(float send = 0.0f) {
        std::fill(m_buf.begin(), m_buf.end(), 0.0f);
        m_write = 0;
        m_prevDelay = 0;
        m_prevSend = send;
        m_prevFeedback = 0.0f;
        m_pingPong = 0;
    }
    // 反馈一下换成 fb（不从上一块的值爬过去）：Release Echo 在开始那一帧起就要满反馈，那一帧上的鼓头才留得住
    void snapFeedback(float fb) { m_prevFeedback = fb; }
    // in / out：交错立体声 frames 帧。delayFrames：这一块的延迟长度（帧）
    void process(const float* in, float* out, int frames, int delayFrames, float send, float feedback, float pingpong) {
        const int size = static_cast<int>(m_buf.size());
        const int samples = frames * 2;
        int delaySamples = std::max(2, delayFrames * 2);
        if (delaySamples > size) {
            delaySamples = size;
        }
        int prevRead = m_write;
        dec(prevRead, m_prevDelay, size);
        int read = m_write;
        dec(read, delaySamples, size);
        const float sendInc = (send - m_prevSend) / static_cast<float>(frames);
        const float fbInc = (feedback - m_prevFeedback) / static_cast<float>(frames);
        int ramp = 0;
        for (int i = 0; i < samples; i += 2) {
            const float s = m_prevSend + sendInc * static_cast<float>(ramp);
            const float fb = m_prevFeedback + fbInc * static_cast<float>(ramp);
            ++ramp;
            float l = m_buf[static_cast<size_t>(read)];
            float r = m_buf[static_cast<size_t>(read) + 1];
            if (read != prevRead) {
                const float frac = static_cast<float>(i) / static_cast<float>(samples);
                l = l * frac + m_buf[static_cast<size_t>(prevRead)] * (1.0f - frac);
                r = r * frac + m_buf[static_cast<size_t>(prevRead) + 1] * (1.0f - frac);
                inc(prevRead, 2, size);
            }
            inc(read, 2, size);
            m_buf[static_cast<size_t>(m_write)] = clamp(in[i] * s + l * fb);
            m_buf[static_cast<size_t>(m_write) + 1] = clamp(in[i + 1] * s + r * fb);
            if (m_pingPong < delaySamples / 2) {
                out[i] = (l + r * pingpong) / (1.0f + pingpong);
                out[i + 1] = r * (1.0f - pingpong);
            } else {
                out[i] = l * (1.0f - pingpong);
                out[i + 1] = (r + l * pingpong) / (1.0f + pingpong);
            }
            inc(m_write, 2, size);
            if (++m_pingPong >= delaySamples) {
                m_pingPong = 0;
            }
        }
        m_prevSend = send;
        m_prevFeedback = feedback;
        m_prevDelay = delaySamples;
    }

  private:
    static void inc(int& p, int by, int size) {
        p += by;
        if (p >= size) {
            p -= size;
        }
    }
    static void dec(int& p, int by, int size) {
        p -= by;
        if (p < 0) {
            p += size;
        }
    }
    static float clamp(float v) { return v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v); }

    std::vector<float> m_buf;
    int m_write = 0;
    int m_prevDelay = 0;
    float m_prevSend = 0.0f;
    float m_prevFeedback = 0.0f;
    int m_pingPong = 0;
};

// Reverb：声音引擎里是 Mixxx 的板式混响（lib/reverb 的 MixxxPlateX2，和 v4 的 Reverb 同一个）；模拟器里是一个很简单的
// 梳状 / 全通混响（只为了让测试里有尾音，音色不作数）。
class Reverb {
  public:
    virtual ~Reverb() = default;
    virtual void reset() = 0;
    // in / out：交错立体声 frames 帧；输出只有效果声。send / sendPrev：这一块末尾 / 开头的送出量
    virtual void process(const float* in, float* out, int frames, float decay, float bandwidth, float damping,
            float send, float sendPrev) = 0;
};
using ReverbFactory = std::function<std::unique_ptr<Reverb>(int sampleRate)>;

class SimpleReverb : public Reverb {
  public:
    explicit SimpleReverb(int sampleRate) {
        const int combs[4] = {1557, 1617, 1491, 1422};
        const int alls[2] = {225, 556};
        for (int i = 0; i < 4; ++i) {
            m_comb[i].assign(static_cast<size_t>(combs[i] * sampleRate / 44100 + 1), 0.0f);
        }
        for (int i = 0; i < 2; ++i) {
            m_all[i].assign(static_cast<size_t>(alls[i] * sampleRate / 44100 + 1), 0.0f);
        }
    }
    void reset() override {
        for (auto& c : m_comb) {
            std::fill(c.begin(), c.end(), 0.0f);
        }
        for (auto& a : m_all) {
            std::fill(a.begin(), a.end(), 0.0f);
        }
    }
    void process(const float* in, float* out, int frames, float decay, float, float, float send, float sendPrev) override {
        const float fb = 0.70f + 0.28f * decay;
        for (int n = 0; n < frames; ++n) {
            const float s = sendPrev + (send - sendPrev) * static_cast<float>(n) / static_cast<float>(frames);
            const float x = 0.5f * (in[2 * n] + in[2 * n + 1]) * s;
            float acc = 0.0f;
            for (int i = 0; i < 4; ++i) {
                auto& c = m_comb[i];
                const float y = c[m_ci[i]];
                c[m_ci[i]] = x + y * fb;
                m_ci[i] = (m_ci[i] + 1) % c.size();
                acc += y;
            }
            acc *= 0.25f;
            for (int i = 0; i < 2; ++i) {
                auto& a = m_all[i];
                const float y = a[m_ai[i]];
                a[m_ai[i]] = acc + y * 0.5f;
                m_ai[i] = (m_ai[i] + 1) % a.size();
                acc = y - acc * 0.5f;
            }
            out[2 * n] = acc;
            out[2 * n + 1] = acc;
        }
    }

  private:
    std::vector<float> m_comb[4], m_all[2];
    size_t m_ci[4] = {0, 0, 0, 0}, m_ai[2] = {0, 0};
};

} // namespace djs5
