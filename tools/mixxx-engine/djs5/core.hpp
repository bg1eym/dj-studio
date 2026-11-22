// v5 执行计划（djplan/1）的数据结构：全场速度图、曲线、片段（源位置分段）、发送效果。
// 全部以「全场拍」为单位（第 1 小节第 1 拍 = 0 拍），由 Python 的时间线编译器（djstudio/v5/compile.py）算好；
// 这里只读不改，不做任何决策。不依赖 Qt / Mixxx：声音引擎和独立模拟器（sim5）用同一份。
#pragma once

#include <algorithm>
#include <cmath>
#include <map>
#include <string>
#include <vector>

#include "json.hpp"

namespace djs5 {

constexpr int kMaxDecks = 4;
constexpr const char* kPlanSchema = "djplan/1";

enum Param { P_LO = 0, P_MID, P_HI, P_FILTER, P_VOL, P_COUNT };
inline const char* paramName(int p) {
    static const char* names[P_COUNT] = {"lo", "mid", "hi", "filter", "vol"};
    return p >= 0 && p < P_COUNT ? names[p] : "?";
}
inline int paramIndex(const std::string& s) {
    for (int p = 0; p < P_COUNT; ++p) {
        if (s == paramName(p)) {
            return p;
        }
    }
    return -1;
}
// 各旋钮的中性值（Mixxx 原生值）：EQ 增益 1、滤波 0.5、推子 1
inline double paramNeutral(int p) {
    return p == P_FILTER ? 0.5 : 1.0;
}

// 分段线性曲线：点 (拍, 值)；同一拍上的两个点 = 阶跃（那一拍起取后一个）
struct Curve {
    std::vector<double> x, y;

    bool empty() const { return x.empty(); }
    double at(double b) const {
        if (x.empty()) {
            return 0.0;
        }
        if (b < x.front()) {
            return y.front();
        }
        const size_t i = static_cast<size_t>(std::upper_bound(x.begin(), x.end(), b) - x.begin()) - 1;
        if (i + 1 >= x.size()) {
            return y[i];
        }
        const double x0 = x[i], x1 = x[i + 1];
        return x1 > x0 ? y[i] + (y[i + 1] - y[i]) * (b - x0) / (x1 - x0) : y[i + 1];
    }
    static Curve parse(const Json& a) {
        Curve c;
        for (const Json& p : a.arr()) {
            c.x.push_back(p.at(0).num());
            c.y.push_back(p.at(1).num());
        }
        for (size_t i = 1; i < c.x.size(); ++i) {
            if (c.x[i] < c.x[i - 1]) {
                throw std::runtime_error("曲线的点没有按拍递增");
            }
        }
        return c;
    }
};

// 全场速度图：BPM 对「全场拍」分段线性（同一拍两个点 = 一下换速）。秒数按 BPM 曲线积分：
//   dt = 60 / bpm(b) db；一段里 bpm = v0 + k·u（u = 离段首的拍数）→ t(u) = 60/k · ln(1 + k·u/v0)，k = 0 时 60·u/v0。
// 所有 deck 都跟这一张图（速率 = 图上的 BPM / 曲子的网格 BPM），没有哪个 deck 自己用墙钟追拍。
struct TempoMap {
    std::vector<double> b, v, t; // 拍、BPM、到这一点的秒数（第一个点 = 0）

    static TempoMap parse(const Json& a) {
        TempoMap m;
        for (const Json& p : a.arr()) {
            m.b.push_back(p.at(0).num());
            m.v.push_back(p.at(1).num());
        }
        if (m.b.empty()) {
            throw std::runtime_error("速度图是空的");
        }
        for (size_t i = 0; i < m.b.size(); ++i) {
            if (!(m.v[i] > 20.0 && m.v[i] < 400.0)) {
                throw std::runtime_error("速度图里的 BPM 不合理");
            }
            if (i && m.b[i] < m.b[i - 1]) {
                throw std::runtime_error("速度图的点没有按拍递增");
            }
        }
        m.t.assign(m.b.size(), 0.0);
        for (size_t i = 1; i < m.b.size(); ++i) {
            m.t[i] = m.t[i - 1] + segSeconds(m.v[i - 1], m.v[i], m.b[i] - m.b[i - 1], m.b[i] - m.b[i - 1]);
        }
        return m;
    }
    // 一段（长 L 拍，BPM 从 v0 到 v1）里走 u 拍用的秒数
    static double segSeconds(double v0, double v1, double L, double u) {
        if (L <= 0.0 || u <= 0.0) {
            return 0.0;
        }
        const double k = (v1 - v0) / L;
        if (std::fabs(k) < 1e-12) {
            return 60.0 * u / v0;
        }
        return 60.0 / k * std::log1p(k * u / v0);
    }
    static double segBeats(double v0, double v1, double L, double sec) {
        const double k = L > 0.0 ? (v1 - v0) / L : 0.0;
        if (std::fabs(k) < 1e-12) {
            return sec * v0 / 60.0;
        }
        return v0 * std::expm1(k * sec / 60.0) / k;
    }
    double bpmAt(double beat) const {
        if (beat < b.front()) {
            return v.front();
        }
        const size_t i = static_cast<size_t>(std::upper_bound(b.begin(), b.end(), beat) - b.begin()) - 1;
        if (i + 1 >= b.size()) {
            return v[i];
        }
        const double L = b[i + 1] - b[i];
        return L > 0.0 ? v[i] + (v[i + 1] - v[i]) * (beat - b[i]) / L : v[i + 1];
    }
    double secAt(double beat) const {
        if (beat < b.front()) {
            return (beat - b.front()) * 60.0 / v.front();
        }
        const size_t i = static_cast<size_t>(std::upper_bound(b.begin(), b.end(), beat) - b.begin()) - 1;
        if (i + 1 >= b.size()) {
            return t[i] + (beat - b[i]) * 60.0 / v[i];
        }
        return t[i] + segSeconds(v[i], v[i + 1], b[i + 1] - b[i], beat - b[i]);
    }
    double beatAt(double sec) const {
        if (sec < 0.0) {
            return b.front() + sec * v.front() / 60.0;
        }
        const size_t i = static_cast<size_t>(std::upper_bound(t.begin(), t.end(), sec) - t.begin()) - 1;
        if (i + 1 >= b.size()) {
            return b[i] + (sec - t[i]) * v[i] / 60.0;
        }
        return b[i] + segBeats(v[i], v[i + 1], b[i + 1] - b[i], sec - t[i]);
    }
};

// 片段的一段源位置：全场第 g 拍时读到原曲的第 src 秒，之后一拍一拍往下读（原曲的拍，与全场拍 1:1）；
// loop > 0：在 [lsrc, lsrc + loop 拍) 里转（普通 loop：全场拍照常往前走，源位置回绕）。
// 相邻两段在交界处不连续 = 跳段；连续 = 进 loop / 出 loop。id = 这一段开头那个动作的编号（回执用）。
struct Seg {
    double g = 0.0;
    double src = 0.0;
    double loop = 0.0;
    double lsrc = 0.0;
    std::string id;
};

struct Clip {
    std::string id, asset;
    int deck = 1;
    int gen = 0;        // 装载代数：每次装载一个新的，迟到的回执和旧命令认不上就丢掉
    double load = 0.0;  // 全场拍：从这里开始装载（准备区间的开头）
    double start = 0.0; // 全场拍：起播
    double stop = 0.0;  // 全场拍：停
    double bpm = 120.0; // 这首的网格 BPM（速率 = 全场 BPM / 它）
    bool keylock = false;
    std::vector<Seg> segs;
    double init[P_COUNT];
    Curve curves[P_COUNT];

    double spb() const { return 60.0 / bpm; } // 原曲一拍多少秒
    int segIndex(double g) const {
        int i = 0;
        for (int k = 1; k < static_cast<int>(segs.size()); ++k) {
            if (segs[static_cast<size_t>(k)].g <= g) {
                i = k;
            }
        }
        return i;
    }
    // 这一段在全场第 g 拍读到原曲哪里（秒）。g 在段首之前 = 按直线往回推（预卷用）
    static double segSrc(const Seg& s, double g, double spb) {
        const double d = g - s.g;
        if (s.loop > 0.0 && d >= 0.0) {
            const double off = (s.src - s.lsrc) / spb;
            return s.lsrc + std::fmod(d + off, s.loop) * spb;
        }
        return s.src + d * spb;
    }
    double srcAt(double g) const { return segSrc(segs[static_cast<size_t>(segIndex(g))], g, spb()); }
    double value(int p, double g) const { return curves[p].empty() ? init[p] : curves[p].at(g); }
};

// 发送效果（Delay / Echo / Reverb）：一个片段的推子后信号送进自己的实例，返回只有效果声。
// slot = 用哪个实例（编译时排好：同一时间一个实例只给一个效果用，尾音也算在内）。
struct FxSpec {
    std::string id, kind, clip;
    int slot = 0;
    double start = 0.0, end = 0.0, tail = 0.0; // 全场拍：开、关送出、关之后尾音留多少拍
    std::map<std::string, double> p;           // 固定参数（beats、feedback、decay …）
    Curve send, wet;                           // 送出量 / 返回量（0..1）

    double param(const std::string& k, double def) const {
        auto it = p.find(k);
        return it == p.end() ? def : it->second;
    }
};

struct Plan {
    std::string exec; // 执行编号（谱哈希 + 编译器版本 + 能力签名算出来的）
    TempoMap tempo;
    std::vector<Clip> clips;
    std::vector<FxSpec> fx;
    std::vector<int> byDeck[kMaxDecks + 1]; // 每个 deck 上的片段（按起播排）
    std::map<std::string, int> clipIndex;
    double len = 0.0;

    static Plan parse(const Json& j, int maxDecks, int fxSlots) {
        if (j["t"].str("") != kPlanSchema) {
            throw std::runtime_error(std::string("不是 ") + kPlanSchema + " 的执行计划");
        }
        Plan pl;
        pl.exec = j["exec"].str("");
        pl.tempo = TempoMap::parse(j["tempo"]);
        pl.len = j["len"].num();
        for (const Json& c : j["clips"].arr()) {
            Clip k;
            k.id = c["id"].str("");
            k.asset = c["asset"].str("");
            k.deck = static_cast<int>(c["deck"].num());
            k.gen = static_cast<int>(c["gen"].num());
            k.load = c["load"].num();
            k.start = c["start"].num();
            k.stop = c["stop"].num();
            k.bpm = c["bpm"].num();
            k.keylock = c["keylock"].boolean();
            if (k.id.empty() || k.asset.empty()) {
                throw std::runtime_error("片段少了 id 或素材");
            }
            if (k.deck < 1 || k.deck > maxDecks) {
                throw std::runtime_error("片段 " + k.id + " 的 Deck 超出引擎的能力（最多 " + std::to_string(maxDecks) + " 台）");
            }
            if (!(k.bpm > 20.0) || !(k.stop > k.start) || k.load > k.start || k.gen <= 0) {
                throw std::runtime_error("片段 " + k.id + " 的时间 / BPM / 代数不对");
            }
            for (const Json& s : c["segs"].arr()) {
                Seg g;
                g.g = s["g"].num();
                g.src = s["src"].num();
                g.loop = s["loop"].num();
                g.lsrc = s.has("lsrc") ? s["lsrc"].num() : g.src;
                g.id = s["id"].str("");
                if (!k.segs.empty() && g.g <= k.segs.back().g) {
                    throw std::runtime_error("片段 " + k.id + " 的分段没有按拍递增");
                }
                k.segs.push_back(g);
            }
            if (k.segs.empty() || std::fabs(k.segs.front().g - k.start) > 1e-6) {
                throw std::runtime_error("片段 " + k.id + " 的第一段要从起播那一拍开始");
            }
            for (int p = 0; p < P_COUNT; ++p) {
                const Json& iv = c["init"][paramName(p)];
                k.init[p] = iv.isNum() ? iv.num() : paramNeutral(p);
                const Json& cv = c["curves"][paramName(p)];
                if (cv.isArr()) {
                    k.curves[p] = Curve::parse(cv);
                }
            }
            pl.clipIndex[k.id] = static_cast<int>(pl.clips.size());
            pl.clips.push_back(std::move(k));
        }
        for (int d = 1; d <= kMaxDecks; ++d) {
            for (int i = 0; i < static_cast<int>(pl.clips.size()); ++i) {
                if (pl.clips[static_cast<size_t>(i)].deck == d) {
                    pl.byDeck[d].push_back(i);
                }
            }
            std::sort(pl.byDeck[d].begin(), pl.byDeck[d].end(), [&pl](int a, int b2) {
                return pl.clips[static_cast<size_t>(a)].start < pl.clips[static_cast<size_t>(b2)].start;
            });
            for (size_t i = 1; i < pl.byDeck[d].size(); ++i) {
                const Clip& a = pl.clips[static_cast<size_t>(pl.byDeck[d][i - 1])];
                const Clip& n = pl.clips[static_cast<size_t>(pl.byDeck[d][i])];
                if (n.load < a.stop) {
                    throw std::runtime_error("Deck " + std::to_string(d) + "：" + n.id + " 要在 " + a.id + " 停下以后才能装");
                }
            }
        }
        for (const Json& f : j["fx"].arr()) {
            FxSpec x;
            x.id = f["id"].str("");
            x.kind = f["kind"].str("");
            x.clip = f["clip"].str("");
            x.slot = static_cast<int>(f["slot"].num());
            x.start = f["start"].num();
            x.end = f["end"].num();
            x.tail = f["tail"].num();
            for (const auto& kv : f["p"].obj()) {
                x.p[kv.first] = kv.second.num();
            }
            x.send = Curve::parse(f["send"]);
            x.wet = Curve::parse(f["wet"]);
            if (!pl.clipIndex.count(x.clip)) {
                throw std::runtime_error("效果 " + x.id + " 的目标片段 " + x.clip + " 不存在");
            }
            if (x.slot < 0 || x.slot >= fxSlots) {
                throw std::runtime_error("效果 " + x.id + " 要的实例超出引擎的能力（最多 " + std::to_string(fxSlots) + " 个同时）");
            }
            if (x.kind != "delay" && x.kind != "echo" && x.kind != "reverb") {
                throw std::runtime_error("效果 " + x.id + "：这个引擎不会「" + x.kind + "」");
            }
            if (!(x.end > x.start) || x.tail < 0.0) {
                throw std::runtime_error("效果 " + x.id + " 的时间不对");
            }
            pl.fx.push_back(std::move(x));
        }
        std::sort(pl.fx.begin(), pl.fx.end(), [](const FxSpec& a, const FxSpec& b2) { return a.start < b2.start; });
        return pl;
    }
};

} // namespace djs5
