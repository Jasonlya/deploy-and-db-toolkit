#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Tomcat 一键部署 + 应用程序包部署脚本
#
# 功能:
#   1. 自动检测 RHEL 系发行版并安装 JDK / Tomcat / systemd 服务
#   2. 支持 Tomcat 9 / 10 / 11，自动匹配 JDK 8 / 17
#   3. 支持部署 .war / .zip / .tar.gz 程序包，部署前自动备份旧版本
#   4. 已安装同版本 Tomcat 时自动切换为"应用更新模式"，不重装 Tomcat
#   5. 启动后做端口监听 + HTTP 探测，失败时自动打印 catalina.out 末尾日志
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

# ---------- 全局默认值 ----------
INSTALL_BASE="/opt/tomcat"          # Tomcat 安装根目录
SERVICE_PREFIX="tomcat"             # systemd 服务名前缀
DEFAULT_PORT=8080                   # 默认 HTTP 端口
DEFAULT_HEAP_MIN="512m"             # 默认最小堆
DEFAULT_HEAP_MAX="1024m"            # 默认最大堆

APP_PACKAGE="${APP_PACKAGE:-}"      # 程序包路径，可由 -p 覆盖
APP_CONTEXT="${APP_CONTEXT:-}"      # 上下文路径，空表示自动推断
EXPLODE="false"                     # 是否解压 war 部署
ASSUME_YES="false"                  # 是否跳过交互确认
DEPLOYED_CONTEXT="ROOT"             # 最终部署的上下文路径
BACKUP_PATH=""                      # 本次备份目录

# ---------- 日志着色 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error(){ echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------- 帮助信息 ----------
usage() {
    cat <<EOF
用法: $0 [选项]
  -p, --package PATH   应用程序包 (.war/.zip/.tar.gz)，不传则交互询问
  -c, --context NAME   上下文路径，默认由包名推断；ROOT 表示根路径
  -e, --explode        解压 war 后删除 war 文件（目录形式部署）
  -y, --yes            跳过确认
  -h, --help           显示帮助

环境变量: APP_PACKAGE, APP_CONTEXT

示例:
  $0 -p ./myapp.war -y
  $0 -p ./myapp.war -c ROOT -e -y
  $0 -p ./release.zip
EOF
}

# ---------- 参数解析 ----------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--package) APP_PACKAGE="$2"; shift 2 ;;
            -c|--context) APP_CONTEXT="$2"; shift 2 ;;
            -e|--explode) EXPLODE="true"; shift ;;
            -y|--yes)     ASSUME_YES="true"; shift ;;
            -h|--help)    usage; exit 0 ;;
            *) error "未知参数: $1（使用 -h 查看帮助）" ;;
        esac
    done
}

# ---------- 版本映射表 ----------
# Tomcat 版本 -> 所需 JDK 主版本
declare -A JDK_VERSIONS=(
    [9]="1.8"
    [10]="17"
    [11]="17"
)
# Tomcat 版本 -> 下载地址（清华镜像）
declare -A TOMCAT_URLS=(
    [9]="https://mirrors.tuna.tsinghua.edu.cn/apache/tomcat/tomcat-9/v9.0.117/bin/apache-tomcat-9.0.117.tar.gz"
    [10]="https://mirrors.tuna.tsinghua.edu.cn/apache/tomcat/tomcat-10/v10.1.54/bin/apache-tomcat-10.1.54.tar.gz"
    [11]="https://mirrors.tuna.tsinghua.edu.cn/apache/tomcat/tomcat-11/v11.0.21/bin/apache-tomcat-11.0.21.tar.gz"
)
# JDK 主版本 -> 包名
declare -A JDK_PKG_MAP=(
    ["1.8"]="java-1.8.0-openjdk-devel"
    ["17"]="java-17-openjdk-devel"
)

# ---------- 系统检测 ----------
# 读取 /etc/os-release，识别包管理器（仅支持 dnf/yum）
detect_os() {
    [[ -f /etc/os-release ]] || error "无法检测操作系统：/etc/os-release 不存在"
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_NAME="${NAME:-Unknown}"; OS_ID="${ID:-unknown}"
    if command -v dnf &>/dev/null; then PKG_MGR="dnf"
    elif command -v yum &>/dev/null; then PKG_MGR="yum"
    else error "未找到 dnf 或 yum，仅支持 RHEL 系发行版"; fi
    info "检测到系统: ${OS_NAME} (${OS_ID})，包管理器: ${PKG_MGR}"
}

# ---------- 基础依赖安装 ----------
install_deps() {
    info "安装基础依赖 (wget tar unzip curl systemd)..."
    "$PKG_MGR" install -y wget tar unzip curl systemd >/dev/null 2>&1 || error "基础依赖安装失败"
    ok "基础依赖安装完成"
}

# ---------- JAVA_HOME 设置 ----------
# 从 java 命令反推 JAVA_HOME，写入 /etc/profile.d/java.sh
set_java_home() {
    local java_bin
    java_bin=$(readlink -f "$(command -v java)" 2>/dev/null || true)
    [[ -z "$java_bin" ]] && error "java 命令不可用"
    JAVA_HOME=$(dirname "$(dirname "$java_bin")")
    export JAVA_HOME
    cat > /etc/profile.d/java.sh <<EOF
export JAVA_HOME=${JAVA_HOME}
export PATH=\$JAVA_HOME/bin:\$PATH
EOF
    chmod +x /etc/profile.d/java.sh
    # shellcheck disable=SC1091
    source /etc/profile.d/java.sh
    info "JAVA_HOME=${JAVA_HOME}"
}

# ---------- JDK 安装 ----------
# 若已有匹配版本则直接复用，否则安装对应 OpenJDK devel 包
install_jdk() {
    local jdk_ver="$1"
    local pkg="${JDK_PKG_MAP[$jdk_ver]:-}"
    [[ -z "$pkg" ]] && error "不支持的 JDK 版本: $jdk_ver"

    if command -v java &>/dev/null; then
        local current; current=$(java -version 2>&1 | head -1)
        if [[ "$current" == *"\"${jdk_ver}"* ]] || [[ "$current" == *"\"1.${jdk_ver}"* ]]; then
            ok "现有 Java 版本符合要求 (${jdk_ver})"; set_java_home; return 0
        fi
        warn "现有 Java 版本与目标 ${jdk_ver} 不匹配，将安装 ${pkg}"
    fi

    info "安装 JDK ${jdk_ver} (${pkg})..."
    "$PKG_MGR" install -y "$pkg" >/dev/null 2>&1 || error "JDK ${jdk_ver} 安装失败"
    set_java_home
    ok "JDK ${jdk_ver} 安装完成"
}

# ---------- 旧 Tomcat 检测 ----------
# 若已有 tomcat-N 服务在运行，询问/自动停止，避免端口冲突
check_and_stop_other_tomcat() {
    local running=()
    while IFS= read -r svc; do [[ -n "$svc" ]] && running+=("$svc"); done < <(
        systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
        | grep -oE 'tomcat-[0-9]+\.service' || true
    )
    [[ ${#running[@]} -eq 0 ]] && return 0

    warn "检测到以下正在运行的 Tomcat 服务，可能占用 ${DEFAULT_PORT} 端口:"
    printf '  - %s\n' "${running[@]}"

    if [[ "$ASSUME_YES" == "true" ]]; then
        info "已指定 -y，自动停止旧服务"
    else
        read -r -p "是否停止这些服务以释放端口？(y/n): " answer
        [[ "$answer" =~ ^[Yy]$ ]] || error "用户取消，为避免端口冲突，安装退出"
    fi

    for svc in "${running[@]}"; do systemctl stop "$svc" 2>/dev/null || true; done
    ok "旧 Tomcat 服务已停止"
}

# ---------- 清理同版本旧环境 ----------
# 停止、禁用并删除同名 systemd 服务与安装目录
cleanup_same_version() {
    local ver="$1"
    local svc_name="${SERVICE_PREFIX}-${ver}"
    if systemctl is-enabled "$svc_name" &>/dev/null 2>&1; then
        systemctl stop "$svc_name" 2>/dev/null || true
        systemctl disable "$svc_name" 2>/dev/null || true
    fi
    rm -f "/etc/systemd/system/${svc_name}.service"
    rm -rf "${INSTALL_BASE:?}/${ver}"
    systemctl daemon-reload 2>/dev/null || true
}

# ---------- 生成 systemd 服务 ----------
# 使用 Type=forking，调用 Tomcat 自带 startup.sh/shutdown.sh
create_systemd_service() {
    local ver="$1"
    local svc_name="${SERVICE_PREFIX}-${ver}"
    local home="${INSTALL_BASE}/${ver}"
    local min_heap="${JVM_HEAP_MIN:-$DEFAULT_HEAP_MIN}"
    local max_heap="${JVM_HEAP_MAX:-$DEFAULT_HEAP_MAX}"

    cat > "/etc/systemd/system/${svc_name}.service" <<EOF
[Unit]
Description=Apache Tomcat ${ver} Server
After=network.target

[Service]
Type=forking
Environment="JAVA_HOME=${JAVA_HOME}"
Environment="CATALINA_HOME=${home}"
Environment="CATALINA_BASE=${home}"
Environment="CATALINA_PID=${home}/temp/tomcat.pid"
Environment="CATALINA_OPTS=-Xms${min_heap} -Xmx${max_heap} -Djava.security.egd=file:/dev/./urandom"
ExecStart=${home}/bin/startup.sh
ExecStop=${home}/bin/shutdown.sh
Restart=always
RestartSec=10
PrivateTmp=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

    chmod 644 "/etc/systemd/system/${svc_name}.service"
    systemctl daemon-reload
    systemctl enable "$svc_name" >/dev/null 2>&1
    ok "systemd 服务 ${svc_name} 已创建并启用"
}

# ---------- war 包完整性校验 ----------
# 优先 unzip -t，其次 jar tf，最后检查 zip 魔数
# 同时要求包含 WEB-INF/，避免把普通 zip 当 war 部署
validate_war() {
    local war="$1"
    local listing=""
    if command -v unzip &>/dev/null; then
        unzip -tq "$war" >/dev/null 2>&1 || { warn "unzip -t 失败"; return 1; }
        listing=$(unzip -l "$war" 2>/dev/null || true)
    elif command -v jar &>/dev/null; then
        listing=$(jar tf "$war" 2>/dev/null) || { warn "jar tf 失败"; return 1; }
    else
        [[ "$(head -c 2 "$war" | od -An -tx1 | tr -d ' \n')" == "504b" ]] || { warn "文件不是 zip 格式"; return 1; }
        return 0
    fi
    [[ "$listing" == *"WEB-INF/"* ]] || { warn "war 中未找到 WEB-INF/ 目录"; return 1; }
    return 0
}

# ---------- 由包名推断 context ----------
# myapp.war -> myapp；release.zip -> release
infer_context_from_name() {
    local base; base=$(basename "$1")
    base="${base%.war}"; base="${base%.zip}"; base="${base%.tar.gz}"; base="${base%.tgz}"
    echo "$base"
}

# ---------- 应用部署 ----------
# 支持三种形态：
#   1. 直接给 .war
#   2. .zip/.tar.gz 内含 .war
#   3. .zip/.tar.gz 内是已解压目录（含 WEB-INF）
# 部署前将旧 war/目录移入 backup，便于回滚
deploy_application() {
    local home="$1"
    local pkg="$2"
    local context="$3"
    local explode="$4"
    local webapps="${home}/webapps"
    local tmp=""
    local war_file=""

    [[ -f "$pkg" ]] || error "程序包不存在: $pkg"
    local ext="${pkg##*.}"

    case "$ext" in
        war)
            war_file="$pkg"
            ;;
        zip|tgz|gz)
            info "解压程序包以查找 war ..."
            tmp=$(mktemp -d)
            if [[ "$ext" == "zip" ]]; then
                unzip -q "$pkg" -d "$tmp" || error "zip 解压失败"
            else
                tar -xzf "$pkg" -C "$tmp" || error "tar.gz 解压失败"
            fi
            war_file=$(find "$tmp" -maxdepth 3 -name "*.war" -type f | head -1)

            # 没有 war，则尝试按"已解压目录"处理
            if [[ -z "$war_file" ]]; then
                local webinf
                webinf=$(find "$tmp" -maxdepth 3 -type d -name "WEB-INF" | head -1)
                [[ -z "$webinf" ]] && error "程序包中未找到 war 或 WEB-INF 目录"
                local app_dir; app_dir=$(dirname "$webinf")
                [[ -z "$context" ]] && context=$(basename "$app_dir")

                BACKUP_PATH="${home}/backup/$(date +%Y%m%d%H%M%S)"
                mkdir -p "$BACKUP_PATH"
                [[ -d "${webapps}/${context}" ]] && mv "${webapps}/${context}" "$BACKUP_PATH/"
                rm -rf "${webapps:?}/${context}"
                cp -a "$app_dir" "${webapps}/${context}"
                DEPLOYED_CONTEXT="$context"
                ok "应用目录已部署到 ${webapps}/${context}"
                rm -rf "$tmp"
                return 0
            fi
            ;;
        *)
            error "不支持的程序包类型: .${ext}（仅支持 .war/.zip/.tar.gz）"
            ;;
    esac

    [[ -z "$context" ]] && context=$(infer_context_from_name "$war_file")

    info "校验 war 包完整性: $(basename "$war_file") ..."
    validate_war "$war_file" || error "war 包校验失败: $war_file"
    ok "war 包校验通过"

    # 备份旧版本
    BACKUP_PATH="${home}/backup/$(date +%Y%m%d%H%M%S)"
    mkdir -p "$BACKUP_PATH"
    if [[ -e "${webapps}/${context}.war" ]]; then
        info "备份旧 war -> ${BACKUP_PATH}/"
        mv "${webapps}/${context}.war" "$BACKUP_PATH/"
    fi
    if [[ -d "${webapps}/${context}" ]]; then
        info "备份旧应用目录 -> ${BACKUP_PATH}/"
        mv "${webapps}/${context}" "$BACKUP_PATH/"
    fi

    # 解压或直接复制 war
    if [[ "$explode" == "true" ]]; then
        info "解压 war 到 ${webapps}/${context} ..."
        rm -rf "${webapps:?}/${context}"
        mkdir -p "${webapps}/${context}"
        unzip -q "$war_file" -d "${webapps}/${context}" || error "war 解压失败"
        ok "应用已解压部署"
    else
        info "复制 war 到 ${webapps}/${context}.war ..."
        cp -f "$war_file" "${webapps}/${context}.war"
        ok "war 已部署"
    fi

    DEPLOYED_CONTEXT="$context"
    [[ -n "$tmp" ]] && rm -rf "$tmp"
}

# ---------- 等待端口监听 ----------
# 轮询 ss 输出，超时返回 1
wait_for_port() {
    local port="$1" timeout="${2:-60}" i=0
    while (( i < timeout )); do
        if ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]${port}$"; then
            return 0
        fi
        sleep 1; ((i++))
    done
    return 1
}

# ---------- 健康检查 ----------
# 先等端口，再用 curl 探测 HTTP 状态码；失败时打印 catalina.out 尾部
health_check() {
    local home="$1" port="$2" context="$3"
    info "等待 Tomcat 监听端口 ${port} ..."
    if ! wait_for_port "$port" 60; then
        warn "端口 ${port} 未在 60 秒内监听"
        tail -n 50 "${home}/logs/catalina.out" 2>/dev/null || true
        return 1
    fi
    ok "Tomcat 已监听端口 ${port}"

    local path="/"
    [[ "$context" != "ROOT" && -n "$context" ]] && path="/${context}/"

    if command -v curl &>/dev/null; then
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
               "http://127.0.0.1:${port}${path}" || true)
        info "应用探测 http://127.0.0.1:${port}${path} -> HTTP ${code:-N/A}"
    fi
    return 0
}

# ---------- 主流程 ----------
main() {
    parse_args "$@"

    echo "============================================"
    echo "    Tomcat 一键部署 + 应用包部署工具"
    echo "============================================"

    detect_os
    install_deps

    # 选择 Tomcat 版本
    echo ""
    echo "请选择要安装的 Tomcat 版本:"
    echo "  1) Tomcat 9  (JDK 8)"
    echo "  2) Tomcat 10 (JDK 17)"
    echo "  3) Tomcat 11 (JDK 17)"
    read -r -p "请输入选项 (1-3): " choice
    case "$choice" in
        1) TOMCAT_VER=9 ;;
        2) TOMCAT_VER=10 ;;
        3) TOMCAT_VER=11 ;;
        *) error "无效选项: $choice" ;;
    esac

    local jdk_ver="${JDK_VERSIONS[$TOMCAT_VER]}"
    local url="${TOMCAT_URLS[$TOMCAT_VER]}"
    local home="${INSTALL_BASE}/${TOMCAT_VER}"
    local svc="${SERVICE_PREFIX}-${TOMCAT_VER}"
    local mode="install"

    # 已安装同版本且存在 systemd 服务 -> 进入更新模式
    if [[ -d "$home" && -f "/etc/systemd/system/${svc}.service" ]]; then
        mode="update"
        info "检测到已安装 Tomcat ${TOMCAT_VER}，进入应用更新模式（不重装 Tomcat）"
    fi

    # ---------- 安装模式 ----------
    if [[ "$mode" == "install" ]]; then
        echo ""
        read -r -p "Tomcat 端口 (默认 ${DEFAULT_PORT}): " input_port
        TOMCAT_PORT="${input_port:-$DEFAULT_PORT}"

        read -r -p "JVM 最小堆 (默认 ${DEFAULT_HEAP_MIN}): " input_min
        JVM_HEAP_MIN="${input_min:-$DEFAULT_HEAP_MIN}"

        read -r -p "JVM 最大堆 (默认 ${DEFAULT_HEAP_MAX}): " input_max
        JVM_HEAP_MAX="${input_max:-$DEFAULT_HEAP_MAX}"

        check_and_stop_other_tomcat
        cleanup_same_version "$TOMCAT_VER"
        install_jdk "$jdk_ver"

        local pkg="/tmp/apache-tomcat-${TOMCAT_VER}.tar.gz"
        info "下载 Tomcat ${TOMCAT_VER} ..."
        wget -q --show-progress -O "$pkg" "$url" || error "Tomcat 下载失败"

        mkdir -p "$INSTALL_BASE"
        info "解压到 ${home} ..."
        tar -xzf "$pkg" -C "$INSTALL_BASE"

        # 压缩包内目录名与目标名不一致时重命名
        if [[ ! -d "$home" ]]; then
            local extracted
            extracted=$(find "$INSTALL_BASE" -maxdepth 1 -type d -name "apache-tomcat-${TOMCAT_VER}*" | head -1)
            [[ -z "$extracted" ]] && error "无法定位解压后的 Tomcat 目录"
            mv "$extracted" "$home"
        fi

        # 修改 HTTP 端口
        if [[ "$TOMCAT_PORT" != "$DEFAULT_PORT" ]]; then
            sed -i "s/port=\"8080\"/port=\"${TOMCAT_PORT}\"/g" "${home}/conf/server.xml"
        fi

        chmod +x "${home}/bin/"*.sh
        mkdir -p "${home}/temp"

        create_systemd_service "$TOMCAT_VER"
        rm -f "$pkg"

    # ---------- 更新模式 ----------
    else
        set_java_home
        # 从 server.xml 读取现有 HTTP 端口
        TOMCAT_PORT=$(sed -n 's/.*port="\([0-9]\{2,5\}\)".*protocol="HTTP\/1.1".*/\1/p' \
                      "${home}/conf/server.xml" | head -1)
        TOMCAT_PORT="${TOMCAT_PORT:-$DEFAULT_PORT}"
        info "从 server.xml 读取端口: ${TOMCAT_PORT}"
    fi

    # ---------- 程序包处理 ----------
    # 未通过 -p 指定且非 -y 时，交互询问是否部署应用
    if [[ -z "$APP_PACKAGE" && "$ASSUME_YES" != "true" ]]; then
        echo ""
        read -r -p "是否部署应用程序包? (y/n): " deploy_ans
        if [[ "$deploy_ans" =~ ^[Yy]$ ]]; then
            read -r -p "请输入程序包路径 (.war/.zip/.tar.gz): " APP_PACKAGE
            read -r -p "上下文路径 (留空自动推断，ROOT 表示根路径): " APP_CONTEXT
        fi
    fi

    if [[ -n "$APP_PACKAGE" ]]; then
        deploy_application "$home" "$APP_PACKAGE" "$APP_CONTEXT" "$EXPLODE"
    else
        info "未指定程序包，跳过应用部署"
    fi

    # ---------- 启动 + 健康检查 ----------
    info "启动/重启 Tomcat ..."
    systemctl restart "$svc"

    local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
    if ! health_check "$home" "$TOMCAT_PORT" "$DEPLOYED_CONTEXT"; then
        error "Tomcat 或应用启动异常，请检查: journalctl -u ${svc} -n 80 --no-pager"
    fi

    local app_path="/"
    [[ "$DEPLOYED_CONTEXT" != "ROOT" && -n "$DEPLOYED_CONTEXT" ]] && app_path="/${DEPLOYED_CONTEXT}/"

    # ---------- 结果输出 ----------
    echo ""
    echo "============================================"
    ok "部署完成"
    echo "============================================"
    echo "  Tomcat  : ${home}"
    echo "  服务名  : ${svc}"
    echo "  端口    : ${TOMCAT_PORT}"
    echo "  应用    : ${APP_PACKAGE:-未部署}"
    echo "  上下文  : ${DEPLOYED_CONTEXT}"
    echo "  访问    : http://${ip}:${TOMCAT_PORT}${app_path}"
    echo "  JAVA_HOME: ${JAVA_HOME}"
    echo ""
    echo "  运维命令:"
    echo "    systemctl status ${svc}"
    echo "    systemctl restart ${svc}"
    echo "    tail -f ${home}/logs/catalina.out"

    # 有备份时打印回滚命令
    if [[ -n "$BACKUP_PATH" ]]; then
        echo ""
        echo "  本次备份目录: ${BACKUP_PATH}"
        echo "  回滚命令示例:"
        echo "    systemctl stop ${svc}"
        echo "    rm -rf ${home}/webapps/${DEPLOYED_CONTEXT}*"
        echo "    cp -a ${BACKUP_PATH}/. ${home}/webapps/"
        echo "    systemctl start ${svc}"
    fi
    echo "============================================"
}

main "$@"
