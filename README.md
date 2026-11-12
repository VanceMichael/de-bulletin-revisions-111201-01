# 科研资讯·更正与撤稿采集

面向合作资料接收人员的 Python 采集组件。读取通报的标题、正文和声明链接，按请求地址保存单次观察；
在持续追踪模式下，以发布单位的原始条目为主线保存历次正文，区分更正、撤稿与暂时抓取失败。
输入样本都是本地合成数据，example 域名只表示来源身份，不需联网访问。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行；演示启动真实的回环 HTTP 来源、处理响应、事务入库后读取实际结果。
每次演示使用独立临时目录，结束后清理；通常两秒内结束，不回放历史输出。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -v
python3 -m collector demo
```

演示覆盖：原文更正、转载滞后、撤稿，以及“原文保存后、索引更新前进程退出”的中途恢复，
每一步都打印编辑通过查询接口实际看到的结果。

常规导入保留在本地数据库中，可重复运行；默认路径已忽略，不会进入版本库。

```bash
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

持续追踪按轮询工作，每个 `--url` 抓取一次，可重复执行；`--allow` 声明采信 canonical 的来源：

```bash
python3 -m collector track --db var/track.sqlite3 \
    --url https://lab.example/item/1 --allow https://lab.example
python3 -m collector serve --db var/track.sqlite3 --port 8080
```

查询服务监听回环地址，按 Ctrl-C 停止。接口：

- `GET /items` 条目一览：当前版本、状态（active/retracted）、撤稿证据、各链接滞后标记；
- `GET /versions?url=<条目地址>` 历次正文：序号、initial/correction、来源时间、当前标记；
- `GET /status` 采集状态：各链接 ETag/Last-Modified、最近状态、暂时失败记录；
- `GET /events?url=<地址>` 抓取事件证据流；`GET /records` 为旧版导入记录。

## 组件与数据

`collector/domain.py` 负责来源格式、领域校验、链接归一化与条目身份；
`storage.py` 保留原始对象、导入凭据、当前记录，以及条目、链接、版本、索引头和抓取事件。
单次导入在同一事务内提交，异常时整体回滚；相同来源和原文只登记一次凭据。
当前记录以领域标识为键，同键再次导入替换当前观察值，不代表保留了完整修订历史。
`transport.py` 提供请求客户端和本地响应服务；服务按路径依次消费响应脚本，并记录实际请求。
`tracker.py` 是持续追踪引擎：条件请求、版本切换、撤稿与失败区分。
`api.py` 提供只读查询入口。`fixtures/sample.json` 是当前导入器支持的输入；
`fixtures/bulletins.json` 是演示用的合成通报与响应脚本。
其他附件描述同领域的合成页面、协议或事件，可以由调用方构造本地来源。
当前响应服务是顺序脚本工具，不自动推断认证、文件或表单业务；领域对象提供对应的本地交互。

## 追踪语义

- 条目身份只由链接决定：原始条目为主线，转载页经允许的 canonical 归并到主线，
  标题相同不会合并不同研究；canonical 声明仅在目标属于 `--allow` 来源时采信。
- 链接归一化去掉 utm、fbclid 等跟踪参数与碎片，版本号等查询参数保留。
- 再次请求携带各链接自己的 ETag/Last-Modified；304 只沿用本链接缓存的正文，
  不借用其他转载页或主线的缓存。转载只更新自身凭据，不创建版本、不移动主线索引头。
- 版本按解析后的正文内容去重；更正产生新版本并切换索引头，撤稿（410）翻转条目状态，
  暂时失败（5xx、网络异常）只记录证据。变更前后的正文与来源时间都保留在库中。
- 正文先持久化，版本与索引头在单个事务内生效；两阶段之间崩溃不会留下对外可见的
  半成品，重启后重采只产生一次有效版本切换。多工作进程并发发现同一更正时，
  SQLite 写事务串行化保证同样只切换一次。

## 可复现运行

宿主机或容器均不需要网络。镜像准备好以后可以关闭外部网络再运行上述命令。
仓库内不含真实账号、新闻正文或人员资料；不要将生产凭据写入样本或日志。
