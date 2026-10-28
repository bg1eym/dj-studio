"""从 Mixxx 源码的 res/schema.xml 生成真实的数据库结构（与 SchemaManager::upgradeToSchemaVersion 相同：
按版本顺序执行每个 revision 的 SQL，按分号切语句，忽略重复加列的错误）。

用法：python3 tools/mixxx_schema.py <mixxx 源码>/res/schema.xml out.sqlite
再导出用到的表：见 djstudio/mixxx/schema_2_5_6.sql 文件头。
"""
import sqlite3
import sys
import xml.etree.ElementTree as ET


def build(schema_xml: str, out: str) -> int:
    revs = {int(r.get("version")): "".join(r.find("sql").itertext())
            for r in ET.parse(schema_xml).getroot().iter("revision")}
    c = sqlite3.connect(out)
    for v in sorted(revs):
        for stmt in revs[v].split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                c.execute(s)
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e):
                    continue
                raise
    c.commit()
    return max(revs)


if __name__ == "__main__":
    print("schema version", build(sys.argv[1], sys.argv[2]))
