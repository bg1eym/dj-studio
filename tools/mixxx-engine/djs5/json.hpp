// 很小的 JSON 读写（v5 调度器用）：不依赖 Qt / Mixxx，所以同一份代码既编进声音引擎，也编进独立的模拟器（sim5）。
// 只支持我们自己协议里用到的：对象、数组、数（double）、字符串（\uXXXX 转成 UTF-8）、true / false / null。
#pragma once

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace djs5 {

class Json {
  public:
    enum Type { Null, Bool, Num, Str, Arr, Obj };
    using Array = std::vector<Json>;
    using Object = std::vector<std::pair<std::string, Json>>; // 保持写入顺序

    Json() = default;
    Json(bool b) : m_type(Bool), m_num(b ? 1.0 : 0.0) {}
    Json(double d) : m_type(Num), m_num(d) {}
    Json(int d) : m_type(Num), m_num(d) {}
    Json(long long d) : m_type(Num), m_num(static_cast<double>(d)) {}
    Json(const char* s) : m_type(Str), m_str(s) {}
    Json(std::string s) : m_type(Str), m_str(std::move(s)) {}
    static Json array() {
        Json j;
        j.m_type = Arr;
        return j;
    }
    static Json object() {
        Json j;
        j.m_type = Obj;
        return j;
    }

    Type type() const { return m_type; }
    bool isNull() const { return m_type == Null; }
    bool isNum() const { return m_type == Num; }
    bool isStr() const { return m_type == Str; }
    bool isArr() const { return m_type == Arr; }
    bool isObj() const { return m_type == Obj; }

    double num(double def = 0.0) const { return m_type == Num || m_type == Bool ? m_num : def; }
    bool boolean(bool def = false) const { return m_type == Bool || m_type == Num ? m_num != 0.0 : def; }
    const std::string& str() const { return m_str; }
    std::string str(const std::string& def) const { return m_type == Str ? m_str : def; }
    const Array& arr() const { return m_arr; }
    const Object& obj() const { return m_obj; }

    // 对象取值：没有就是 null
    const Json& operator[](const std::string& key) const {
        static const Json kNull;
        for (const auto& kv : m_obj) {
            if (kv.first == key) {
                return kv.second;
            }
        }
        return kNull;
    }
    bool has(const std::string& key) const { return !(*this)[key].isNull(); }
    const Json& at(size_t i) const {
        static const Json kNull;
        return i < m_arr.size() ? m_arr[i] : kNull;
    }
    size_t size() const { return m_type == Arr ? m_arr.size() : m_obj.size(); }

    Json& set(const std::string& key, Json v) {
        m_type = Obj;
        for (auto& kv : m_obj) {
            if (kv.first == key) {
                kv.second = std::move(v);
                return *this;
            }
        }
        m_obj.emplace_back(key, std::move(v));
        return *this;
    }
    Json& push(Json v) {
        m_type = Arr;
        m_arr.push_back(std::move(v));
        return *this;
    }

    std::string dump() const {
        std::string out;
        write(out);
        return out;
    }

    static Json parse(const std::string& text) {
        size_t i = 0;
        Json v = parseValue(text, i);
        skip(text, i);
        if (i != text.size()) {
            throw std::runtime_error("JSON 后面多了东西");
        }
        return v;
    }

  private:
    Type m_type = Null;
    double m_num = 0.0;
    std::string m_str;
    Array m_arr;
    Object m_obj;

    static void skip(const std::string& s, size_t& i) {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\r' || s[i] == '\t')) {
            ++i;
        }
    }
    static void utf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out += static_cast<char>(cp);
        } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }
    static unsigned hex4(const std::string& s, size_t& i) {
        if (i + 4 > s.size()) {
            throw std::runtime_error("JSON \\u 不完整");
        }
        unsigned v = static_cast<unsigned>(std::strtoul(s.substr(i, 4).c_str(), nullptr, 16));
        i += 4;
        return v;
    }
    static std::string parseString(const std::string& s, size_t& i) {
        std::string out;
        ++i; // 开头的引号
        while (i < s.size() && s[i] != '"') {
            char c = s[i++];
            if (c != '\\') {
                out += c;
                continue;
            }
            if (i >= s.size()) {
                break;
            }
            char e = s[i++];
            switch (e) {
            case 'n': out += '\n'; break;
            case 't': out += '\t'; break;
            case 'r': out += '\r'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case 'u': {
                unsigned cp = hex4(s, i);
                if (cp >= 0xD800 && cp < 0xDC00 && i + 6 <= s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                    i += 2;
                    unsigned lo = hex4(s, i);
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                }
                utf8(out, cp);
                break;
            }
            default: out += e; break;
            }
        }
        if (i >= s.size()) {
            throw std::runtime_error("JSON 字符串没有结束");
        }
        ++i;
        return out;
    }
    static Json parseValue(const std::string& s, size_t& i) {
        skip(s, i);
        if (i >= s.size()) {
            throw std::runtime_error("JSON 是空的");
        }
        char c = s[i];
        if (c == '{') {
            Json j = object();
            ++i;
            skip(s, i);
            if (i < s.size() && s[i] == '}') {
                ++i;
                return j;
            }
            while (true) {
                skip(s, i);
                if (i >= s.size() || s[i] != '"') {
                    throw std::runtime_error("JSON 对象的键要是字符串");
                }
                std::string k = parseString(s, i);
                skip(s, i);
                if (i >= s.size() || s[i] != ':') {
                    throw std::runtime_error("JSON 对象少了冒号");
                }
                ++i;
                j.m_obj.emplace_back(std::move(k), parseValue(s, i));
                skip(s, i);
                if (i < s.size() && s[i] == ',') {
                    ++i;
                    continue;
                }
                if (i < s.size() && s[i] == '}') {
                    ++i;
                    return j;
                }
                throw std::runtime_error("JSON 对象没有结束");
            }
        }
        if (c == '[') {
            Json j = array();
            ++i;
            skip(s, i);
            if (i < s.size() && s[i] == ']') {
                ++i;
                return j;
            }
            while (true) {
                j.m_arr.push_back(parseValue(s, i));
                skip(s, i);
                if (i < s.size() && s[i] == ',') {
                    ++i;
                    continue;
                }
                if (i < s.size() && s[i] == ']') {
                    ++i;
                    return j;
                }
                throw std::runtime_error("JSON 数组没有结束");
            }
        }
        if (c == '"') {
            return Json(parseString(s, i));
        }
        if (s.compare(i, 4, "true") == 0) {
            i += 4;
            return Json(true);
        }
        if (s.compare(i, 5, "false") == 0) {
            i += 5;
            return Json(false);
        }
        if (s.compare(i, 4, "null") == 0) {
            i += 4;
            return Json();
        }
        char* end = nullptr;
        double d = std::strtod(s.c_str() + i, &end);
        if (end == s.c_str() + i) {
            throw std::runtime_error("JSON 里有看不懂的值");
        }
        i = static_cast<size_t>(end - s.c_str());
        return Json(d);
    }

    static void writeString(std::string& out, const std::string& s) {
        out += '"';
        for (unsigned char c : s) {
            switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    std::snprintf(b, sizeof(b), "\\u%04x", c);
                    out += b;
                } else {
                    out += static_cast<char>(c);
                }
            }
        }
        out += '"';
    }
    void write(std::string& out) const {
        switch (m_type) {
        case Null: out += "null"; break;
        case Bool: out += m_num != 0.0 ? "true" : "false"; break;
        case Num: {
            // NaN / 无穷大写成 null。不靠 isfinite：引擎跟着 Mixxx 用 -ffast-math 编，那个判断会被优化成恒真
            if (!(m_num > -1e300 && m_num < 1e300)) {
                out += "null";
                break;
            }
            char b[40];
            if (m_num == std::floor(m_num) && std::fabs(m_num) < 1e15) {
                std::snprintf(b, sizeof(b), "%.0f", m_num);
            } else {
                std::snprintf(b, sizeof(b), "%.9g", m_num);
            }
            out += b;
            break;
        }
        case Str: writeString(out, m_str); break;
        case Arr:
            out += '[';
            for (size_t k = 0; k < m_arr.size(); ++k) {
                if (k) {
                    out += ',';
                }
                m_arr[k].write(out);
            }
            out += ']';
            break;
        case Obj:
            out += '{';
            for (size_t k = 0; k < m_obj.size(); ++k) {
                if (k) {
                    out += ',';
                }
                writeString(out, m_obj[k].first);
                out += ':';
                m_obj[k].second.write(out);
            }
            out += '}';
            break;
        }
    }
};

} // namespace djs5
