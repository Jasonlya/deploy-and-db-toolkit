# deploy-and-db-toolkit

面向客户现场交付的运维自动化工具集：**Tomcat 一键部署** + **多数据库 SQL 校验执行**，把部署和数据库变更中的手工操作收敛成可复用脚本。

## 背景

客户现场交付时，重复劳动主要集中在两件事上：

- **应用服务器部署**：装 JDK、下 Tomcat、配端口和堆内存、注册 systemd、部署 war、处理旧版本，每一步都可能因环境差异出错。
- **数据库变更**：变更 SQL 写在 Word 里，夹杂中文说明、全角标点、智能引号、中英文分号，直接拿到客户库上执行经常报语法错误；多个数据库还要逐个手工跑、逐个记录结果。

本仓库提供两个独立脚本分别解决上述问题，可单独使用，也可组合成"先部署应用、再执行数据库变更"的交付流程。

## 功能

### 1. Tomcat 一键部署（`tomcat-deploy/deploy-tomcat.sh`）

- 自动检测 RHEL 系发行版（CentOS / Rocky / Alma / openEuler / Anolis / 麒麟 / 统信），自动选择 dnf / yum
- 支持 Tomcat 9 / 10 / 11，自动匹配 JDK 8 / 17，已有匹配 JDK 时直接复用
- 自动下载（清华镜像）、解压、修改端口、注册 systemd 服务并开机自启
- 支持部署 `.war` / `.zip` / `.tar.gz` 程序包，自动推断 context，支持 `ROOT` 根路径部署
- war 部署前做完整性校验（`unzip -t` / `jar tf` + `WEB-INF/` 检查）
- 部署前自动备份旧 war/应用目录，输出回滚命令
- 已安装同版本 Tomcat 时自动切换为"应用更新模式"，不重装 Tomcat
- 启动后做端口监听 + HTTP 探测，失败自动打印 `catalina.out` 尾部日志

### 2. 多数据库 SQL 执行（`multi-db-runner/multi_db_runner.py`）

- 从 `.docx` / `.txt` / `.sql` / `.md` 提取 SQL，自动剥离 SQL 前后中文说明文字
- **格式校验与规范化**：全角/半角标点、智能引号、不可见字符、NBSP、全角空格、空白与换行、中英文分号、关键字大小写、括号平衡、未闭合引号/注释
- 状态机区分代码区/字符串/标识符/注释，只规范化代码区，不破坏业务数据
- 支持 **MySQL / PostgreSQL / Oracle / SQL Server / DB2 / 达梦** 6 种数据库
- 统一通过 jaydebeapi + JDBC 执行，达梦支持原生 `dmPython` 驱动回退
- `--check-only --dump-sql` 先导出规范化 SQL 人工复核，再正式执行
- 逐库逐句记录耗时、影响行数、返回数据、错误信息，终端汇总 + 可选 JSON 明细
