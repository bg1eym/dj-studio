# DJ Studio 声音引擎：一个可执行文件，只链接 Mixxx 的核心静态库（用不到的目标文件不会进程序）。
# 由 tools/mixxx-engine/build.sh 追加到 Mixxx 源码的 CMakeLists.txt 末尾。
add_executable(djstudio-engine "${DJSTUDIO_ENGINE_DIR}/djstudio_engine.cpp")
target_link_libraries(djstudio-engine PRIVATE mixxx-lib mixxx-gitinfostore)
target_compile_features(djstudio-engine PRIVATE cxx_std_20)
