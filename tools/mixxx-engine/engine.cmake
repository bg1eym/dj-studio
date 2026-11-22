# DJ Studio 声音引擎：一个可执行文件，只链接 Mixxx 的核心静态库（用不到的目标文件不会进程序）。
# 由 tools/mixxx-engine/build.sh 追加到 Mixxx 源码的 CMakeLists.txt 末尾。
add_executable(djstudio-engine "${DJSTUDIO_ENGINE_DIR}/djstudio_engine.cpp")
target_link_libraries(djstudio-engine PRIVATE mixxx-lib mixxx-gitinfostore)
target_compile_features(djstudio-engine PRIVATE cxx_std_20)
# v5 的 Reverb 实例直接用 Mixxx 带的板式混响（lib/reverb；Mixxx 自己只把这个目录给了 mixxx-lib）
target_include_directories(djstudio-engine SYSTEM PRIVATE "${CMAKE_SOURCE_DIR}/lib/reverb")
target_link_libraries(djstudio-engine PRIVATE Reverb)
# Mac 的依赖里 libsndfile 是静态库，它带的 Ogg Opus 支持要 libopus。Mixxx 自己的构建在 OPUS=ON 时顺带链接上；
# 我们关了 OPUS（不读 .opus），这里只给引擎补上这个库（Mixxx 的 CMakeLists 前面已经 find_package(Opus)）。
if(TARGET Opus::Opus)
  target_link_libraries(djstudio-engine PRIVATE Opus::Opus)
endif()
# 跟着系统默认输出走（插耳机 / 连 AirPods 自动换过去）：直接问 CoreAudio 现在的默认输出设备
if(APPLE)
  target_link_libraries(djstudio-engine PRIVATE "-framework CoreAudio" "-framework CoreFoundation")
endif()
