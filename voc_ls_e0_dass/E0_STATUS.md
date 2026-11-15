# VOC-LS E0 / Dragon1712/dass 状态

- 仓库已通过 GitHub 连接核查，默认分支 main，2026-11-15 的 commit `d5a786dbbe2351d65564bf1245b54c72edfe2164`。
- 项目位于仓库嵌套目录 `pathwyse-main/`，包含实际 CMakeLists.txt、pathwyse.set、src、toy、SPPRCLIB 单实例。
- 已修正 E0 启动器以针对用户 fork 和嵌套路径运行；编译、运行及统计需要在 Linux 执行。
- GitHub 连接是文件读取，并不是远程 Linux Shell。此会话容器中 `git ls-remote https://github.com/Dragon1712/dass.git HEAD` 报 DNS 错，故无真实运行结果。
- 不应把脚本测试和官方 README 中给出的 toy 参考答案当作实测 PathWyse 求解结果。
