// DJ Studio 声音引擎（djstudio-engine）：只用 Mixxx 2.5.6 里打碟要用的部分——
//   deck / EngineBuffer（播放、变速、同步）、EngineMixer（推子、crossfader、主输出、电平）、
//   EQ 与快速效果（滤波）、效果单元（Echo / Reverb）、MidiController + 脚本引擎（跑 DJStudio.js）、
//   解码（SoundSourceProxy，Mac 上是 CoreAudio）。
// 没有界面、曲库、皮肤、广播、黑胶控制、HID，也不带测试框架。Mixxx 的核心是静态库，用不到的代码不会进程序。
//
// 四种用法：
//   djstudio-engine                 演出：stdin / stdout 与 Python 通信（djstudio/engine/link.py 的 EngineLink），
//                                   协议与 sim/fake_mixxx.mjs 相同：十六进制 = 原始 MIDI；'#' 开头 = 控制命令 / 回复（JSON）。
//                                   声音经 PortAudio 输出到系统默认输出设备，引擎按声卡的时钟走（和 Mixxx 一样）。
//   djstudio-engine --probe <文件>  打印 JSON：时长、采样率、声道、码率、标签
//   djstudio-engine --decode <文件> <输出> <采样率>
//                                   解码成单声道 float32（小端）写到输出文件，按给定采样率重采样（分析用）。
//                                   输出第 i 点 = 播放时曲目的第 i 帧（解码器起点不是 0 时前面补静音，见 decodeFile）
//   djstudio-engine --render <文件> <输出> <秒数> [起点秒]
//                                   离线渲染：deck 1 原速播放，主输出下混成单声道 float32（44100 Hz）。
//                                   对时测试用：和 --decode 的结果逐点比，确认分析看到的就是播放出来的
//
// 环境变量：DJSTUDIO_MAPPING = DJStudio.midi.xml 路径；DJSTUDIO_SYSTEM_MAPPINGS = 放 common-controller-scripts.js
// 的目录（默认：程序旁边的 res/controllers）；DJSTUDIO_SPEED = 引擎相对墙钟的倍速（默认 1；不是 1 时没有声音）；
// DJSTUDIO_AUDIO = 0 时不出声（测试 / 预演）；DJSTUDIO_AUDIO_FAKE = <路径.wav> 时用按真实时钟取数据的假声卡
// （测试实时节奏，取到的声音写成 WAV）。
//
// 不是 Mixxx 的地方（明确列出）：
// - [AutoDJ],enabled / skip_next 由本文件里移植的 AutoDJProcessor 逻辑处理（toggleAutoDJ / P1LOADED /
//   skipNext / 队首删除），装载走真正的 Deck::slotLoadTrack；不模拟 AutoDJ 的自动淡化。
// - 没有 PlayerManager，所以「空 deck 上 eject 装回上一首」的逻辑不可用（Deck 构造时 PlayerManager 为空）。
// - 声音输出是这里直接开的 PortAudio 输出流（Mixxx 的 SoundManager 也是 PortAudio）。和 Mixxx 一样，
//   引擎在声卡回调里处理（每次 512 帧）；主线程只跑脚本、定时器和命令。

#include <portaudio.h>

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMutex>
#include <QTemporaryDir>
#include <QThread>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <numeric>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

#include "control/controlindicatortimer.h"
#include "control/controlobject.h"
#include "control/controlpushbutton.h"
#include "controllers/legacycontrollermappingfilehandler.h"
#include "controllers/midi/legacymidicontrollermapping.h"
#include "controllers/midi/midicontroller.h"
#include "effects/effectsmanager.h"
#include "engine/channels/enginedeck.h"
#include "engine/enginebuffer.h"
#include "engine/enginemixer.h"
#include "mixer/deck.h"
#include "mixer/playerinfo.h"
#include "mixxxapplication.h"
#include "preferences/usersettings.h"
#include "sources/audiosourcestereoproxy.h"
#include "sources/soundsourceproxy.h"
#include "track/beats.h"
#include "track/track.h"
#include "track/trackmetadata.h"
#include "util/logging.h"
#include "util/samplebuffer.h"
#include "util/time.h"

namespace {

constexpr int kSampleRate = 44100;
constexpr double kPi = 3.14159265358979323846;
constexpr int kBufferSamples = 1024; // 每次处理 512 帧（立体声），和 Mixxx 自己的测试一样
constexpr int kBufferFrames = kBufferSamples / 2;

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

class EngineController : public MidiController {
  public:
    EngineController()
            : MidiController(QStringLiteral("DJStudioEngine")) {
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

// 声音输出：系统默认输出设备，每次回调固定 512 帧。和 Mixxx 的 SoundManager 一样，引擎就在声卡回调里处理
// （render），主线程只管脚本、定时器和命令——主线程偶尔卡一下也不会断音。
class AudioOut {
  public:
    using Render = std::function<void(float*)>; // 写 kBufferSamples 个交错立体声采样

    // fakePath 不空：不开声卡，用一个按真实时钟取数据的「假声卡」线程，把取到的声音写成 WAV（测试实时节奏用）
    bool open(Render render, const QString& fakePath = QString()) {
        m_render = std::move(render);
        if (!fakePath.isEmpty()) {
            return openFake(fakePath);
        }
        if (Pa_Initialize() != paNoError) {
            m_error = QStringLiteral("PortAudio 初始化失败");
            return false;
        }
        m_initialized = true;
        const PaDeviceIndex dev = Pa_GetDefaultOutputDevice();
        if (dev == paNoDevice) {
            m_error = QStringLiteral("没有声音输出设备");
            return false;
        }
        PaStreamParameters out{};
        out.device = dev;
        out.channelCount = 2;
        out.sampleFormat = paFloat32;
        out.suggestedLatency = Pa_GetDeviceInfo(dev)->defaultLowOutputLatency;
        PaError e = Pa_OpenStream(&m_stream, nullptr, &out, kSampleRate, kBufferFrames, paNoFlag,
                &AudioOut::callback, this);
        if (e != paNoError) {
            m_error = QString::fromUtf8(Pa_GetErrorText(e));
            m_stream = nullptr;
            return false;
        }
        m_device = QString::fromUtf8(Pa_GetDeviceInfo(dev)->name);
        m_deviceRate = Pa_GetDeviceInfo(dev)->defaultSampleRate;
        if (Pa_StartStream(m_stream) != paNoError) {
            m_error = QStringLiteral("声音输出启动失败");
            Pa_CloseStream(m_stream);
            m_stream = nullptr;
            return false;
        }
        m_latencyMs = Pa_GetStreamInfo(m_stream)->outputLatency * 1000.0;
        return true;
    }
    void close() {
        if (m_fake.joinable()) {
            m_fakeStop = true;
            m_fake.join();
        }
        if (m_stream) {
            Pa_StopStream(m_stream);
            Pa_CloseStream(m_stream);
            m_stream = nullptr;
        }
        if (m_initialized) {
            Pa_Terminate();
            m_initialized = false;
        }
    }
    bool active() const {
        return m_stream != nullptr || m_fake.joinable();
    }
    QJsonObject info() const {
        QJsonObject o;
        o["on"] = active();
        o["device"] = m_device;
        o["latency_ms"] = m_latencyMs + kBufferFrames * 1000.0 / kSampleRate;
        o["underruns"] = static_cast<qint64>(m_underruns.load()); // 声卡报告的断音次数
        o["played_ms"] = static_cast<double>(m_played.load()) * 1000.0 / kSampleRate; // 声卡已经取走的
        o["rate"] = kSampleRate;           // 引擎输出的采样率
        if (m_deviceRate > 0) {
            o["device_rate"] = m_deviceRate; // 设备本身的采样率（不同时由系统转换）
        }
        if (!m_error.isEmpty()) {
            o["error"] = m_error;
        }
        return o;
    }
    QString m_error;

  private:
    static int callback(const void*, void* output, unsigned long frames, const PaStreamCallbackTimeInfo*,
            PaStreamCallbackFlags flags, void* user) {
        auto* self = static_cast<AudioOut*>(user);
        if (flags & paOutputUnderflow) {
            self->m_underruns++;
        }
        // PortAudio 按约定每次要 512 帧；万一不是（某些驱动 / 采样率转换），也按 512 帧一块渲染，余下的留到下次
        auto* o = static_cast<float*>(output);
        unsigned long done = 0;
        while (done < frames) {
            if (self->m_carryPos >= kBufferFrames) {
                self->m_render(self->m_carry.data());
                self->m_carryPos = 0;
            }
            const unsigned long n = std::min<unsigned long>(frames - done, kBufferFrames - self->m_carryPos);
            std::copy_n(self->m_carry.data() + self->m_carryPos * 2, n * 2, o + done * 2);
            self->m_carryPos += static_cast<int>(n);
            done += n;
        }
        self->m_played += static_cast<qint64>(frames);
        return paContinue;
    }
    bool openFake(const QString& path) {
        auto* f = new QFile(path);
        if (!f->open(QIODevice::WriteOnly)) {
            m_error = QStringLiteral("写不了 ") + path;
            delete f;
            return false;
        }
        m_device = QStringLiteral("fake");
        m_fake = std::thread([this, f] {
            const quint32 sr = kSampleRate;
            f->write(QByteArray(44, '\0'));
            std::vector<float> buf(kBufferSamples);
            std::vector<qint16> pcm(kBufferSamples);
            qint64 frames = 0;
            const auto t0 = std::chrono::steady_clock::now();
            while (!m_fakeStop) {
                std::this_thread::sleep_until(t0 + std::chrono::microseconds((frames + kBufferFrames) * 1000000 / sr));
                callback(nullptr, buf.data(), kBufferFrames, nullptr, 0, this);
                for (int i = 0; i < kBufferSamples; ++i) {
                    pcm[i] = static_cast<qint16>(std::max(-1.0f, std::min(1.0f, buf[i])) * 32767.0f);
                }
                f->write(reinterpret_cast<const char*>(pcm.data()), kBufferSamples * 2);
                frames += kBufferFrames;
            }
            // WAV 头（16 位立体声）
            const quint32 data = static_cast<quint32>(frames * 4);
            QByteArray h;
            auto u32 = [&h](quint32 v) { h.append(reinterpret_cast<const char*>(&v), 4); };
            auto u16 = [&h](quint16 v) { h.append(reinterpret_cast<const char*>(&v), 2); };
            h.append("RIFF");
            u32(36 + data);
            h.append("WAVEfmt ");
            u32(16);
            u16(1);
            u16(2);
            u32(sr);
            u32(sr * 4);
            u16(4);
            u16(16);
            h.append("data");
            u32(data);
            f->seek(0);
            f->write(h);
            f->close();
            delete f;
        });
        return true;
    }
    Render m_render;
    std::vector<float> m_carry = std::vector<float>(kBufferSamples);
    int m_carryPos = kBufferFrames;
    PaStream* m_stream = nullptr;
    bool m_initialized = false;
    QString m_device;
    double m_latencyMs = 0;
    double m_deviceRate = 0;
    std::atomic<qint64> m_underruns{0};
    std::atomic<qint64> m_played{0};
    std::thread m_fake;
    std::atomic<bool> m_fakeStop{false};
};

struct LibTrack {
    QString id;
    TrackPointer track;
};

// EngineMixer 的主输出 / 监听默认是关着的（真 Mixxx 里由 SoundManager 按声卡配置打开）
class OutputEngineMixer : public EngineMixer {
  public:
    using EngineMixer::EngineMixer;
    void enableOutputs() {
        m_pMainEnabled->forceSet(1);
        m_pHeadphoneEnabled->forceSet(1);
        m_pBoothEnabled->forceSet(1);
    }
};

const QString kMainGroup = QStringLiteral("[Master]");

} // namespace

// ------------------------------------------------------------ 引擎（两个 deck + 混音器 + 效果 + 控制器）

class Engine {
  public:
    explicit Engine(UserSettingsPointer config)
            : m_pConfig(config) {
        // 与 Mixxx 自己的 BaseSignalPathTest（src/test/signalpathtest.h）同样的搭法，只要两个 deck
        m_pControlIndicatorTimer = std::make_unique<mixxx::ControlIndicatorTimer>();
        m_pChannelHandleFactory = std::make_shared<ChannelHandleFactory>();
        m_pNumDecks = std::make_unique<ControlObject>(ConfigKey(QStringLiteral("[App]"), QStringLiteral("num_decks")));
        m_pNumSamplers = std::make_unique<ControlObject>(
                ConfigKey(QStringLiteral("[App]"), QStringLiteral("num_samplers")));
        m_pNumPreviewDecks = std::make_unique<ControlObject>(
                ConfigKey(QStringLiteral("[App]"), QStringLiteral("num_preview_decks")));
        m_pEffectsManager = new EffectsManager(m_pConfig, m_pChannelHandleFactory);
        m_pEngineMixer = new OutputEngineMixer(m_pConfig, kMainGroup, m_pEffectsManager, m_pChannelHandleFactory, false);
        m_pEngineMixer->enableOutputs();
        for (int n = 1; n <= 2; ++n) {
            const QString group = QString("[Channel%1]").arg(n);
            Deck* d = new Deck(nullptr, m_pConfig, m_pEngineMixer, m_pEffectsManager, EngineChannel::CENTER,
                    m_pEngineMixer->registerChannelGroup(group));
            m_decks[n] = d;
            ControlObject::set(ConfigKey(group, "main_mix"), 1.0);
            ControlObject::set(ConfigKey(group, "rate_dir"), 1.0);
            ControlObject::set(ConfigKey(group, "rateRange"), 0.08);
            m_pNumDecks->set(m_pNumDecks->get() + 1);
        }
        ControlObject::set(ConfigKey(kMainGroup, "enabled"), 1.0);
        PlayerInfo::create();
        // EQ 与快速效果链（真 Mixxx 里由 PlayerManager::addDeckInner 注册），以及标准效果单元
        for (int n = 1; n <= 2; ++n) {
            m_pEffectsManager->addDeck(ChannelHandleAndGroup(
                    m_decks[n]->getEngineDeck()->getHandle(), QString("[Channel%1]").arg(n)));
        }
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
        m_crossfader = ControlObject::getControl(ConfigKey(kMainGroup, "crossfader"));
    }

    int run(const QByteArray& mappingPath, const QDir& systemMappings, double speed, bool wantAudio) {
        // Mixxx 总会先加载系统映射目录里的 common-controller-scripts.js
        // （legacycontrollermappingfilehandler.cpp:233 REQUIRED_SCRIPT_FILE）
        auto pMapping = LegacyControllerMappingFileHandler::loadMapping(
                QFileInfo(QString::fromLocal8Bit(mappingPath)), systemMappings);
        if (!pMapping) {
            std::fprintf(stderr, "映射加载失败：%s\n", mappingPath.constData());
            return 2;
        }
        m_ctrl = std::make_unique<EngineController>();
        m_ctrl->setMapping(pMapping);
        if (!m_ctrl->start()) {
            std::fprintf(stderr, "脚本初始化失败\n");
            return 3;
        }
        const QString fake = qEnvironmentVariable("DJSTUDIO_AUDIO_FAKE");
        // 出声时引擎在声卡回调线程里处理（同 Mixxx）；冻结（测试里模拟卡顿）时输出静音
        const auto render = [this](float* out) {
            if (m_frozen) {
                std::fill(out, out + kBufferSamples, 0.0f);
                return;
            }
            m_pEngineMixer->process(kBufferSamples);
            const CSAMPLE* p = m_pEngineMixer->getMainBuffer();
            std::copy(p, p + kBufferSamples, out);
            m_engineFrames += kBufferFrames;
        };
        if (wantAudio && speed == 1.0 && !m_audio.open(render, fake)) {
            std::fprintf(stderr, "没有声音：%s\n", m_audio.m_error.toUtf8().constData());
        }

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
                    command(QJsonDocument::fromJson(QByteArray::fromStdString(line.substr(1))).object());
                    continue;
                }
                emitLine(line); // IAC 回环：发送者自己也收到
                if (!m_frozen) {
                    m_ctrl->feed(QByteArray::fromHex(QByteArray::fromStdString(line)));
                }
            }
            if (!m_frozen) {
                if (m_audio.active()) {
                    autodjPositionTick(); // 引擎在声卡线程里走；AutoDJ 在主线程跟着看（同 Mixxx 的 AutoDJProcessor）
                } else {
                    // 不出声：按倍速推进，直到引擎时间追上墙钟
                    const double wall = static_cast<double>(mixxx::Time::elapsed().toIntegerMillis() - t0);
                    int n = 0;
                    while (engineMs() < wall * speed && n < 64) {
                        processOne();
                        ++n;
                    }
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
        m_audio.close();
        return 0;
    }

  private:
    double engineMs() const {
        return static_cast<double>(m_engineFrames) * 1000.0 / kSampleRate;
    }
    void processOne() {
        m_pEngineMixer->process(kBufferSamples);
        recordBuffer();
        m_engineFrames += kBufferFrames;
        autodjPositionTick();
    }

    // ------------------------------------------------------------ AutoDJ（autodjprocessor.cpp 移植）

    Deck* deckN(int n) {
        return m_decks[n];
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
        m_loadLog.push_back({static_cast<qint64>(engineMs()), n, id});
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
                    QStringLiteral("djstudio-engine")));
        }
        m_lib[t["id"].toString()] = LibTrack{t["id"].toString(), pTrack};
        QJsonObject r;
        r["id"] = t["id"];
        r["duration"] = pTrack->getDuration();
        r["samplerate"] = static_cast<int>(sr.value());
        r["bpm"] = pTrack->getBpm();
        return r;
    }

    // ------------------------------------------------------------ 录音（测试用：把主输出录成 WAV）

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
        const quint32 sr = kSampleRate, ch = 2, bits = 16;
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
        b.resize(kBufferSamples * 2);
        auto* s = reinterpret_cast<qint16*>(b.data());
        for (int i = 0; i < kBufferSamples; ++i) {
            float v = std::max(-1.0f, std::min(1.0f, p[i]));
            s[i] = static_cast<qint16>(v * 32767.0f);
        }
        m_rec->write(b);
        m_recFrames += kBufferFrames;
    }
    void stopRecord() {
        if (m_rec) {
            writeWavHeader();
            m_rec->close();
            m_rec.reset();
        }
    }

    // ------------------------------------------------------------ 离线渲染（--render，对时测试用）

  public:
    int render(const QString& path, const QString& outPath, double seconds, double startSec) {
        QJsonObject t;
        t["id"] = QStringLiteral("r");
        t["path"] = path;
        addTrack(t);
        scheduleLoad(1, QStringLiteral("r"), false);
        // 停着走引擎，直到装载完成（读盘线程装好后在引擎处理里回报，见 CachingReader::process）
        const auto idle = [this](int n) {
            for (int i = 0; i < n; ++i) {
                m_pEngineMixer->process(kBufferSamples);
                QCoreApplication::processEvents();
                QThread::msleep(2);
            }
        };
        for (int i = 0; i < 2500 && !(co(1, "track_loaded") > 0 && co(1, "track_samples") > 0); ++i) {
            idle(1);
        }
        if (!(co(1, "track_loaded") > 0)) {
            std::fprintf(stderr, "装载失败：%s\n", path.toUtf8().constData());
            return 1;
        }
        TrackPointer track = deckN(1)->getLoadedTrack();
        const double sr = track->getSampleRate().toDouble();
        ControlObject::set(ConfigKey("[Channel1]", "playposition"), startSec * sr / (co(1, "track_samples") / 2));
        idle(100); // 定位 + 让读盘线程把开头几块读进缓存
        EngineBuffer* eb = deckN(1)->getEngineDeck()->getEngineBuffer();
        const double start = eb->getExactPlayPos().value();
        ControlObject::set(ConfigKey("[Channel1]", "play"), 1.0);
        QFile f(outPath);
        if (!f.open(QIODevice::WriteOnly)) {
            std::fprintf(stderr, "写不了：%s\n", outPath.toUtf8().constData());
            return 1;
        }
        const long buffers = static_cast<long>(std::ceil(seconds * kSampleRate / kBufferFrames));
        std::vector<float> mono(kBufferFrames);
        for (long b = 0; b < buffers; ++b) {
            m_pEngineMixer->process(kBufferSamples);
            const CSAMPLE* p = m_pEngineMixer->getMainBuffer();
            for (int i = 0; i < kBufferFrames; ++i) {
                mono[static_cast<size_t>(i)] = 0.70710678f * (p[2 * i] + p[2 * i + 1]);
            }
            f.write(reinterpret_cast<const char*>(mono.data()), kBufferFrames * sizeof(float));
            QThread::msleep(1); // 让读盘线程跟上（真放的时候它有 11.6 ms）
        }
        QJsonObject o;
        o["start"] = start; // 输出第 i 点 = 曲目第 start + i 帧（原速、曲目 44100 Hz 时）
        o["track_rate"] = sr;
        o["rate"] = kSampleRate;
        emitLine(QJsonDocument(o).toJson(QJsonDocument::Compact).toStdString());
        return 0;
    }

  private:
    // ------------------------------------------------------------ 状态

    QJsonObject state() {
        QJsonObject o;
        o["t"] = static_cast<qint64>(engineMs());
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
        o["audio"] = m_audio.info();
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

    void command(const QJsonObject& c) {
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
        } else if (cmd == "audio") {
            r = m_audio.info();
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

    UserSettingsPointer m_pConfig;
    std::unique_ptr<mixxx::ControlIndicatorTimer> m_pControlIndicatorTimer;
    ChannelHandleFactoryPointer m_pChannelHandleFactory;
    std::unique_ptr<ControlObject> m_pNumDecks;
    std::unique_ptr<ControlObject> m_pNumSamplers;
    std::unique_ptr<ControlObject> m_pNumPreviewDecks;
    EffectsManager* m_pEffectsManager = nullptr;
    OutputEngineMixer* m_pEngineMixer = nullptr;
    std::map<int, Deck*> m_decks;
    std::unique_ptr<EngineController> m_ctrl;
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
    std::atomic<qint64> m_engineFrames{0};
    std::atomic<bool> m_frozen{false};
    bool m_quit = false;
    AudioOut m_audio;
};

// ------------------------------------------------------------ 分析用：读信息、解码

namespace {

int probeFile(const QString& path) {
    TrackPointer t = Track::newTemporary(path);
    // 只读文件里的标签（没有标签就是空的；不像 Mixxx 曲库那样从文件名猜艺人 / 标题）
    mixxx::TrackMetadata meta;
    SoundSourceProxy(t).importTrackMetadataAndCoverImage(&meta, nullptr, false);
    mixxx::AudioSource::OpenParams params;
    const auto src = SoundSourceProxy(t).openAudioSource(params);
    QJsonObject o;
    if (!src) {
        o["error"] = QStringLiteral("打不开（不支持的格式或文件损坏）");
        emitLine(QJsonDocument(o).toJson(QJsonDocument::Compact).toStdString());
        return 1;
    }
    const auto sr = src->getSignalInfo().getSampleRate();
    o["duration"] = static_cast<double>(src->frameLength()) / sr.toDouble();
    o["samplerate"] = static_cast<int>(sr.value());
    o["channels"] = static_cast<int>(src->getSignalInfo().getChannelCount());
    o["first_frame"] = static_cast<qint64>(src->frameIndexRange().start()); // 解码器起点（Mac 的 CoreAudio 不是 0）
    o["bitrate"] = static_cast<int>(meta.getStreamInfo().getBitrate().value()) * 1000;
    o["codec"] = QFileInfo(path).suffix().toLower();
    o["title"] = meta.getTrackInfo().getTitle();
    o["artist"] = meta.getTrackInfo().getArtist();
    o["album"] = meta.getAlbumInfo().getTitle();
    emitLine(QJsonDocument(o).toJson(QJsonDocument::Compact).toStdString());
    return 0;
}

// 加窗 sinc 重采样（Blackman 窗，每边 32 个零点），把单声道信号从 srIn 换到 srOut。
// 采样率是整数：srOut / srIn = L / M（约分后），输出第 i 点落在输入 i·M/L，小数部分只有 L 种，滤波系数预先算好。
std::vector<float> resample(const std::vector<float>& x, int srIn, int srOut) {
    if (srIn == srOut) {
        return x;
    }
    const long g = std::gcd(srIn, srOut);
    const long L = srOut / g, M = srIn / g;
    const double cutoff = std::min(1.0, static_cast<double>(srOut) / srIn) * 0.95; // 相对输入奈奎斯特
    const double width = 32 / cutoff;                                              // 半宽，输入采样数
    const long H = static_cast<long>(std::ceil(width));
    std::vector<float> table(static_cast<size_t>(L * 2 * H));
    for (long p = 0; p < L; ++p) {
        const double frac = static_cast<double>(p) / L;
        for (long j = -H + 1; j <= H; ++j) {
            const double d = j - frac;
            double w = 0.0;
            if (std::abs(d) < width) {
                const double t = d * cutoff;
                const double sinc = t == 0.0 ? 1.0 : std::sin(kPi * t) / (kPi * t);
                w = sinc * cutoff * (0.42 + 0.5 * std::cos(kPi * d / width) + 0.08 * std::cos(2 * kPi * d / width));
            }
            table[static_cast<size_t>(p * 2 * H + (j + H - 1))] = static_cast<float>(w);
        }
    }
    const long nIn = static_cast<long>(x.size());
    const long n = static_cast<long>(static_cast<double>(nIn) * L / M);
    std::vector<float> y(static_cast<size_t>(n));
    for (long i = 0; i < n; ++i) {
        const long pos = i * M;
        const long base = pos / L, p = pos % L;
        const float* w = &table[static_cast<size_t>(p * 2 * H)];
        float acc = 0.0f;
        for (long j = -H + 1; j <= H; ++j) {
            const long k = base + j;
            if (k >= 0 && k < nIn) {
                acc += x[static_cast<size_t>(k)] * w[j + H - 1];
            }
        }
        y[static_cast<size_t>(i)] = acc;
    }
    return y;
}

int decodeFile(const QString& path, const QString& outPath, int srOut) {
    TrackPointer t = Track::newTemporary(path);
    mixxx::AudioSource::OpenParams params;
    params.setChannelCount(mixxx::audio::ChannelCount::stereo());
    const auto src = SoundSourceProxy(t).openAudioSource(params);
    if (!src) {
        std::fprintf(stderr, "打不开：%s\n", path.toUtf8().constData());
        return 1;
    }
    const auto sr = src->getSignalInfo().getSampleRate();
    const int ch = static_cast<int>(src->getSignalInfo().getChannelCount());
    mixxx::AudioSourceStereoProxy proxy(src, 65536);
    mixxx::SampleBuffer buffer(65536 * 2);
    std::vector<float> mono;
    mono.reserve(static_cast<size_t>(src->frameLength()));
    mixxx::IndexRange remaining = src->frameIndexRange();
    // 输出第 i 点必须是播放时曲目的第 i 帧（拍线、波形都按它算）。Mixxx 播放时帧号就是解码器的帧号，
    // 解码器起点之前那段放静音（cachingreader.cpp:351-380 preroll）。Mac 的 CoreAudio 解码器起点是
    // leadingFrames 而不是 0（soundsourcecoreaudio.cpp:181,197），所以这里同样在前面补静音；起点 < 0 就丢掉负的那段。
    const SINT first = remaining.start();
    SINT skip = first < 0 ? -first : 0;
    if (first > 0) {
        mono.assign(static_cast<size_t>(first), 0.0f);
    }
    while (!remaining.empty()) {
        auto chunk = remaining.splitAndShrinkFront(std::min<SINT>(65536, remaining.length()));
        const auto readable = proxy.readSampleFrames(
                mixxx::WritableSampleFrames(chunk, mixxx::SampleBuffer::WritableSlice(buffer)));
        remaining = intersect(remaining, proxy.frameIndexRange());
        const CSAMPLE* p = readable.readableData();
        const auto frames = readable.frameIndexRange().length();
        for (SINT i = 0; i < frames; ++i) {
            if (skip > 0) {
                --skip;
                continue;
            }
            // 和 ffmpeg -ac 1 一样：立体声按 -3 dB 下混，单声道原样（立体声代理把它复制到了两个声道）
            mono.push_back(ch == 1 ? p[2 * i] : 0.70710678f * (p[2 * i] + p[2 * i + 1]));
        }
    }
    const auto y = resample(mono, static_cast<int>(sr.value()), srOut);
    QFile f(outPath);
    if (!f.open(QIODevice::WriteOnly)) {
        std::fprintf(stderr, "写不了：%s\n", outPath.toUtf8().constData());
        return 1;
    }
    f.write(reinterpret_cast<const char*>(y.data()), static_cast<qint64>(y.size() * sizeof(float)));
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    // 协议用单独复制的 stdout，标准输出改到 stderr（Qt / Mixxx 的日志不会混进协议行）
    gOut = fdopen(dup(fileno(stdout)), "w");
    dup2(fileno(stderr), fileno(stdout));
    if (!qEnvironmentVariableIsSet("QT_QPA_PLATFORM")) {
        qputenv("QT_QPA_PLATFORM", "offscreen"); // 不开窗口
    }
    mixxx::Logging::initialize(QString(), mixxx::LogLevel::Warning, mixxx::LogLevel::Warning, mixxx::LogFlag::None);
    MixxxApplication app(argc, argv);
    QTemporaryDir tmp;
    UserSettingsPointer config(new UserSettings(tmp.filePath(QStringLiteral("djstudio-engine.cfg"))));
    ControlDoublePrivate::setUserConfig(config);
    SoundSourceProxy::registerProviders();

    const QStringList args = QCoreApplication::arguments();
    if (args.size() >= 3 && args[1] == QLatin1String("--probe")) {
        const int rc = probeFile(args[2]);
        std::fflush(gOut);
        tmp.remove();
        std::_Exit(rc);
    }
    if (args.size() >= 5 && args[1] == QLatin1String("--decode")) {
        const int rc = decodeFile(args[2], args[3], args[4].toInt());
        tmp.remove();
        std::_Exit(rc);
    }

    if (args.size() >= 5 && args[1] == QLatin1String("--render")) {
        auto* engine = new Engine(config);
        const int rc = engine->render(args[2], args[3], args[4].toDouble(), args.size() >= 6 ? args[5].toDouble() : 0.0);
        std::fflush(gOut);
        tmp.remove();
        std::_Exit(rc);
    }

    const QByteArray mappingPath = qgetenv("DJSTUDIO_MAPPING");
    if (mappingPath.isEmpty()) {
        std::fprintf(stderr, "DJSTUDIO_MAPPING 未设置\n");
        return 2;
    }
    const QString sys = qEnvironmentVariable("DJSTUDIO_SYSTEM_MAPPINGS",
            QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("res/controllers")));
    const double speed = qEnvironmentVariableIsSet("DJSTUDIO_SPEED")
            ? qEnvironmentVariable("DJSTUDIO_SPEED").toDouble()
            : 1.0;
    const bool audio = qEnvironmentVariable("DJSTUDIO_AUDIO", QStringLiteral("1")) != QLatin1String("0");
    auto* engine = new Engine(config);
    const int rc = engine->run(mappingPath, QDir(sys), speed, audio);
    std::fflush(gOut);
    tmp.remove();
    // 不逐个析构 Mixxx 的对象（顺序很讲究，退出时也用不着）：直接结束进程
    std::_Exit(rc);
}
