// DJ Studio 真引擎测试台：把 DJStudio.js / DJStudio.midi.xml 装进 Mixxx 2.5.6 真正的 MidiController 与
// 脚本引擎，驱动真正的 deck / 引擎 / 效果 / 同步，可选把主输出录成 WAV。
//
// 编进 Mixxx 的 mixxx-test（见 tools/mixxx-harness/README.md），通过 stdin / stdout 与 Python 通信，
// 协议与 sim/fake_mixxx.mjs 相同：十六进制 = 原始 MIDI；'#' 开头 = 控制命令 / 回复（JSON）。
// 其他输出（gtest、qDebug）走 stderr 或被 Python 忽略。
//
// 环境变量：DJSTUDIO_MAPPING = DJStudio.midi.xml 路径；DJSTUDIO_SYSTEM_MAPPINGS = Mixxx 的 res/controllers
// （放 common-controller-scripts.js 的目录）；DJSTUDIO_SPEED = 引擎相对墙钟的倍速（默认 1）。
//
// 不是 Mixxx 的地方（明确列出）：
// - [AutoDJ],enabled / skip_next 由本文件里移植的 AutoDJProcessor 逻辑处理（toggleAutoDJ / P1LOADED /
//   skipNext / 队首删除），装载走真正的 Deck::slotLoadTrack；不模拟 AutoDJ 的自动淡化。
// - 没有 PlayerManager，所以「空 deck 上 eject 装回上一首」的逻辑不可用（Deck 构造时 PlayerManager 为空）。

#include <gtest/gtest.h>

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMutex>
#include <QThread>
#include <atomic>
#include <cstdio>
#include <deque>
#include <iostream>
#include <map>
#include <string>
#include <thread>

#include "control/controlobject.h"
#include "control/controlpushbutton.h"
#include "controllers/legacycontrollermappingfilehandler.h"
#include "controllers/midi/legacymidicontrollermapping.h"
#include "controllers/midi/midicontroller.h"
#include "effects/effectsmanager.h"
#include "sources/soundsourceproxy.h"
#include "test/signalpathtest.h"
#include "track/beats.h"
#include "track/track.h"
#include "util/time.h"

namespace {

std::FILE* gOut = stdout;
QMutex gOutLock;

void emitLine(const std::string& s) {
    QMutexLocker lock(&gOutLock);
    std::fputs(s.c_str(), gOut);
    std::fputc('\n', gOut);
    std::fflush(gOut);
}

std::string toHex(const QByteArray& b) {
    return b.toHex().toStdString();
}

class HarnessController : public MidiController {
  public:
    HarnessController()
            : MidiController(QStringLiteral("DJStudioHarness")) {
    }
    void sendShortMsg(unsigned char status, unsigned char byte1, unsigned char byte2) override {
        QByteArray b;
        b.append(static_cast<char>(status));
        b.append(static_cast<char>(byte1));
        b.append(static_cast<char>(byte2));
        out(b);
    }
    void sendBytes(const QByteArray& data) override {
        out(data);
    }
    bool isPolling() const override {
        return false;
    }
    int open() override {
        return 0;
    }
    int close() override {
        return 0;
    }
    bool start() {
        startEngine();
        return applyMapping();
    }
    void feed(const QByteArray& b) {
        if (b.isEmpty()) {
            return;
        }
        if (static_cast<unsigned char>(b.at(0)) == 0xF0) {
            receive(b, mixxx::Time::elapsed());
        } else if (b.size() == 3) {
            receivedShortMessage(static_cast<unsigned char>(b.at(0)),
                    static_cast<unsigned char>(b.at(1)),
                    static_cast<unsigned char>(b.at(2)),
                    mixxx::Time::elapsed());
        }
    }
    bool frozen = false;
    std::deque<QByteArray> loopback; // IAC 回环：Mixxx 发出的消息会回到自己的输入

  private:
    void out(const QByteArray& b) {
        if (frozen) {
            return;
        }
        emitLine(toHex(b));
        loopback.push_back(b);
    }
};

struct LibTrack {
    QString id;
    TrackPointer track;
};

} // namespace

class DJStudioHarness : public BaseSignalPathTest {
  protected:
    DJStudioHarness() {
        // EQ 与快速效果链（真 Mixxx 里由 PlayerManager::addDeckInner 注册），以及标准效果单元
        m_pEffectsManager->addDeck(ChannelHandleAndGroup(m_pChannel1->getHandle(), m_sGroup1));
        m_pEffectsManager->addDeck(ChannelHandleAndGroup(m_pChannel2->getHandle(), m_sGroup2));
        m_pEffectsManager->setup();
        m_autoDJEnabled = std::make_unique<ControlPushButton>(ConfigKey("[AutoDJ]", "enabled"));
        m_autoDJEnabled->setButtonMode(ControlPushButton::TOGGLE);
        m_autoDJEnabled->connectValueChangeRequest(m_autoDJEnabled.get(), [this](double v) {
            m_pending.push_back([this, v] { toggleAutoDJ(v > 0); });
        });
        m_skipNext = std::make_unique<ControlPushButton>(ConfigKey("[AutoDJ]", "skip_next"));
        QObject::connect(m_skipNext.get(), &ControlObject::valueChanged, m_skipNext.get(), [this](double v) {
            if (v > 0) {
                m_pending.push_back([this] { skipNext(); });
            }
        });
        m_crossfader = ControlObject::getControl(ConfigKey("[Master]", "crossfader"));
    }

    // ------------------------------------------------------------ AutoDJ（autodjprocessor.cpp 移植）

    Deck* deckN(int n) {
        return n == 1 ? m_pMixerDeck1 : m_pMixerDeck2;
    }
    double co(int n, const char* key) {
        return ControlObject::get(ConfigKey(QString("[Channel%1]").arg(n), key));
    }
    bool playing(int n) {
        return co(n, "play") > 0;
    }
    int leftDeck() {
        for (int n = 1; n <= 2; ++n) {
            if (co(n, "orientation") == 0) {
                return n;
            }
        }
        return 0;
    }
    int rightDeck() {
        for (int n = 1; n <= 2; ++n) {
            if (co(n, "orientation") == 2) {
                return n;
            }
        }
        return 0;
    }
    QString loadedId(int n) {
        TrackPointer t = deckN(n)->getLoadedTrack();
        if (!t) {
            return {};
        }
        for (const auto& [id, lt] : m_lib) {
            if (lt.track == t) {
                return id;
            }
        }
        return {};
    }
    bool removeLoadedTop(int n) {
        QString id = loadedId(n);
        if (id.isEmpty() || m_queue.empty() || m_queue.front() != id) {
            return false;
        }
        m_queue.pop_front();
        return true;
    }
    void scheduleLoad(int n, const QString& id, bool play) {
        auto it = m_lib.find(id);
        if (it == m_lib.end()) {
            return;
        }
        deckN(n)->slotLoadTrack(it->second.track, play);
        m_loadLog.push_back({m_engineMs, n, id});
    }
    bool loadNextFromQueue(int n, bool play) {
        if (m_queue.empty()) {
            toggleAutoDJ(false);
            if (!playing(n)) {
                ControlObject::set(ConfigKey(QString("[Channel%1]").arg(n), "eject"), 1);
            }
            return false;
        }
        scheduleLoad(n, m_queue.front(), play);
        return true;
    }
    void toggleAutoDJ(bool enable) {
        if (!enable) {
            m_adj = "DISABLED";
            m_autoDJEnabled->setAndConfirm(0);
            return;
        }
        int L = leftDeck(), R = rightDeck();
        if (!L || !R) {
            return;
        }
        bool lp = playing(L), rp = playing(R);
        if (lp && rp) {
            return;
        }
        if (lp) {
            removeLoadedTop(L);
        } else if (rp) {
            removeLoadedTop(R);
        } else if (co(L, "playposition") < 0.66 && removeLoadedTop(L)) {
            ControlObject::set(ConfigKey(QString("[Channel%1]").arg(L), "play"), 1);
            lp = true;
        } else if (co(R, "playposition") < 0.66 && removeLoadedTop(R)) {
            ControlObject::set(ConfigKey(QString("[Channel%1]").arg(R), "play"), 1);
            rp = true;
        }
        if (m_queue.empty()) {
            m_autoDJEnabled->setAndConfirm(0);
            return;
        }
        m_autoDJEnabled->setAndConfirm(1);
        if (!lp && !rp) {
            m_adj = "P1LOADED";
            m_crossfader->set(-1);
            scheduleLoad(L, m_queue.front(), true);
        } else {
            m_adj = "IDLE";
            if (lp) {
                scheduleLoad(R, m_queue.front(), false);
                m_crossfader->set(-1);
            } else {
                scheduleLoad(L, m_queue.front(), false);
                m_crossfader->set(1);
            }
        }
    }
    void autodjPositionTick() {
        if (m_adj != "P1LOADED") {
            return;
        }
        int L = leftDeck(), R = rightDeck();
        if (!L || !R) {
            return;
        }
        if (playing(L) || playing(R)) {
            m_adj = "IDLE";
            if (!playing(R)) {
                removeLoadedTop(L);
                loadNextFromQueue(R, false);
            }
        }
    }
    void skipNext() {
        if (m_adj == "DISABLED") {
            return;
        }
        int L = leftDeck(), R = rightDeck();
        if (!L || !R) {
            toggleAutoDJ(false);
            return;
        }
        if (!playing(L)) {
            removeLoadedTop(L);
            loadNextFromQueue(L, false);
        } else if (!playing(R)) {
            removeLoadedTop(R);
            loadNextFromQueue(R, false);
        }
    }

    // ------------------------------------------------------------ 曲目

    QJsonObject addTrack(const QJsonObject& t) {
        QString path = t["path"].toString();
        TrackPointer pTrack = Track::newTemporary(path);
        SoundSourceProxy(pTrack).updateTrackFromSource(
                SoundSourceProxy::UpdateTrackFromSourceMode::Once, SyncTrackMetadataParams{});
        const auto sr = pTrack->getSampleRate();
        if (t.contains("bpm") && sr.isValid()) {
            const double bpm = t["bpm"].toDouble();
            const double fb = t["first_beat"].toDouble();
            pTrack->trySetBeats(mixxx::Beats::fromConstTempo(sr,
                    mixxx::audio::FramePos(std::floor(fb * sr.toDouble())),
                    mixxx::Bpm(bpm),
                    QStringLiteral("djstudio-harness")));
        }
        m_lib[t["id"].toString()] = LibTrack{t["id"].toString(), pTrack};
        QJsonObject r;
        r["id"] = t["id"];
        r["duration"] = pTrack->getDuration();
        r["samplerate"] = static_cast<int>(sr.value());
        r["bpm"] = pTrack->getBpm();
        return r;
    }

    // ------------------------------------------------------------ 录音

    void startRecord(const QString& path) {
        m_rec.reset(new QFile(path));
        if (!m_rec->open(QIODevice::WriteOnly)) {
            m_rec.reset();
            return;
        }
        m_recFrames = 0;
        writeWavHeader();
    }
    void writeWavHeader() {
        const quint32 sr = 44100, ch = 2, bits = 16;
        const quint32 dataBytes = static_cast<quint32>(m_recFrames * ch * bits / 8);
        QByteArray h;
        auto u32 = [&h](quint32 v) { h.append(reinterpret_cast<const char*>(&v), 4); };
        auto u16 = [&h](quint16 v) { h.append(reinterpret_cast<const char*>(&v), 2); };
        h.append("RIFF");
        u32(36 + dataBytes);
        h.append("WAVEfmt ");
        u32(16);
        u16(1);
        u16(ch);
        u32(sr);
        u32(sr * ch * bits / 8);
        u16(ch * bits / 8);
        u16(bits);
        h.append("data");
        u32(dataBytes);
        m_rec->seek(0);
        m_rec->write(h);
        m_rec->seek(m_rec->size());
    }
    void recordBuffer() {
        if (!m_rec) {
            return;
        }
        const CSAMPLE* p = m_pEngineMixer->getMainBuffer();
        QByteArray b;
        b.resize(kProcessBufferSize * 2);
        auto* s = reinterpret_cast<qint16*>(b.data());
        for (int i = 0; i < kProcessBufferSize; ++i) {
            float v = std::max(-1.0f, std::min(1.0f, p[i]));
            s[i] = static_cast<qint16>(v * 32767.0f);
        }
        m_rec->write(b);
        m_recFrames += kProcessBufferSize / 2;
    }
    void stopRecord() {
        if (m_rec) {
            writeWavHeader();
            m_rec->close();
            m_rec.reset();
        }
    }

    // ------------------------------------------------------------ 状态

    QJsonObject state() {
        QJsonObject o;
        o["t"] = m_engineMs;
        o["autodj"] = m_adj;
        QJsonArray q;
        for (const auto& id : m_queue) {
            q.append(id);
        }
        o["queue"] = q;
        QJsonArray loads;
        for (const auto& l : m_loadLog) {
            loads.append(QJsonArray{l.t, l.deck, l.id});
        }
        o["loads"] = loads;
        o["crossfader"] = m_crossfader->get();
        o["xFaderMode"] = ControlObject::get(ConfigKey("[Mixer Profile]", "xFaderMode"));
        QJsonArray decks;
        for (int n = 1; n <= 2; ++n) {
            QJsonObject d;
            d["n"] = n;
            QString id = loadedId(n);
            d["id"] = id.isEmpty() ? QJsonValue() : QJsonValue(id);
            d["playing"] = playing(n);
            EngineBuffer* eb = deckN(n)->getEngineDeck()->getEngineBuffer();
            TrackPointer t = deckN(n)->getLoadedTrack();
            const auto pos = eb->getExactPlayPos();
            double posSec = 0, beat = 0;
            if (t && pos.isValid() && t->getSampleRate().isValid()) {
                posSec = pos.value() / t->getSampleRate().toDouble();
                auto beats = t->getBeats();
                if (beats) {
                    // 恒定网格：用首个标记推算拍号（与 Python 侧 Grid 一致）
                    const auto first = beats->firstBeat();
                    const double bpm = beats->getBpmInRange(mixxx::audio::kStartFramePos,
                                                     mixxx::audio::FramePos(t->getDuration() *
                                                             t->getSampleRate().toDouble()))
                                               .value();
                    beat = (pos.value() - first.value()) / t->getSampleRate().toDouble() * bpm / 60.0;
                }
            }
            d["pos"] = posSec;
            d["beat"] = beat;
            d["bpm"] = co(n, "bpm");
            d["rate"] = co(n, "rate_ratio");
            d["volume"] = co(n, "volume");
            decks.append(d);
        }
        o["decks"] = decks;
        return o;
    }

    // ------------------------------------------------------------ 命令

    void simCommand(const QJsonObject& c) {
        QJsonObject r;
        r["_id"] = c["_id"];
        const QString cmd = c["cmd"].toString();
        if (cmd == "library") {
            QJsonArray out;
            for (const auto& v : c["tracks"].toArray()) {
                out.append(addTrack(v.toObject()));
            }
            r["ok"] = true;
            r["tracks"] = out;
        } else if (cmd == "queue") {
            m_queue.clear();
            for (const auto& v : c["ids"].toArray()) {
                m_queue.push_back(v.toString());
            }
            r["ok"] = true;
        } else if (cmd == "load") {
            scheduleLoad(c["deck"].toInt(), c["id"].toString(), false);
            r["ok"] = true;
        } else if (cmd == "freeze") {
            m_frozen = true;
            m_ctrl->frozen = true;
            r["ok"] = true;
        } else if (cmd == "unfreeze") {
            m_frozen = false;
            m_ctrl->frozen = false;
            r["ok"] = true;
        } else if (cmd == "state") {
            r = state();
            r["_id"] = c["_id"];
        } else if (cmd == "get") {
            r["v"] = ControlObject::get(ConfigKey(c["group"].toString(), c["key"].toString()));
        } else if (cmd == "set") {
            ControlObject::set(ConfigKey(c["group"].toString(), c["key"].toString()), c["value"].toDouble());
            r["ok"] = true;
        } else if (cmd == "record") {
            startRecord(c["path"].toString());
            r["ok"] = static_cast<bool>(m_rec);
        } else if (cmd == "record_stop") {
            stopRecord();
            r["ok"] = true;
        } else if (cmd == "quit") {
            m_quit = true;
            r["ok"] = true;
        } else {
            r["ok"] = false;
            r["err"] = QString(QStringLiteral("unknown command ") + cmd);
        }
        emitLine("#" + QJsonDocument(r).toJson(QJsonDocument::Compact).toStdString());
    }

    struct LoadEntry {
        qint64 t;
        int deck;
        QString id;
    };

    std::unique_ptr<HarnessController> m_ctrl;
    std::unique_ptr<ControlPushButton> m_autoDJEnabled;
    std::unique_ptr<ControlPushButton> m_skipNext;
    ControlObject* m_crossfader = nullptr;
    std::map<QString, LibTrack> m_lib;
    std::deque<QString> m_queue;
    std::vector<LoadEntry> m_loadLog;
    std::vector<std::function<void()>> m_pending;
    QString m_adj = "DISABLED";
    std::unique_ptr<QFile> m_rec;
    qint64 m_recFrames = 0;
    qint64 m_engineMs = 0;
    bool m_frozen = false;
    bool m_quit = false;
};

TEST_F(DJStudioHarness, Run) {
    const QByteArray mappingPath = qgetenv("DJSTUDIO_MAPPING");
    if (mappingPath.isEmpty()) {
        GTEST_SKIP() << "DJSTUDIO_MAPPING 未设置";
    }
    const double speed = qEnvironmentVariableIsSet("DJSTUDIO_SPEED")
            ? qEnvironmentVariable("DJSTUDIO_SPEED").toDouble()
            : 1.0;

    // gtest / qDebug 写 stdout 会混进协议行：协议用单独复制的 stdout，标准输出改到 stderr
    gOut = fdopen(dup(fileno(stdout)), "w");
    dup2(fileno(stderr), fileno(stdout));

    // Mixxx 总会先加载系统映射目录里的 common-controller-scripts.js
    // （legacycontrollermappingfilehandler.cpp:233 REQUIRED_SCRIPT_FILE）
    const QDir systemMappings(qEnvironmentVariable("DJSTUDIO_SYSTEM_MAPPINGS"));
    auto pMapping = LegacyControllerMappingFileHandler::loadMapping(
            QFileInfo(QString::fromLocal8Bit(mappingPath)), systemMappings);
    ASSERT_TRUE(pMapping) << "映射加载失败";
    m_ctrl = std::make_unique<HarnessController>();
    m_ctrl->setMapping(pMapping);
    ASSERT_TRUE(m_ctrl->start()) << "脚本初始化失败";

    // stdin 读线程
    QMutex inLock;
    std::deque<std::string> inbox;
    std::atomic<bool> eof{false};
    std::thread reader([&] {
        std::string line;
        while (std::getline(std::cin, line)) {
            QMutexLocker lock(&inLock);
            inbox.push_back(line);
        }
        eof = true;
    });
    reader.detach();

    const double bufferMs = kProcessBufferSize / 2 / 44100.0 * 1000.0;
    const qint64 t0 = mixxx::Time::elapsed().toIntegerMillis();
    while (!m_quit && !eof) {
        std::deque<std::string> lines;
        {
            QMutexLocker lock(&inLock);
            lines.swap(inbox);
        }
        for (auto& line : lines) {
            if (line.empty()) {
                continue;
            }
            if (line[0] == '#') {
                simCommand(QJsonDocument::fromJson(QByteArray::fromStdString(line.substr(1))).object());
                continue;
            }
            emitLine(line); // IAC 回环：发送者自己也收到
            if (!m_frozen) {
                m_ctrl->feed(QByteArray::fromHex(QByteArray::fromStdString(line)));
            }
        }
        if (!m_frozen) {
            // 按倍速推进音频，直到引擎时间追上墙钟
            const qint64 wall = mixxx::Time::elapsed().toIntegerMillis() - t0;
            int n = 0;
            while (m_engineMs < wall * speed && n < 64) {
                ProcessBuffer();
                recordBuffer();
                m_engineMs += static_cast<qint64>(bufferMs);
                autodjPositionTick();
                ++n;
            }
            auto pending = std::move(m_pending);
            m_pending.clear();
            for (auto& f : pending) {
                f();
            }
            QCoreApplication::processEvents();
            while (!m_ctrl->loopback.empty()) {
                QByteArray b = m_ctrl->loopback.front();
                m_ctrl->loopback.pop_front();
                m_ctrl->feed(b);
            }
        }
        QThread::msleep(2);
    }
    stopRecord();
}
