# ============================================================
# Tomcat 一键部署 + 应用程序包部署脚本
#
# 功能:
#   1. 自动检测系统并安装 JDK / Tomcat / systemd 服务
#   2. 支持 Tomcat 9 / 10 / 11，自动匹配 JDK 8 / 17
#   3. 支持部署 .war / .zip / .tar.gz 程序包，自动备份旧版本
#   4. 已安装同版本 Tomcat 时自动切换为"应用更新模式"
#   5. 启动后做端口监听和 HTTP 探测，失败时打印日志
#
# 用法:
#   sudo ./deploy-tomcat.sh [选项]
#
# 选项:
#   -p, --package PATH   应用程序包路径 (.war/.zip/.tar.gz)
#   -c, --context NAME   上下文路径，默认由包名推断；ROOT 表示根路径
#   -e, --explode        解压 war 后删除 war 文件（目录形式部署）
#   -y, --yes            跳过确认，自动停止旧 Tomcat
#   -h, --help           显示帮助
#
# 环境变量:
#   APP_PACKAGE          等价于 -p
#   APP_CONTEXT          等价于 -c
#
# 示例:
#   # 全新安装并部署 war，自动推断 context
#   sudo ./deploy-tomcat.sh -p ./myapp.war -y
#
#   # 部署到根路径，解压形式运行
#   sudo ./deploy-tomcat.sh -p ./myapp.war -c ROOT -e -y
#
#   # 程序包是 zip，内部包含 war
#   sudo ./deploy-tomcat.sh -p ./release.zip
#
#   # 已安装 Tomcat 时只更新应用
#   sudo ./deploy-tomcat.sh -p ./myapp.war -y
# ============================================================
